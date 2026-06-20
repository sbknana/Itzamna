#!/usr/bin/env python3
"""ForgeSmith GEPA — Automatic prompt evolution using DSPy GEPA optimizer.

Uses DSPy's GEPA (Generalized Efficient Prompt Adaptation) to evolve role
prompts based on historical agent episode data. GEPA reflects on failure
traces to propose instruction improvements, then validates them against
a success-rate metric.

Pipeline position: runs weekly via ForgeSmith (Phase 4.8, after OPRO).

Design:
- Wraps each role prompt as a DSPy Module with a single Predict component
- Converts agent_episodes into DSPy Examples (input=task_description, output=outcome)
- GEPA evolves the instruction text using reflection on failure traces
- Evolved prompts are version-stamped (e.g., developer_v2.md) for A/B testing
- Safety rails: max 20% change per evolution, protected sections, rollback

Usage:
    # As part of ForgeSmith pipeline (integrated in run_full)
    python3 forgesmith.py --auto

    # Standalone for testing
    python3 forgesmith_gepa.py --role developer
    python3 forgesmith_gepa.py --dry-run
    python3 forgesmith_gepa.py --status

Copyright 2026, Forgeborn
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import dspy
except ImportError:
    dspy = None

# --- Paths ---

SCRIPT_DIR = Path(__file__).resolve().parent
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path(__file__).resolve().parent / "theforge.db"),
)
PROMPTS_DIR = SCRIPT_DIR / "prompts"
BACKUP_DIR = SCRIPT_DIR / ".forgesmith-backups"

# --- Constants ---

# H3 fix: Whitelist valid role names to prevent path traversal in backup filenames
ALLOWED_ROLES = frozenset({
    "developer", "tester", "planner", "evaluator", "security-reviewer",
    "frontend-designer", "integration-tester", "debugger", "code-reviewer",
})

MIN_EPISODES_FOR_GEPA = 20
MAX_DIFF_RATIO = 0.20  # Max 20% change per evolution cycle
GEPA_BUDGET = "light"  # light/medium/heavy — start conservative
AB_SPLIT_RATIO = 0.5   # 50/50 A/B split
MIN_AB_TASKS_FOR_ROLLBACK = 10  # Need 10+ tasks before judging
EPISODE_HISTORY_OVERLAP_THRESHOLD = 0.4  # Mutation-to-failed-episode similarity
EPISODE_HISTORY_PENALTY = 0.7  # Multiply fitness by this when matching failed episodes
SUCCESS_OUTCOMES = {"success", "tests_passed", "no_tests"}
FAILURE_OUTCOMES = {"early_terminated", "blocked", "cycles_exhausted",
                    "developer_max_turns"}

# Sections that must NEVER be removed or modified by GEPA
PROTECTED_SECTION_PATTERNS = [
    r"## Output Format",
    r"## Output Requirements",
    r"RESULT:\s*success\s*\|\s*blocked\s*\|\s*failed",
    r"SUMMARY:",
    r"FILES_CHANGED:",
    r"BLOCKERS:",
    r"REFLECTION:",
    r"## Git Commit Requirements",
    r"git\s+add.*git\s+commit",
    r"\(auto-tuned\)",
]


# --- DB Helpers ---

def get_db(write=False):
    """Connect to TheForge SQLite database."""
    db_path = os.environ.get("THEFORGE_DB", THEFORGE_DB)
    if write:
        conn = sqlite3.connect(db_path)
    else:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def log(msg):
    """Print timestamped message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [GEPA] {msg}")


# --- Keyword overlap (local copy from equipa/parsing.py) ---

def compute_keyword_overlap(text_a: str, text_b: str) -> float:
    """Compute simple keyword overlap score between two texts.

    Returns a float between 0.0 and 1.0 representing the fraction of
    words in common (Jaccard similarity on word sets).
    """
    if not text_a or not text_b:
        return 0.0
    words_a = set(re.findall(r'\w+', text_a.lower()))
    words_b = set(re.findall(r'\w+', text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# --- Episode history check for GEPA candidates ---

def get_failed_episodes_by_keywords(
    role: str,
    q_value_threshold: float = 0.3,
    lookback_days: int = 90,
) -> list[dict]:
    """Fetch failed episodes for a role with low q_values.

    Returns list of dicts with approach_summary, outcome, q_value, reflection.
    Used to detect when a GEPA candidate resembles a previously failed approach.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT ae.id, ae.approach_summary, ae.outcome,
                      ae.q_value, ae.reflection, ae.error_patterns
               FROM agent_episodes ae
               WHERE ae.role = ?
                 AND ae.q_value < ?
                 AND ae.outcome IN ('early_terminated', 'blocked',
                                    'cycles_exhausted', 'developer_max_turns')
                 AND ae.created_at >= datetime('now', ?)
                 AND ae.approach_summary IS NOT NULL
                 AND ae.approach_summary != ''
               ORDER BY ae.q_value ASC
               LIMIT 200""",
            (role, q_value_threshold, f"-{lookback_days} days"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_episode_history_for_candidate(
    role: str,
    mutation_diff: str,
    overlap_threshold: float = EPISODE_HISTORY_OVERLAP_THRESHOLD,
    penalty_factor: float = EPISODE_HISTORY_PENALTY,
) -> dict:
    """Check if a GEPA mutation resembles previously failed approaches.

    Compares the textual diff (behavioral change) of a candidate mutation
    against approach_summaries of failed episodes with low q_values.

    Args:
        role: The agent role being evolved.
        mutation_diff: The textual diff between old and new prompt.
        overlap_threshold: Minimum keyword overlap to consider a match.
        penalty_factor: Multiplier applied to fitness when matches found.

    Returns:
        dict with keys:
        - penalize (bool): Whether to apply a penalty.
        - matching_episodes (int): Number of failed episodes that matched.
        - max_overlap (float): Highest overlap score found.
        - penalty_factor (float): The multiplier to apply (1.0 if no penalty).
    """
    if not mutation_diff or not mutation_diff.strip():
        return {
            "penalize": False,
            "matching_episodes": 0,
            "max_overlap": 0.0,
            "penalty_factor": 1.0,
        }

    failed_episodes = get_failed_episodes_by_keywords(role)
    if not failed_episodes:
        return {
            "penalize": False,
            "matching_episodes": 0,
            "max_overlap": 0.0,
            "penalty_factor": 1.0,
        }

    matching_count = 0
    max_overlap = 0.0

    for ep in failed_episodes:
        ep_text = (
            (ep.get("approach_summary", "") or "")
            + " "
            + (ep.get("reflection", "") or "")
        )
        overlap = compute_keyword_overlap(mutation_diff, ep_text)
        if overlap > max_overlap:
            max_overlap = overlap
        if overlap > overlap_threshold:
            matching_count += 1

    should_penalize = matching_count > 0
    effective_penalty = penalty_factor if should_penalize else 1.0

    if should_penalize:
        log(
            f"GEPA candidate for {role} resembles {matching_count} "
            f"failed episode(s) (max overlap: {max_overlap:.2f}) "
            f"— penalizing score (x{effective_penalty})"
        )

    return {
        "penalize": should_penalize,
        "matching_episodes": matching_count,
        "max_overlap": round(max_overlap, 4),
        "penalty_factor": effective_penalty,
    }


def extract_mutation_diff(old_text: str, new_text: str) -> str:
    """Extract the behavioral change between old and new prompt text.

    Returns a string containing only the added/changed lines from the diff,
    which represents the key behavioral mutation GEPA is proposing.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, n=0)

    added_lines = []
    for line in diff:
        # Capture added lines (excluding diff headers)
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:].strip())

    return " ".join(added_lines)


# ============================================================
# STEP 1: Collect training data from agent_episodes
# ============================================================

def collect_episodes_for_role(role, lookback_days=60):
    """Fetch episodes for a role with sufficient data for GEPA training.

    Returns list of episode dicts with task context, outcome, and reflection.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT ae.id, ae.task_id, ae.role, ae.task_type, ae.project_id,
                  ae.approach_summary, ae.turns_used, ae.outcome,
                  ae.error_patterns, ae.reflection, ae.q_value,
                  t.title as task_title, t.description as task_description
           FROM agent_episodes ae
           LEFT JOIN tasks t ON ae.task_id = t.id
           WHERE ae.role = ?
             AND ae.created_at >= datetime('now', ?)
             AND ae.reflection IS NOT NULL AND ae.reflection != ''
           ORDER BY ae.created_at DESC""",
        (role, f"-{lookback_days} days"),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def episodes_to_dspy_examples(episodes):
    """Convert agent episodes into DSPy Example objects for GEPA training.

    Each example represents a task execution with:
    - Input: task description + context
    - Output: whether the agent succeeded
    - Metadata: reflection, error patterns for GEPA feedback
    """
    examples = []
    for ep in episodes:
        task_desc = (ep.get("task_description") or ep.get("task_title") or
                     f"Task #{ep['task_id']}")
        task_type = ep.get("task_type") or "feature"
        outcome = ep.get("outcome", "unknown")
        is_success = outcome in SUCCESS_OUTCOMES
        reflection = ep.get("reflection") or ""
        error_patterns_raw = ep.get("error_patterns") or ""
        turns = ep.get("turns_used") or 0
        q_value = ep.get("q_value", 0.5)

        # Extract structured failure class from JSON error_patterns if available
        failure_class = ""
        error_patterns_display = error_patterns_raw[:300]
        try:
            parsed_ep = json.loads(error_patterns_raw) if error_patterns_raw else None
            if isinstance(parsed_ep, dict) and "failure_class" in parsed_ep:
                failure_class = parsed_ep["failure_class"]
                secondary = parsed_ep.get("secondary_classes", [])
                signals = parsed_ep.get("signals", [])
                # Build a concise display for feedback
                parts = [failure_class]
                if secondary:
                    parts[0] += f" +{','.join(secondary)}"
                if signals:
                    parts.append(signals[0][:100])
                error_patterns_display = ": ".join(parts)
        except (json.JSONDecodeError, TypeError):
            pass

        example = dspy.Example(
            task_description=task_desc[:500],
            task_type=task_type,
            # The "expected" output is the success indicator
            success=str(is_success),
            # Metadata for GEPA feedback
            outcome=outcome,
            reflection=reflection[:500],
            error_patterns=error_patterns_display,
            failure_class=failure_class,
            turns_used=str(turns),
            q_value=str(round(q_value, 2)),
        ).with_inputs("task_description", "task_type")

        examples.append(example)

    return examples


# ============================================================
# STEP 2: Define DSPy Module for prompt evolution
# ============================================================

# dspy is optional. When it is not installed, `dspy` is None (see import guard
# above) and this module is still imported transitively by forgesmith ->
# build_system_prompt on every task dispatch. Fall back to `object` so the class
# definition does not crash at import time; GEPA's entry points (run_gepa /
# run_gepa_for_role) already bail out early when dspy is None, so this class is
# never instantiated in that state.
_DspyModuleBase = dspy.Module if dspy is not None else object


class RolePromptModule(_DspyModuleBase):
    """DSPy Module that wraps a EQUIPA role prompt.

    GEPA evolves the instruction text of the inner Predict component.
    The instruction becomes the evolved prompt content.
    """

    def __init__(self, role, current_prompt_text):
        super().__init__()
        self.role = role
        # Create a signature that represents the role's task
        self.predict = dspy.Predict(
            dspy.Signature(
                "task_description, task_type -> success",
                instructions=current_prompt_text,
            )
        )

    def forward(self, task_description, task_type):
        return self.predict(
            task_description=task_description,
            task_type=task_type,
        )


def gepa_metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
    """GEPA feedback metric for prompt evolution.

    Compares predicted success against actual outcome and provides
    structured feedback from the agent's reflection.

    Returns dspy.Prediction(score, feedback) for predictor-level feedback.
    """
    actual_success = gold.success == "True"
    predicted_success = pred.success.strip().lower() in ("true", "yes", "1")

    # Base score: did prediction match reality?
    score = 1.0 if predicted_success == actual_success else 0.0

    # Build feedback from episode metadata
    feedback_parts = []
    if not actual_success:
        outcome = gold.outcome
        failure_class = getattr(gold, "failure_class", "")
        if failure_class:
            feedback_parts.append(f"Task FAILED — failure class: {failure_class} (outcome: {outcome})")
        else:
            feedback_parts.append(f"Task FAILED with outcome: {outcome}")
        if gold.error_patterns:
            feedback_parts.append(f"Error patterns: {gold.error_patterns}")
        if gold.reflection:
            feedback_parts.append(f"Agent reflection: {gold.reflection}")
        turns = gold.turns_used
        feedback_parts.append(f"Turns used: {turns}")
    else:
        feedback_parts.append("Task SUCCEEDED")
        if gold.reflection:
            feedback_parts.append(f"Success approach: {gold.reflection}")

    feedback = "\n".join(feedback_parts)

    if pred_name is not None:
        return dspy.Prediction(score=score, feedback=feedback)
    return score


# ============================================================
# STEP 3: Run GEPA optimization
# ============================================================

def run_gepa_for_role(role, episodes, cfg, dry_run=False):
    """Run GEPA optimization for a single role.

    Steps:
    1. Convert episodes to DSPy examples
    2. Split into train/val sets
    3. Create DSPy Module with current prompt
    4. Run GEPA optimizer
    5. Extract evolved instructions
    6. Validate safety rails

    Returns dict with evolved prompt text and metadata, or None on failure.
    """
    if dspy is None:
        log("DSPy not installed — cannot run GEPA")
        return None

    prompt_file = PROMPTS_DIR / f"{role}.md"
    if not prompt_file.exists():
        log(f"Prompt file not found: {prompt_file}")
        return None

    current_prompt = prompt_file.read_text(encoding="utf-8")

    # Convert episodes to DSPy examples
    examples = episodes_to_dspy_examples(episodes)
    if len(examples) < MIN_EPISODES_FOR_GEPA:
        log(f"Insufficient episodes for {role}: {len(examples)} "
            f"(need {MIN_EPISODES_FOR_GEPA})")
        return None

    # Split: 70% train, 30% val
    split_idx = int(len(examples) * 0.7)
    trainset = examples[:split_idx]
    valset = examples[split_idx:]

    log(f"Training data: {len(trainset)} train, {len(valset)} val examples")

    if dry_run:
        log(f"[DRY RUN] Would run GEPA for {role} with {len(examples)} examples")
        return {
            "role": role,
            "evolved_prompt": None,
            "dry_run": True,
            "train_size": len(trainset),
            "val_size": len(valset),
        }

    # Configure DSPy LM
    # SAFETY: Default to local Ollama model to avoid Anthropic API charges.
    # The Anthropic API costs real money ($77 bill on 2026-02-23 from 421 GEPA rollouts).
    # Use --model flag or gepa.model in config to override.
    # For Anthropic API: set model to "anthropic/claude-sonnet-4-20250514" AND
    #   set ANTHROPIC_API_KEY env var. This is intentionally NOT the default.
    gepa_cfg = cfg.get("gepa", {})
    model_name = gepa_cfg.get("model", "ollama_chat/devstral-small-2:24b")
    reflection_model = gepa_cfg.get("reflection_model", model_name)

    try:
        if model_name.startswith("anthropic/"):
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                log("ERROR: ANTHROPIC_API_KEY required for Anthropic models but not set.")
                log("HINT: Set via environment variable (never hardcode).")
                log("HINT: Or use a local model instead (default: ollama_chat/devstral-small-2:24b)")
                return None
            # H2 fix: Validate key format without logging it
            if not api_key.startswith("sk-ant-"):
                log("ERROR: ANTHROPIC_API_KEY doesn't look valid (expected sk-ant-... prefix)")
                return None
            lm = dspy.LM(model_name, api_key=api_key)
            reflection_lm = dspy.LM(reflection_model, api_key=api_key)
            log(f"WARNING: Using Anthropic API — this costs real money! Model: {model_name}")
            # H2: Do NOT log key or key length — only confirm it was loaded from env
        elif model_name.startswith("ollama"):
            # Local model via Ollama on forge-inference (free)
            ollama_base = os.environ.get("OLLAMA_URL", "http://localhost:11434")
            lm = dspy.LM(model_name, api_base=ollama_base)
            reflection_lm = dspy.LM(reflection_model, api_base=ollama_base)
            log(f"Using local Ollama model (free): {model_name} at {ollama_base}")
        else:
            lm = dspy.LM(model_name)
            reflection_lm = dspy.LM(reflection_model)
        dspy.configure(lm=lm)
    except Exception as e:
        log(f"Failed to configure DSPy LM: {e}")
        return None

    # Create module with current prompt
    student = RolePromptModule(role, current_prompt)

    # Configure GEPA optimizer
    budget = gepa_cfg.get("budget", GEPA_BUDGET)
    try:
        optimizer = dspy.GEPA(
            metric=gepa_metric,
            auto=budget,
            reflection_lm=reflection_lm,
            skip_perfect_score=True,
            log_dir=str(SCRIPT_DIR / ".gepa-logs" / role),
            track_stats=True,
            seed=42,
        )
    except Exception as e:
        log(f"Failed to create GEPA optimizer: {e}")
        return None

    # Run optimization
    log(f"Running GEPA optimization for {role} (budget={budget})...")
    try:
        optimized = optimizer.compile(
            student,
            trainset=trainset,
            valset=valset,
        )
    except Exception as e:
        log(f"GEPA optimization failed: {e}")
        return None

    # Extract evolved instruction text
    evolved_prompt = None
    try:
        # The optimized module's predict component has evolved instructions
        evolved_sig = optimized.predict.signature
        evolved_prompt = evolved_sig.instructions
    except AttributeError:
        log("Could not extract evolved instructions from optimized module")
        return None

    if not evolved_prompt or evolved_prompt == current_prompt:
        log(f"GEPA produced no changes for {role}")
        return None

    # Validate the evolved prompt
    is_valid, reason = validate_evolved_prompt(current_prompt, evolved_prompt)
    if not is_valid:
        log(f"Evolved prompt rejected: {reason}")
        return None

    # Check mutation against failed episode history
    mutation_diff = extract_mutation_diff(current_prompt, evolved_prompt)
    history_check = check_episode_history_for_candidate(role, mutation_diff)

    return {
        "role": role,
        "evolved_prompt": evolved_prompt,
        "current_prompt": current_prompt,
        "dry_run": False,
        "train_size": len(trainset),
        "val_size": len(valset),
        "episode_history_check": history_check,
    }


# ============================================================
# STEP 4: Safety rails
# ============================================================

def calculate_diff_ratio(old_text, new_text):
    """Calculate the ratio of changed content between two texts.

    Returns float between 0.0 (identical) and 1.0 (completely different).
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    return 1.0 - matcher.ratio()


def check_protected_sections(old_text, new_text):
    """Verify that protected sections are preserved in the evolved prompt.

    Returns (is_safe, list_of_violations).
    """
    violations = []
    for pattern in PROTECTED_SECTION_PATTERNS:
        old_matches = re.findall(pattern, old_text, re.IGNORECASE | re.DOTALL)
        new_matches = re.findall(pattern, new_text, re.IGNORECASE | re.DOTALL)
        if old_matches and not new_matches:
            violations.append(f"Protected pattern removed: {pattern}")
    return len(violations) == 0, violations


def validate_evolved_prompt(current_prompt, evolved_prompt):
    """Validate an evolved prompt against safety rails.

    Returns (is_valid, rejection_reason).
    """
    # Check diff ratio
    diff_ratio = calculate_diff_ratio(current_prompt, evolved_prompt)
    if diff_ratio > MAX_DIFF_RATIO:
        return False, (f"Diff ratio {diff_ratio:.1%} exceeds max "
                       f"{MAX_DIFF_RATIO:.0%}")

    # Check protected sections
    is_safe, violations = check_protected_sections(current_prompt, evolved_prompt)
    if not is_safe:
        return False, f"Protected sections violated: {'; '.join(violations)}"

    # Check minimum length (evolved prompt shouldn't be drastically shorter)
    if len(evolved_prompt) < len(current_prompt) * 0.7:
        return False, (f"Evolved prompt too short: {len(evolved_prompt)} chars "
                       f"vs {len(current_prompt)} original")

    # Check that Output Format / RESULT block is intact
    result_block_pattern = r"RESULT:.*success.*blocked.*failed"
    if (re.search(result_block_pattern, current_prompt, re.DOTALL) and
            not re.search(result_block_pattern, evolved_prompt, re.DOTALL)):
        return False, "RESULT block format was removed"

    return True, None


# ============================================================
# STEP 5: Version-stamped prompt storage + A/B testing
# ============================================================

def get_current_prompt_version(role):
    """Get the current active prompt version for a role.

    Checks forgesmith_changes for the latest gepa_evolution entry.
    Returns version number (int), 1 if no evolutions exist yet.
    """
    conn = get_db()
    row = conn.execute(
        """SELECT new_value FROM forgesmith_changes
           WHERE change_type = 'gepa_evolution'
             AND target_file LIKE ?
             AND reverted_at IS NULL
           ORDER BY created_at DESC LIMIT 1""",
        (f"%{role}%",),
    ).fetchone()
    conn.close()

    if row and row["new_value"]:
        try:
            data = json.loads(row["new_value"])
            return data.get("version", 1)
        except (json.JSONDecodeError, TypeError):
            pass
    return 1


def store_evolved_prompt(result, run_id, cfg, dry_run=False):
    """Store an evolved prompt as a version-stamped file and record in DB.

    Creates: prompts/{role}_v{N}.md
    Records in forgesmith_changes with change_type='gepa_evolution'.
    """
    role = result["role"]
    evolved_prompt = result["evolved_prompt"]
    current_prompt = result["current_prompt"]

    current_version = get_current_prompt_version(role)
    new_version = current_version + 1
    versioned_filename = f"{role}_v{new_version}.md"
    versioned_path = PROMPTS_DIR / versioned_filename

    diff_ratio = calculate_diff_ratio(current_prompt, evolved_prompt)

    if dry_run:
        log(f"[DRY RUN] Would create {versioned_path}")
        log(f"[DRY RUN] Version: v{new_version}, diff: {diff_ratio:.1%}")
        return {"version": new_version, "file": str(versioned_path),
                "diff_ratio": diff_ratio, "stored": False}

    # Backup current baseline
    # H3 fix: Validate role against whitelist to prevent path traversal
    if role not in ALLOWED_ROLES:
        log(f"ERROR: Role '{role}' not in ALLOWED_ROLES. "
            f"Valid roles: {sorted(ALLOWED_ROLES)}")
        return {"version": new_version, "file": str(versioned_path),
                "diff_ratio": diff_ratio, "stored": False, "error": "invalid_role"}
    BACKUP_DIR.mkdir(exist_ok=True)
    safe_role = os.path.basename(role)  # Belt-and-suspenders: strip any path components
    baseline = PROMPTS_DIR / f"{safe_role}.md"
    if baseline.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"{safe_role}.md.{ts}.pre-gepa.bak"
        # Verify backup stays within BACKUP_DIR
        if not backup_path.resolve().is_relative_to(BACKUP_DIR.resolve()):
            log(f"ERROR: Backup path escapes BACKUP_DIR: {backup_path}")
            return {"version": new_version, "file": str(versioned_path),
                    "diff_ratio": diff_ratio, "stored": False, "error": "path_traversal"}
        shutil.copy2(baseline, backup_path)

    # Write versioned prompt file
    versioned_path.write_text(evolved_prompt, encoding="utf-8")
    log(f"Created {versioned_path} (v{new_version}, diff: {diff_ratio:.1%})")

    # Record in forgesmith_changes
    change_data = json.dumps({
        "version": new_version,
        "versioned_file": str(versioned_path),
        "baseline_file": str(baseline),
        "diff_ratio": round(diff_ratio, 4),
        "train_size": result.get("train_size", 0),
        "val_size": result.get("val_size", 0),
    })

    history_check = result.get("episode_history_check", {})
    evidence = json.dumps({
        "episodes_used": result.get("train_size", 0) + result.get("val_size", 0),
        "diff_ratio": round(diff_ratio, 4),
        "episode_history_penalty": history_check.get("penalize", False),
        "matching_failed_episodes": history_check.get("matching_episodes", 0),
        "max_episode_overlap": history_check.get("max_overlap", 0.0),
    })

    conn = get_db(write=True)
    conn.execute(
        """INSERT INTO forgesmith_changes
           (run_id, change_type, target_file, old_value, new_value,
            rationale, evidence)
           VALUES (?, 'gepa_evolution', ?, ?, ?, ?, ?)""",
        (run_id, str(baseline), current_prompt[:500],
         change_data,
         f"GEPA evolution v{new_version} for {role} "
         f"(diff: {diff_ratio:.1%}, "
         f"episodes: {result.get('train_size', 0) + result.get('val_size', 0)})",
         evidence),
    )
    conn.commit()
    conn.close()

    return {"version": new_version, "file": str(versioned_path),
            "diff_ratio": diff_ratio, "stored": True}


def get_ab_prompt_for_role(role):
    """Select which prompt version to use for A/B testing.

    Returns (prompt_file_path, version_label) tuple.

    Strategy:
    - If an evolved version exists and A/B testing is active: 50/50 split
    - Otherwise: use baseline prompt
    """
    import random

    baseline_path = PROMPTS_DIR / f"{role}.md"

    # Find the latest evolved version
    current_version = get_current_prompt_version(role)
    if current_version <= 1:
        return baseline_path, "baseline"

    versioned_path = PROMPTS_DIR / f"{role}_v{current_version}.md"
    if not versioned_path.exists():
        return baseline_path, "baseline"

    # Check if we have enough data to judge — if evolved version is
    # already proven worse, don't use it
    if _should_rollback(role, current_version):
        log(f"Evolved prompt v{current_version} for {role} performing worse — "
            f"using baseline")
        return baseline_path, "baseline"

    # 50/50 A/B split
    if random.random() < AB_SPLIT_RATIO:
        return versioned_path, f"v{current_version}"
    else:
        return baseline_path, "baseline"


def _should_rollback(role, version):
    """Check if an evolved prompt version should be rolled back.

    Rollback if:
    - 10+ tasks have used this version
    - Success rate is lower than baseline
    """
    conn = get_db()

    # Get success rates for baseline vs evolved version
    baseline_stats = conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes
           FROM agent_runs
           WHERE role = ?
             AND prompt_version = 'baseline'
             AND started_at >= datetime('now', '-30 days')""",
        (role,),
    ).fetchone()

    evolved_stats = conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes
           FROM agent_runs
           WHERE role = ?
             AND prompt_version = ?
             AND started_at >= datetime('now', '-30 days')""",
        (role, f"v{version}"),
    ).fetchone()

    conn.close()

    if not evolved_stats or evolved_stats["total"] < MIN_AB_TASKS_FOR_ROLLBACK:
        return False  # Not enough data yet

    baseline_rate = (baseline_stats["successes"] / max(baseline_stats["total"], 1)
                     if baseline_stats and baseline_stats["total"] > 0 else 0)
    evolved_rate = evolved_stats["successes"] / max(evolved_stats["total"], 1)

    if evolved_rate < baseline_rate:
        log(f"Rollback check: {role} v{version} success rate "
            f"{evolved_rate:.0%} < baseline {baseline_rate:.0%}")
        return True

    return False


def rollback_evolved_prompt(role, version):
    """Roll back an evolved prompt by marking it reverted in the DB.

    Does NOT delete the versioned file (kept for records).
    """
    conn = get_db(write=True)
    conn.execute(
        """UPDATE forgesmith_changes
           SET reverted_at = datetime('now')
           WHERE change_type = 'gepa_evolution'
             AND target_file LIKE ?
             AND reverted_at IS NULL""",
        (f"%{role}%",),
    )
    conn.commit()
    conn.close()
    log(f"Rolled back GEPA evolution for {role} (v{version})")


# ============================================================
# STEP 6: A/B test performance tracking
# ============================================================

def get_ab_test_status(role=None):
    """Get current A/B test status for all roles (or a specific role).

    Returns dict with baseline vs evolved success rates.
    """
    conn = get_db()
    conditions = ["started_at >= datetime('now', '-30 days')"]
    params = []

    if role:
        conditions.append("role = ?")
        params.append(role)

    where = " AND ".join(conditions)

    rows = conn.execute(
        f"""SELECT role, prompt_version,
                   COUNT(*) as total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                   AVG(num_turns) as avg_turns,
                   AVG(duration_seconds) as avg_duration
            FROM agent_runs
            WHERE {where}
              AND prompt_version IS NOT NULL
            GROUP BY role, prompt_version
            ORDER BY role, prompt_version""",
        params,
    ).fetchall()
    conn.close()

    results = {}
    for r in rows:
        role_name = r["role"]
        if role_name not in results:
            results[role_name] = {}
        results[role_name][r["prompt_version"]] = {
            "total": r["total"],
            "successes": r["successes"],
            "success_rate": round(r["successes"] / max(r["total"], 1), 3),
            "avg_turns": round(r["avg_turns"] or 0, 1),
            "avg_duration": round(r["avg_duration"] or 0, 0),
        }

    return results


# ============================================================
# MAIN ENTRY POINT (called from forgesmith.py pipeline)
# ============================================================

def ensure_prompt_version_column():
    """Add prompt_version column to agent_runs if it doesn't exist.

    Safety net — column is added by migration 3 (db_migrate.py).
    This function provides defense-in-depth for standalone ForgeSmith runs.
    """
    conn = get_db(write=True)
    try:
        conn.execute(
            "SELECT prompt_version FROM agent_runs LIMIT 1"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN prompt_version TEXT DEFAULT NULL"
        )
        conn.commit()
        log("Added prompt_version column to agent_runs table")
    finally:
        conn.close()


def run_gepa(cfg, dry_run=False, role_filter=None, run_id=None):
    """Run the full GEPA prompt evolution pipeline.

    Steps:
    1. Ensure DB schema is ready (prompt_version column)
    2. For each role with sufficient episodes, run GEPA optimization
    3. Validate and store evolved prompts with version stamps
    4. Check A/B test results and rollback underperformers

    Returns dict with results per role.
    """
    if dspy is None:
        log("DSPy not installed — cannot run GEPA. Install with: pip install dspy")
        return {"error": "dspy_not_installed", "roles_evolved": 0}

    gepa_cfg = cfg.get("gepa", {})
    if not gepa_cfg.get("enabled", True):
        log("GEPA disabled in config")
        return {"disabled": True, "roles_evolved": 0}

    lookback = gepa_cfg.get("lookback_days", 60)
    target_roles = gepa_cfg.get("target_roles", ["developer"])

    results = {
        "roles_analyzed": 0,
        "roles_evolved": 0,
        "rollbacks": 0,
        "details": {},
    }

    # Ensure DB schema supports A/B testing
    if not dry_run:
        ensure_prompt_version_column()

    # Determine which roles to optimize
    if role_filter:
        roles = [role_filter]
    else:
        roles = target_roles

    # Check rate limit: max 1 GEPA evolution per role per week
    conn = get_db()
    recent_evolutions = conn.execute(
        """SELECT DISTINCT target_file FROM forgesmith_changes
           WHERE change_type = 'gepa_evolution'
             AND created_at >= datetime('now', '-7 days')
             AND reverted_at IS NULL"""
    ).fetchall()
    conn.close()
    recently_evolved = set()
    for row in recent_evolutions:
        fname = Path(row["target_file"]).stem if row["target_file"] else ""
        recently_evolved.add(fname)

    for role in roles:
        if role in recently_evolved and not role_filter:
            log(f"Skipping {role} — already evolved this week")
            continue

        log(f"Collecting episodes for {role}...")
        episodes = collect_episodes_for_role(role, lookback_days=lookback)

        if len(episodes) < MIN_EPISODES_FOR_GEPA:
            log(f"Skipping {role}: only {len(episodes)} episodes "
                f"(need {MIN_EPISODES_FOR_GEPA})")
            results["details"][role] = {
                "skipped": True,
                "reason": f"insufficient_episodes ({len(episodes)}/"
                          f"{MIN_EPISODES_FOR_GEPA})",
            }
            continue

        results["roles_analyzed"] += 1
        log(f"Running GEPA for {role} with {len(episodes)} episodes...")

        gepa_result = run_gepa_for_role(role, episodes, cfg, dry_run=dry_run)

        if not gepa_result:
            results["details"][role] = {"evolved": False, "reason": "gepa_failed"}
            continue

        if gepa_result.get("dry_run"):
            results["details"][role] = {
                "evolved": False,
                "dry_run": True,
                "train_size": gepa_result["train_size"],
                "val_size": gepa_result["val_size"],
            }
            continue

        # Store the evolved prompt
        store_result = store_evolved_prompt(
            gepa_result, run_id or "gepa-standalone", cfg, dry_run=dry_run
        )
        history_check = gepa_result.get("episode_history_check", {})
        results["roles_evolved"] += 1
        results["details"][role] = {
            "evolved": True,
            "version": store_result["version"],
            "file": store_result["file"],
            "diff_ratio": store_result["diff_ratio"],
            "episode_penalty_applied": history_check.get("penalize", False),
            "matching_failed_episodes": history_check.get(
                "matching_episodes", 0
            ),
        }

    # Check A/B test results and rollback underperformers
    if not dry_run:
        log("Checking A/B test results for rollback...")
        for role in roles:
            current_version = get_current_prompt_version(role)
            if current_version > 1 and _should_rollback(role, current_version):
                rollback_evolved_prompt(role, current_version)
                results["rollbacks"] += 1
                if role in results["details"]:
                    results["details"][role]["rolled_back"] = True

    # Summary
    log(f"GEPA complete: {results['roles_analyzed']} roles analyzed, "
        f"{results['roles_evolved']} evolved, "
        f"{results['rollbacks']} rolled back")

    return results


# ============================================================
# STANDALONE CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="ForgeSmith GEPA — Automatic prompt evolution using DSPy")
    parser.add_argument("--role", metavar="ROLE",
                        help="Only evolve a specific role (default: developer)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--status", action="store_true",
                        help="Show A/B test status for all roles")
    parser.add_argument("--rollback", metavar="ROLE",
                        help="Rollback evolved prompt for a role")
    parser.add_argument("--lookback", type=int, default=60,
                        help="Days of history to analyze (default: 60)")

    args = parser.parse_args()

    if args.status:
        log("A/B Test Status")
        log("=" * 60)
        ensure_prompt_version_column()
        status = get_ab_test_status()
        if status:
            for role, versions in sorted(status.items()):
                print(f"\n{role}:")
                for version, stats in sorted(versions.items()):
                    rate = stats["success_rate"]
                    print(f"  {version}: {stats['total']} runs, "
                          f"{rate:.0%} success, "
                          f"avg {stats['avg_turns']:.0f} turns")
        else:
            print("No A/B test data yet. Run GEPA first, then wait for tasks.")
        return

    if args.rollback:
        role = args.rollback
        version = get_current_prompt_version(role)
        if version <= 1:
            log(f"No evolved prompt to rollback for {role}")
        else:
            rollback_evolved_prompt(role, version)
            log(f"Rolled back {role} from v{version} to baseline")
        return

    cfg = {
        "gepa": {
            "enabled": True,
            "lookback_days": args.lookback,
            "target_roles": [args.role] if args.role else ["developer"],
        }
    }

    results = run_gepa(cfg, dry_run=args.dry_run, role_filter=args.role)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()

