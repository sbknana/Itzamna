#!/usr/bin/env python3
"""ForgeSmith — Self-learning agent tuning system for EQUIPA.

Runs nightly (or on-demand) to analyze agent telemetry and make targeted
improvements to prompts, config, and blocked task resolution.

Pipeline: COLLECT → ANALYZE → DECIDE → APPLY → SIMBA → PROPOSE → EVOLVE → LOG

Usage:
    python3 forgesmith.py --auto          # Full run: analyze + apply changes
    python3 forgesmith.py --dry-run       # Show proposed changes without applying
    python3 forgesmith.py --report        # JSON analysis report only
    python3 forgesmith.py --rollback RUN  # Revert all changes from a specific run
    python3 forgesmith.py --propose       # Run OPRO proposal step only
    python3 forgesmith.py --simba         # Run SIMBA rule generation only
    python3 forgesmith.py --gepa          # Run GEPA prompt evolution only
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from equipa.config import is_feature_enabled
from forgesmith_gepa import run_gepa

# forgesmith_simba and forgesmith_impact were moved to scripts/ during
# repo cleanup.  Add the scripts directory to sys.path so imports resolve.
_scripts_dir = str(Path(__file__).resolve().parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from forgesmith_simba import run_simba
from forgesmith_impact import (
    run_impact_analysis,
    log_impact_assessment,
    ensure_impact_assessment_column,
)
from forgesmith_litm import run_litm_audit
from lesson_sanitizer import (
    sanitize_lesson_content,
    sanitize_error_signature,
    validate_lesson_structure,
)

# --- Paths ---

SCRIPT_DIR = Path(__file__).resolve().parent
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path(__file__).resolve().parent / "theforge.db"),
)
DISPATCH_CONFIG = SCRIPT_DIR / "dispatch_config.json"
PROMPTS_DIR = SCRIPT_DIR / "prompts"
CONFIG_FILE = SCRIPT_DIR / "forgesmith_config.json"

# --- Load Config ---

def load_config():
    """Load forgesmith_config.json with defaults."""
    defaults = {
        "lookback_days": 7,
        "min_sample_size": 5,
        "max_changes_per_run": 5,
        "max_prompt_patches_per_run": 2,
        "blocked_task_max_hours": 24,
        "blocked_task_max_attempts": 3,
        "thresholds": {
            "max_turns_hit_rate": 0.30,
            "turn_underuse_rate": 0.40,
            "simple_task_success_rate": 0.80,
            "output_compliance_rate": 0.80,
            "repeat_error_count": 3,
        },
        "limits": {
            "max_turns_ceiling": 75,
            "max_turns_floor": 10,
            "turn_increase_step": 10,
            "turn_decrease_step": 5,
            "max_concurrency": 8,
            "allowed_models": ["sonnet", "opus"],
        },
        "protected_files": [
            "_common.md",
            "forge_orchestrator.py",
            "forgesmith.py",
            "forgesmith_config.json",
        ],
        "backup_dir": ".forgesmith-backups",
        "max_backups": 30,
        "rollback_threshold": -0.1,
        "suppression_cooldown_days": 14,
        "forgesmith_project_id": 28,
        "rubric_definitions": {
            "developer": {
                "result_success": 5,
                "files_changed": 3,
                "tests_written": 3,
                "turns_efficiency": 2,
                "output_compliance": 2,
                "dependency_direction": 2,
                "separation_of_concerns": 2,
                "error_handling": 2,
                "naming_consistency": 2,
            },
            "tester": {
                "tests_pass": 5,
                "edge_cases": 3,
                "coverage_meaningful": 2,
                "false_positives": -2,
            },
            "code-reviewer": {
                "issues_found": 3,
                "actionable_feedback": 2,
                "false_alarms": -1,
            },
            "security-reviewer": {
                "vulns_found": 3,
                "severity_accuracy": 2,
                "false_alarms": -1,
            },
            "integration-tester": {
                "tests_pass": 5,
                "edge_cases": 3,
                "coverage_meaningful": 2,
                "false_positives": -2,
            },
            "frontend-designer": {
                "result_success": 5,
                "files_changed": 3,
                "turns_efficiency": 2,
                "output_compliance": 2,
            },
            "researcher": {
                "result_success": 5,
                "output_compliance": 3,
                "actionable_feedback": 2,
            },
        },
        "rubric_evolution": {
            "max_weight_change_pct": 10,
            "min_sample_size": 10,
            "evolution_lookback_days": 30,
        },
        "rubric_version": 1,
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            user_cfg = json.load(f)
        # Merge (shallow for top-level, deep for nested dicts)
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(defaults.get(k), dict):
                defaults[k].update(v)
            else:
                defaults[k] = v
    return defaults


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
    print(f"[{ts}] {msg}")


# ============================================================
# PHASE 1: COLLECT — Gather telemetry from the lookback window
# ============================================================

def collect_agent_runs(cfg):
    """Fetch agent_runs from the lookback window."""
    lookback = cfg["lookback_days"]
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM agent_runs
           WHERE started_at >= datetime('now', ?)
           ORDER BY started_at DESC""",
        (f"-{lookback} days",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def collect_blocked_tasks(cfg):
    """Fetch tasks that have been blocked longer than the threshold.

    Uses the most recent agent_run completed_at as the "last touched" time.
    Falls back to tasks.created_at if no agent runs exist for the task.
    """
    max_hours = cfg["blocked_task_max_hours"]
    conn = get_db()
    rows = conn.execute(
        """SELECT t.id, t.title, t.project_id, t.status,
                  p.codename as project_name,
                  COALESCE(ar.last_attempt, t.created_at) as last_touched,
                  ROUND((julianday('now') - julianday(
                      COALESCE(ar.last_attempt, t.created_at)
                  )) * 24, 1) as hours_blocked
           FROM tasks t
           LEFT JOIN projects p ON t.project_id = p.id
           LEFT JOIN (
               SELECT task_id, MAX(completed_at) as last_attempt
               FROM agent_runs GROUP BY task_id
           ) ar ON ar.task_id = t.id
           WHERE t.status = 'blocked'
             AND COALESCE(ar.last_attempt, t.created_at) <= datetime('now', ?)
           ORDER BY last_touched ASC""",
        (f"-{max_hours} hours",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def collect_previous_changes(run_id=None):
    """Fetch ForgeSmith changes that haven't been evaluated yet."""
    conn = get_db()
    if run_id:
        rows = conn.execute(
            "SELECT * FROM forgesmith_changes WHERE run_id = ?", (run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM forgesmith_changes
               WHERE effectiveness_score IS NULL AND reverted_at IS NULL
               ORDER BY created_at DESC"""
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_suppressed_changes(cfg):
    """Get change signatures that scored <= 0 in the last N days.

    Returns a set of (change_type, key_signature) tuples that should NOT
    be re-applied. A change is suppressed if:
    - It scored <= 0 (ineffective or harmful)
    - It was applied within the lookback window
    - It hasn't been manually overridden (reverted_at IS NULL means it's
      still active — we suppress the pattern, not the specific change)

    This prevents the feedback loop where ForgeSmith keeps re-applying
    the same ineffective change every night.
    """
    lookback = cfg["lookback_days"]
    cooldown_days = cfg.get("suppression_cooldown_days", 14)
    conn = get_db()
    rows = conn.execute(
        """SELECT change_type, target_file, old_value, new_value,
                  effectiveness_score, rationale
           FROM forgesmith_changes
           WHERE effectiveness_score IS NOT NULL
             AND effectiveness_score <= 0
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC""",
        (f"-{cooldown_days} days",),
    ).fetchall()
    conn.close()

    suppressed = set()
    for r in rows:
        change_type = r["change_type"]
        if change_type == "prompt_patch":
            # For prompt patches, extract the error signature from the rationale
            # Rationale format: "Error 'some error...' seen 5 times for role 'dev'."
            # Normalize to match analyze_repeat_errors() output: lowercase, no trailing ...
            match = re.search(r"Error '(.+?)'", r["rationale"] or "")
            if match:
                err_text = match.group(1).rstrip(".").lower()[:100]
                sig = ("prompt_patch", err_text)
            else:
                sig = ("prompt_patch", r["new_value"][:100].lower())
        else:
            sig = (change_type, r["new_value"])
        suppressed.add(sig)

    return suppressed


def extract_lessons(runs, cfg):
    """Extract lessons from agent runs and persist to lessons_learned table.

    Analyzes successful vs failed runs to identify actionable patterns:
    - What do successful runs do differently from failed ones?
    - What error patterns keep recurring?
    - What role/project combinations consistently fail?
    """
    conn = get_db(write=True)
    lessons_added = 0

    # Group runs by error type to find patterns
    error_groups = {}
    for r in runs:
        if not r["success"] and r.get("error_summary"):
            sig = (r.get("error_summary", "")[:100].strip().lower())
            if sig not in error_groups:
                error_groups[sig] = {"count": 0, "roles": set(),
                                     "projects": set(), "error_type": r.get("error_type")}
            error_groups[sig]["count"] += 1
            error_groups[sig]["roles"].add(r["role"])
            if r.get("project_id"):
                error_groups[sig]["projects"].add(r["project_id"])

    for sig, info in error_groups.items():
        if info["count"] < 2:
            continue

        # Check if lesson already exists
        existing = conn.execute(
            "SELECT id, times_seen FROM lessons_learned WHERE error_signature = ?",
            (sig,)
        ).fetchone()

        if existing:
            # Update existing lesson's count
            conn.execute(
                """UPDATE lessons_learned
                   SET times_seen = times_seen + ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (info["count"], existing["id"]),
            )
        else:
            # Create new lesson based on error pattern
            lesson = _generate_lesson(sig, info)
            if lesson and validate_lesson_structure(lesson):
                # Sanitize the lesson content before storage
                lesson = sanitize_lesson_content(lesson)
                safe_sig = sanitize_error_signature(sig)
                cursor = conn.execute(
                    """INSERT INTO lessons_learned
                       (role, error_type, error_signature, lesson, times_seen)
                       VALUES (?, ?, ?, ?, ?)""",
                    (list(info["roles"])[0] if len(info["roles"]) == 1 else None,
                     info["error_type"], safe_sig, lesson, info["count"]),
                )
                lesson_id = cursor.lastrowid
                lessons_added += 1

                # Attempt to generate and store embedding (if vector_memory enabled)
                try:
                    if is_feature_enabled(cfg, "vector_memory"):
                        from equipa.embeddings import embed_and_store_lesson
                        embed_and_store_lesson(lesson_id, lesson, dispatch_config=cfg)
                except Exception:
                    # Ollama down or import failed — continue without embedding
                    pass

    conn.commit()
    conn.close()
    return lessons_added


def _generate_lesson(error_sig, info):
    """Generate an actionable lesson from an error pattern.

    The else branch embeds agent-controlled error_sig text into the lesson.
    We sanitize it to prevent prompt-injection content from flowing into
    future agent prompts via the lesson pipeline (PM-28).
    """
    sig_lower = error_sig.lower()

    if "max turns" in sig_lower:
        return (
            "Agents hitting max turns should: (1) plan before coding, "
            "(2) make fewer, larger edits, (3) stop after 3 failed approaches "
            "and report findings instead of looping."
        )
    elif "timed out" in sig_lower or "timeout" in sig_lower:
        return (
            "Tasks timing out should: (1) focus on core requirement only, "
            "(2) skip optional improvements, (3) avoid full test suites, "
            "(4) write partial progress if time is running low."
        )
    elif "permission" in sig_lower:
        return (
            "Permission errors usually mean wrong directory or file ownership. "
            "Check paths carefully before retrying."
        )
    elif "not found" in sig_lower or "no such file" in sig_lower:
        return (
            "File not found errors often mean the project structure has changed. "
            "List the directory first to find the correct path."
        )
    elif "syntax error" in sig_lower or "parse error" in sig_lower:
        return (
            "Syntax errors in generated code: read the existing file format first, "
            "match the style, and validate with a build before marking complete."
        )
    else:
        # Sanitize the agent-controlled error_sig before embedding (PM-28)
        safe_sig = sanitize_error_signature(error_sig)
        safe_roles = ', '.join(info['roles'])
        lesson = (
            f"Recurring error ({info['count']}x): {safe_sig}. "
            f"Affected roles: {safe_roles}. "
            f"Try a fundamentally different approach if previous attempts failed."
        )
        return lesson


def get_relevant_lessons(role=None, error_type=None, limit=5):
    """Fetch active lessons relevant to a role or error type.

    Used by the orchestrator to inject lessons into agent prompts.
    """
    conn = get_db()
    conditions = ["active = 1"]
    params = []

    if role:
        conditions.append("(role = ? OR role IS NULL)")
        params.append(role)
    if error_type:
        conditions.append("error_type = ?")
        params.append(error_type)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"""SELECT id, lesson, error_signature, times_seen, effectiveness_score
            FROM lessons_learned
            WHERE {where}
            ORDER BY times_seen DESC, created_at DESC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# PHASE 2: ANALYZE — Detect patterns from the collected data
# ============================================================

def analyze_max_turns_hit(runs, cfg):
    """Detect roles hitting max_turns too often (>30% of runs)."""
    threshold = cfg["thresholds"]["max_turns_hit_rate"]
    min_samples = cfg["min_sample_size"]
    by_role = {}
    for r in runs:
        role = r["role"]
        if role not in by_role:
            by_role[role] = {"total": 0, "hit_max": 0}
        by_role[role]["total"] += 1
        if r["num_turns"] and r["max_turns_allowed"]:
            if r["num_turns"] >= r["max_turns_allowed"] - 1:
                by_role[role]["hit_max"] += 1

    findings = []
    for role, stats in by_role.items():
        if stats["total"] < min_samples:
            continue
        rate = stats["hit_max"] / stats["total"]
        if rate > threshold:
            findings.append({
                "pattern": "max_turns_hit",
                "role": role,
                "rate": round(rate, 2),
                "samples": stats["total"],
                "hit_count": stats["hit_max"],
            })
    return findings


def analyze_turn_underuse(runs, cfg):
    """Detect roles consistently underusing their turn budget (<40% utilization)."""
    threshold = cfg["thresholds"]["turn_underuse_rate"]
    min_samples = cfg["min_sample_size"]
    by_role = {}
    for r in runs:
        role = r["role"]
        if not r["num_turns"] or not r["max_turns_allowed"]:
            continue
        if role not in by_role:
            by_role[role] = {"total": 0, "utilizations": []}
        by_role[role]["total"] += 1
        util = r["num_turns"] / r["max_turns_allowed"]
        by_role[role]["utilizations"].append(util)

    findings = []
    for role, stats in by_role.items():
        if stats["total"] < min_samples:
            continue
        avg_util = sum(stats["utilizations"]) / len(stats["utilizations"])
        if avg_util < threshold:
            findings.append({
                "pattern": "turn_underuse",
                "role": role,
                "avg_utilization": round(avg_util, 2),
                "samples": stats["total"],
            })
    return findings


def analyze_model_downgrade(runs, cfg):
    """Detect simple tasks on opus that could use sonnet (>80% success rate)."""
    threshold = cfg["thresholds"]["simple_task_success_rate"]
    min_samples = cfg["min_sample_size"]
    simple_opus = [r for r in runs
                   if r["complexity"] == "simple" and r["model"] == "opus"]
    if len(simple_opus) < min_samples:
        return []

    success_count = sum(1 for r in simple_opus if r["success"])
    rate = success_count / len(simple_opus)
    if rate >= threshold:
        # Group by role to see which roles are candidates
        by_role = {}
        for r in simple_opus:
            role = r["role"]
            if role not in by_role:
                by_role[role] = {"total": 0, "success": 0}
            by_role[role]["total"] += 1
            if r["success"]:
                by_role[role]["success"] += 1

        findings = []
        for role, stats in by_role.items():
            if stats["total"] < min_samples:
                continue
            role_rate = stats["success"] / stats["total"]
            if role_rate >= threshold:
                findings.append({
                    "pattern": "model_downgrade",
                    "role": role,
                    "success_rate": round(role_rate, 2),
                    "samples": stats["total"],
                    "current_model": "opus",
                    "proposed_model": "sonnet",
                })
        return findings
    return []


def analyze_repeat_errors(runs, cfg):
    """Detect the same error_summary repeating 3+ times."""
    threshold = cfg["thresholds"]["repeat_error_count"]
    error_counts = {}
    for r in runs:
        if not r["error_summary"]:
            continue
        # Normalize: take first 200 chars as signature
        sig = r["error_summary"][:200].strip().lower()
        if sig not in error_counts:
            error_counts[sig] = {"count": 0, "roles": set(), "example": r["error_summary"]}
        error_counts[sig]["count"] += 1
        error_counts[sig]["roles"].add(r["role"])

    findings = []
    for sig, info in error_counts.items():
        if info["count"] >= threshold:
            findings.append({
                "pattern": "repeat_error",
                "error_signature": sig[:100],
                "count": info["count"],
                "roles": list(info["roles"]),
                "example": info["example"][:300],
            })
    return findings


def analyze_blocked_tasks(blocked_tasks, runs, cfg):
    """Analyze blocked tasks and decide resolution strategy."""
    max_attempts = cfg["blocked_task_max_attempts"]
    findings = []

    for task in blocked_tasks:
        task_id = task["id"]
        # Count how many times this task has been attempted
        task_runs = [r for r in runs if r["task_id"] == task_id]
        attempt_count = len(task_runs)

        # Check if the last failure was max_turns
        last_error = None
        if task_runs:
            last_run = sorted(task_runs, key=lambda r: r["started_at"] or "")[-1]
            last_error = last_run.get("error_type")

        if attempt_count >= max_attempts:
            # Too many attempts — escalate
            findings.append({
                "pattern": "blocked_escalate",
                "task_id": task_id,
                "title": task["title"],
                "project_id": task["project_id"],
                "project_name": task.get("project_name"),
                "hours_blocked": task["hours_blocked"],
                "attempts": attempt_count,
                "last_error": last_error,
            })
        else:
            # Reset to todo for another attempt
            findings.append({
                "pattern": "blocked_reset",
                "task_id": task_id,
                "title": task["title"],
                "project_id": task["project_id"],
                "project_name": task.get("project_name"),
                "hours_blocked": task["hours_blocked"],
                "attempts": attempt_count,
                "last_error": last_error,
            })

    return findings


def run_analysis(cfg):
    """Run all analysis passes and return combined findings."""
    log("PHASE 1: COLLECT")
    runs = collect_agent_runs(cfg)
    blocked = collect_blocked_tasks(cfg)
    log(f"  Collected {len(runs)} agent runs, {len(blocked)} blocked tasks")

    if not runs and not blocked:
        log("  No data to analyze.")
        return [], runs, blocked

    log("PHASE 2: ANALYZE")
    findings = []

    if runs:
        findings.extend(analyze_max_turns_hit(runs, cfg))
        findings.extend(analyze_turn_underuse(runs, cfg))
        findings.extend(analyze_model_downgrade(runs, cfg))
        findings.extend(analyze_repeat_errors(runs, cfg))

    if blocked:
        findings.extend(analyze_blocked_tasks(blocked, runs, cfg))

    log(f"  Found {len(findings)} actionable patterns")
    for f in findings:
        log(f"    - {f['pattern']}: {json.dumps({k: v for k, v in f.items() if k != 'pattern'}, default=str)}")

    return findings, runs, blocked


# ============================================================
# PHASE 3: DECIDE + APPLY — Make targeted changes
# ============================================================

def backup_file(filepath, cfg):
    """Create a timestamped backup of a file before modifying it."""
    backup_dir = SCRIPT_DIR / cfg["backup_dir"]
    backup_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{filepath.name}.{ts}.bak"
    backup_path = backup_dir / backup_name
    shutil.copy2(filepath, backup_path)

    # Clean old backups (keep last N)
    max_backups = cfg["max_backups"]
    backups = sorted(backup_dir.glob(f"{filepath.name}.*.bak"))
    while len(backups) > max_backups:
        backups[0].unlink()
        backups.pop(0)

    return backup_path


def apply_config_change(key, old_val, new_val, rationale, run_id, cfg, dry_run=False):
    """Modify a value in dispatch_config.json.

    Runs change-impact analysis before applying. HIGH-risk changes are
    blocked from auto-apply and logged for manual review.
    """
    target = str(DISPATCH_CONFIG)

    # Run impact analysis before applying
    assessment = run_impact_analysis(
        "config_tune", target, str(old_val), str(new_val), rationale=rationale
    )

    if dry_run:
        log(f"  [DRY-RUN] Would change {key}: {old_val} -> {new_val}")
        return {"change_type": "config_tune", "target_file": target,
                "old_value": str(old_val), "new_value": str(new_val),
                "rationale": rationale, "run_id": run_id,
                "impact_assessment": assessment}

    if assessment["blocked"]:
        log(f"  [BLOCKED] Config change {key}: {old_val} -> {new_val} "
            f"— HIGH risk, requires manual approval")
        # Record the blocked change in DB for review
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO forgesmith_changes
               (run_id, change_type, target_file, old_value, new_value,
                rationale, evidence, impact_assessment)
               VALUES (?, 'config_tune', ?, ?, ?, ?, ?, ?)""",
            (run_id, target, str(old_val), str(new_val),
             f"[BLOCKED] {rationale}",
             f"ForgeSmith auto-tune at {datetime.now().isoformat()}",
             json.dumps(assessment, default=str)),
        )
        conn.commit()
        conn.close()
        return None

    # Backup
    backup_file(DISPATCH_CONFIG, cfg)

    # Load, modify, write
    with open(DISPATCH_CONFIG) as f:
        config = json.load(f)
    config[key] = new_val
    with open(DISPATCH_CONFIG, "w") as f:
        json.dump(config, f, indent=4)
        f.write("\n")

    log(f"  [APPLIED] {key}: {old_val} -> {new_val}")

    # Record change in DB with impact assessment
    conn = get_db(write=True)
    conn.execute(
        """INSERT INTO forgesmith_changes
           (run_id, change_type, target_file, old_value, new_value,
            rationale, evidence, impact_assessment)
           VALUES (?, 'config_tune', ?, ?, ?, ?, ?, ?)""",
        (run_id, target, str(old_val), str(new_val), rationale,
         f"ForgeSmith auto-tune at {datetime.now().isoformat()}",
         json.dumps(assessment, default=str)),
    )
    conn.commit()
    conn.close()

    return {"change_type": "config_tune", "target_file": target,
            "old_value": str(old_val), "new_value": str(new_val),
            "rationale": rationale, "run_id": run_id,
            "impact_assessment": assessment}


def apply_prompt_patch(role, patch_text, rationale, run_id, cfg, dry_run=False):
    """Append a ForgeSmith Tuning section to a role's prompt file.

    ONLY appends to the bottom. Never touches the main prompt body.
    Protected files (_common.md) are never modified.
    Runs change-impact analysis before applying. HIGH-risk changes are
    blocked from auto-apply and logged for manual review.
    """
    prompt_file = PROMPTS_DIR / f"{role}.md"
    if not prompt_file.exists():
        log(f"  [SKIP] Prompt file not found: {prompt_file}")
        return None

    if prompt_file.name in cfg["protected_files"]:
        log(f"  [SKIP] Protected file: {prompt_file.name}")
        return None

    target = str(prompt_file)

    # Run impact analysis before applying
    assessment = run_impact_analysis(
        "prompt_patch", target, "", patch_text, rationale=rationale
    )

    if dry_run:
        log(f"  [DRY-RUN] Would patch prompt for {role}: {patch_text[:80]}...")
        return {"change_type": "prompt_patch", "target_file": target,
                "old_value": "", "new_value": patch_text,
                "rationale": rationale, "run_id": run_id,
                "impact_assessment": assessment}

    if assessment["blocked"]:
        log(f"  [BLOCKED] Prompt patch for {role} — HIGH risk, "
            f"requires manual approval")
        # Record the blocked change in DB for review
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO forgesmith_changes
               (run_id, change_type, target_file, old_value, new_value,
                rationale, evidence, impact_assessment)
               VALUES (?, 'prompt_patch', ?, '', ?, ?, ?, ?)""",
            (run_id, target, patch_text,
             f"[BLOCKED] {rationale}",
             f"ForgeSmith prompt patch at {datetime.now().isoformat()}",
             json.dumps(assessment, default=str)),
        )
        conn.commit()
        conn.close()
        return None

    # Backup
    backup_file(prompt_file, cfg)

    # Read current content
    content = prompt_file.read_text(encoding="utf-8")

    # Check if ForgeSmith section already exists — append to it
    marker = "\n## ForgeSmith Tuning\n"
    if marker in content:
        # Check if this exact patch is already in the section (avoid duplicates)
        existing_section = content.split(marker)[1] if marker in content else ""
        # Use the first line of the patch as a dedup key (e.g. "**Turn Budget Management**")
        first_line = patch_text.split("\n")[0].strip()
        if first_line in existing_section:
            log(f"  [SKIP] Patch already exists for {role}: {first_line[:60]}")
            return None
        # Append to existing section
        content = content.rstrip() + f"\n\n{patch_text}\n"
    else:
        # Create new section
        content = content.rstrip() + f"\n\n## ForgeSmith Tuning\n\n{patch_text}\n"
    prompt_file.write_text(content, encoding="utf-8")

    log(f"  [APPLIED] Prompt patch for {role}")

    # Record change with impact assessment
    conn = get_db(write=True)
    conn.execute(
        """INSERT INTO forgesmith_changes
           (run_id, change_type, target_file, old_value, new_value,
            rationale, evidence, impact_assessment)
           VALUES (?, 'prompt_patch', ?, '', ?, ?, ?, ?)""",
        (run_id, target, patch_text, rationale,
         f"ForgeSmith prompt patch at {datetime.now().isoformat()}",
         json.dumps(assessment, default=str)),
    )
    conn.commit()
    conn.close()

    return {"change_type": "prompt_patch", "target_file": target,
            "old_value": "", "new_value": patch_text,
            "rationale": rationale, "run_id": run_id,
            "impact_assessment": assessment}


def apply_blocked_resolution(finding, run_id, cfg, dry_run=False):
    """Reset a blocked task to todo or escalate via open_questions."""
    task_id = finding["task_id"]
    pattern = finding["pattern"]

    if pattern == "blocked_reset":
        if dry_run:
            log(f"  [DRY-RUN] Would reset task #{task_id} from blocked -> todo")
            return {"change_type": "blocked_resolution", "target_file": "tasks",
                    "old_value": "blocked", "new_value": "todo",
                    "rationale": f"Task blocked {finding['hours_blocked']}h, "
                                 f"{finding['attempts']} attempts. Resetting for retry.",
                    "run_id": run_id}

        conn = get_db(write=True)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
        log(f"  [APPLIED] Task #{task_id} reset: blocked -> todo "
            f"(blocked {finding['hours_blocked']}h)")

        # Record change
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO forgesmith_changes
               (run_id, change_type, target_file, old_value, new_value, rationale, evidence)
               VALUES (?, 'blocked_resolution', 'tasks', 'blocked', 'todo', ?, ?)""",
            (run_id,
             f"Task #{task_id} '{finding['title']}' blocked {finding['hours_blocked']}h, "
             f"{finding['attempts']} prior attempts. Auto-reset for retry.",
             f"last_error={finding.get('last_error')}"),
        )
        conn.commit()
        conn.close()

        return {"change_type": "blocked_resolution", "target_file": "tasks",
                "old_value": "blocked", "new_value": "todo",
                "rationale": f"Auto-reset after {finding['hours_blocked']}h blocked",
                "run_id": run_id}

    elif pattern == "blocked_escalate":
        if dry_run:
            log(f"  [DRY-RUN] Would escalate task #{task_id} "
                f"({finding['attempts']} failed attempts)")
            return {"change_type": "blocked_resolution", "target_file": "open_questions",
                    "old_value": "", "new_value": "escalation",
                    "rationale": f"Task #{task_id} failed {finding['attempts']} times. "
                                 "Needs human review.",
                    "run_id": run_id}

        question = (
            f"Task #{task_id} '{finding['title']}' has failed {finding['attempts']} times "
            f"and been blocked for {finding['hours_blocked']}h. "
            f"Last error: {finding.get('last_error', 'unknown')}. "
            f"Needs human intervention or task redesign."
        )
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO open_questions (project_id, question, context)
               VALUES (?, ?, 'ForgeSmith auto-escalation')""",
            (finding["project_id"], question),
        )
        conn.commit()
        conn.close()
        log(f"  [APPLIED] Escalated task #{task_id} to open_questions "
            f"({finding['attempts']} failed attempts)")

        # Record change
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO forgesmith_changes
               (run_id, change_type, target_file, old_value, new_value, rationale, evidence)
               VALUES (?, 'blocked_resolution', 'open_questions', '', 'escalation', ?, ?)""",
            (run_id,
             f"Task #{task_id} failed {finding['attempts']} times over "
             f"{finding['hours_blocked']}h. Escalated for human review.",
             f"last_error={finding.get('last_error')}"),
        )
        conn.commit()
        conn.close()

        return {"change_type": "blocked_resolution", "target_file": "open_questions",
                "old_value": "", "new_value": "escalation",
                "rationale": f"Escalated after {finding['attempts']} failures",
                "run_id": run_id}


def apply_changes(findings, run_id, cfg, dry_run=False):
    """Convert findings into concrete changes. Respects limits and suppression.

    Key safety: checks get_suppressed_changes() before applying ANY change.
    If a change type + value combination previously scored <= 0, it is
    skipped with a log message. This prevents the feedback loop where
    ForgeSmith re-applies the same ineffective changes nightly.
    """
    changes = []
    max_changes = cfg["max_changes_per_run"]
    max_prompts = cfg["max_prompt_patches_per_run"]
    prompt_count = 0
    limits = cfg["limits"]

    # Load suppression list — changes that scored <= 0 recently
    suppressed = get_suppressed_changes(cfg)
    if suppressed:
        log(f"  Suppression list: {len(suppressed)} previously ineffective changes")

    # Load current dispatch config for reference
    with open(DISPATCH_CONFIG) as f:
        dispatch = json.load(f)

    for finding in findings:
        if len(changes) >= max_changes:
            log(f"  Hit max changes limit ({max_changes}). Stopping.")
            break

        pattern = finding["pattern"]

        if pattern == "max_turns_hit":
            role = finding["role"]
            key = f"max_turns_{role.replace('-', '_')}"
            current = dispatch.get(key, 25)
            proposed = min(current + limits["turn_increase_step"],
                           limits["max_turns_ceiling"])
            if proposed > current:
                # Check suppression
                sig = ("config_tune", str(proposed))
                if sig in suppressed:
                    log(f"  [SUPPRESSED] max_turns {current}->{proposed} for {role} "
                        f"(previously scored <= 0)")
                    continue
                rationale = (
                    f"Role '{role}' hitting max turns in {finding['rate']*100:.0f}% "
                    f"of {finding['samples']} runs. Increasing {current} -> {proposed}."
                )
                change = apply_config_change(
                    key, current, proposed, rationale, run_id, cfg, dry_run)
                if change:
                    changes.append(change)
                    dispatch[key] = proposed  # Update local copy

        elif pattern == "turn_underuse":
            role = finding["role"]
            key = f"max_turns_{role.replace('-', '_')}"
            current = dispatch.get(key, 25)
            proposed = max(current - limits["turn_decrease_step"],
                           limits["max_turns_floor"])
            if proposed < current:
                sig = ("config_tune", str(proposed))
                if sig in suppressed:
                    log(f"  [SUPPRESSED] turn decrease {current}->{proposed} for {role} "
                        f"(previously scored <= 0)")
                    continue
                rationale = (
                    f"Role '{role}' only using {finding['avg_utilization']*100:.0f}% "
                    f"of turn budget across {finding['samples']} runs. "
                    f"Decreasing {current} -> {proposed}."
                )
                change = apply_config_change(
                    key, current, proposed, rationale, run_id, cfg, dry_run)
                if change:
                    changes.append(change)
                    dispatch[key] = proposed

        elif pattern == "model_downgrade":
            if finding["proposed_model"] not in limits["allowed_models"]:
                continue
            role = finding["role"]
            key = f"model_{role.replace('-', '_')}"
            current = dispatch.get(key, "opus")
            if current == "opus":
                # Check suppression — model downgrades that scored <= 0
                sig = ("config_tune", "sonnet")
                if sig in suppressed:
                    log(f"  [SUPPRESSED] model downgrade opus->sonnet for {role} "
                        f"(previously scored <= 0, cooldown active)")
                    continue
                rationale = (
                    f"Role '{role}' succeeds {finding['success_rate']*100:.0f}% "
                    f"on simple tasks with opus ({finding['samples']} runs). "
                    f"Switching simple tasks to sonnet for cost savings."
                )
                # We use model_simple as the key for simple-task model override
                change = apply_config_change(
                    "model_simple", current, "sonnet", rationale, run_id, cfg, dry_run)
                if change:
                    changes.append(change)

        elif pattern == "repeat_error":
            if prompt_count >= max_prompts:
                continue
            roles = finding["roles"]
            error_sig = finding["error_signature"]

            # Check if prompt patches for this error signature were ineffective
            sig = ("prompt_patch", error_sig)
            if sig in suppressed:
                log(f"  [SUPPRESSED] prompt patch for '{error_sig[:50]}...' "
                    f"(previously scored <= 0)")
                continue

            # Build more actionable prompt patches (not just "watch out")
            patch = _build_error_patch(error_sig, finding["count"])

            for role in roles:
                if prompt_count >= max_prompts:
                    break
                rationale = (
                    f"Error '{error_sig[:60]}...' seen {finding['count']} times "
                    f"for role '{role}'. Adding guidance to prompt."
                )
                change = apply_prompt_patch(
                    role, patch, rationale, run_id, cfg, dry_run)
                if change:
                    changes.append(change)
                    prompt_count += 1

        elif pattern in ("blocked_reset", "blocked_escalate"):
            change = apply_blocked_resolution(finding, run_id, cfg, dry_run)
            if change:
                changes.append(change)

    return changes


def _build_error_patch(error_sig, count):
    """Build actionable prompt patches based on error signature.

    Instead of generic "watch out for this error", provide specific
    guidance based on the error type.
    """
    sig_lower = error_sig.lower()

    if "max turns limit" in sig_lower:
        return (
            "**Turn Budget Management** (auto-tuned):\n"
            "You have a limited turn budget. To use it effectively:\n"
            "1. Read and understand ALL relevant files BEFORE making changes\n"
            "2. Create a brief plan (3-5 steps) before writing any code\n"
            "3. Make changes in large, complete chunks — not tiny incremental edits\n"
            "4. If you've tried 3 different approaches and none worked, STOP and "
            "report what you tried, what failed, and your best theory on root cause\n"
            "5. Do NOT retry the same failing approach — try something fundamentally different\n"
            "6. Combine related file reads into fewer turns (read multiple files at once)"
        )
    elif "timed out" in sig_lower or "timeout" in sig_lower:
        return (
            "**Time Management** (auto-tuned):\n"
            "Tasks have a time limit. To complete within it:\n"
            "1. Focus on the specific task — do NOT refactor surrounding code\n"
            "2. Skip optional improvements (comments, formatting, extra tests)\n"
            "3. If a build/test takes too long, check if you can run a subset\n"
            "4. If stuck on a complex issue for more than 10 turns, write a summary "
            "of what you've tried and stop — partial progress is better than timeout\n"
            "5. Do NOT install large dependencies or run full test suites unless required"
        )
    elif "permission denied" in sig_lower:
        return (
            "**Permission Errors** (auto-tuned):\n"
            "If you hit permission errors, do NOT repeatedly retry with sudo.\n"
            "Instead: check file ownership, check if the path is correct, "
            "and verify you're in the right directory."
        )
    else:
        return (
            f"**Recurring Issue Alert** (seen {count}x, auto-tuned):\n"
            f"This error has occurred multiple times: `{error_sig}`\n"
            f"When you encounter this:\n"
            f"1. Do NOT retry the same approach\n"
            f"2. Analyze WHY it's failing before attempting a fix\n"
            f"3. If you can't resolve it in 3 attempts, stop and report"
        )


# ============================================================
# PHASE 4: EVALUATE PREVIOUS — Score past changes
# ============================================================

def _get_rubric_score_delta(runs, change_time):
    """Compare average rubric normalized scores before vs after a change.

    Returns (delta, count_after) where delta is avg_after - avg_before.
    Positive delta means rubric scores improved after the change.
    Returns (None, 0) if insufficient data.
    """
    run_ids_before = [r["id"] for r in runs if (r["started_at"] or "") <= change_time]
    run_ids_after = [r["id"] for r in runs if (r["started_at"] or "") > change_time]

    if len(run_ids_after) < 3:
        return None, 0

    conn = get_db()
    # Fetch rubric scores for before/after runs
    all_ids = run_ids_before + run_ids_after
    placeholders = ",".join("?" for _ in all_ids)
    rows = conn.execute(
        f"""SELECT agent_run_id, normalized_score
            FROM rubric_scores
            WHERE agent_run_id IN ({placeholders})""",
        all_ids,
    ).fetchall()
    conn.close()

    scores_by_id = {row["agent_run_id"]: row["normalized_score"] for row in rows}

    before_scores = [scores_by_id[rid] for rid in run_ids_before if rid in scores_by_id]
    after_scores = [scores_by_id[rid] for rid in run_ids_after if rid in scores_by_id]

    if len(after_scores) < 3:
        return None, 0

    avg_before = sum(before_scores) / max(len(before_scores), 1) if before_scores else 0.0
    avg_after = sum(after_scores) / len(after_scores)

    return round(avg_after - avg_before, 3), len(after_scores)


def evaluate_previous_changes(runs, cfg):
    """Score previous ForgeSmith changes based on outcomes since they were made.

    Uses a blended scoring approach:
    - Binary success rate comparison (before vs after)
    - Rubric normalized score comparison (before vs after)
    - Change-type-specific heuristics (error rates, task progress)

    The final score is a weighted blend: 60% rubric delta + 40% heuristic score.
    Falls back to heuristic-only when rubric data is insufficient.
    """
    prev_changes = collect_previous_changes()
    if not prev_changes:
        return []

    evaluated = []
    rollback_threshold = cfg["rollback_threshold"]

    for change in prev_changes:
        change_time = change["created_at"]
        change_type = change["change_type"]

        # Compute rubric score delta (before vs after this change)
        rubric_delta, rubric_count = _get_rubric_score_delta(runs, change_time)

        if change_type == "config_tune":
            # Compare success rate before vs after the change
            runs_after = [r for r in runs if (r["started_at"] or "") > change_time]
            if len(runs_after) < 3:
                continue  # Not enough data yet

            success_after = sum(1 for r in runs_after if r["success"]) / len(runs_after)
            all_success = sum(1 for r in runs if r["success"]) / max(len(runs), 1)
            heuristic_score = round(success_after - all_success, 2)

        elif change_type == "prompt_patch":
            # Check if the error that triggered this patch still recurs.
            runs_after = [r for r in runs if (r["started_at"] or "") > change_time]
            if len(runs_after) < 3:
                continue  # Not enough data yet

            # Extract error text from rationale
            rationale = change.get("rationale", "")
            error_match = re.search(r"Error '(.+?)'", rationale)
            error_text = error_match.group(1) if error_match else ""

            if error_text:
                # Count how many runs after the patch still have this error
                error_runs_after = [
                    r for r in runs_after
                    if r.get("error_summary")
                    and error_text[:50].lower() in (r["error_summary"] or "")[:200].lower()
                ]
                error_rate_after = len(error_runs_after) / len(runs_after)

                # Compare to error rate in all runs (including before)
                all_error_runs = [
                    r for r in runs
                    if r.get("error_summary")
                    and error_text[:50].lower() in (r["error_summary"] or "")[:200].lower()
                ]
                error_rate_all = len(all_error_runs) / max(len(runs), 1)

                if error_rate_all > 0:
                    heuristic_score = round(error_rate_all - error_rate_after, 2)
                else:
                    heuristic_score = 0.0
            else:
                success_after = sum(1 for r in runs_after if r["success"]) / len(runs_after)
                all_success = sum(1 for r in runs if r["success"]) / max(len(runs), 1)
                heuristic_score = round(success_after - all_success, 2)

        elif change_type == "blocked_resolution":
            # Extract task_id from the rationale string
            rationale = change.get("rationale", "")
            task_match = re.search(r"Task #(\d+)", rationale)
            if task_match and change["new_value"] == "todo":
                task_id = int(task_match.group(1))
                conn = get_db()
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                conn.close()
                if row:
                    status = row["status"]
                    if status in ("done", "in_progress"):
                        heuristic_score = 0.5
                    elif status == "blocked":
                        heuristic_score = -0.2
                    else:
                        heuristic_score = 0.0
                else:
                    heuristic_score = 0.0
            else:
                heuristic_score = 0.0

        else:
            heuristic_score = 0.0

        # Blend rubric delta with heuristic score
        # Rubric delta provides a more nuanced view than binary success
        if rubric_delta is not None and rubric_count >= 3:
            # 60% rubric delta, 40% heuristic
            score = round(0.6 * rubric_delta + 0.4 * heuristic_score, 3)
        else:
            score = heuristic_score

        # Update effectiveness score
        conn = get_db(write=True)
        conn.execute(
            "UPDATE forgesmith_changes SET effectiveness_score = ? WHERE id = ?",
            (score, change["id"]),
        )
        conn.commit()
        conn.close()

        evaluated.append({"change_id": change["id"], "score": score,
                          "change_type": change_type,
                          "rubric_delta": rubric_delta,
                          "heuristic_score": heuristic_score})

        # Auto-rollback if score is very negative (config_tune and prompt_patch)
        if score < rollback_threshold and change_type in ("config_tune", "prompt_patch"):
            log(f"  [ROLLBACK] Change #{change['id']} scored {score} — reverting")
            rollback_change(change)

    return evaluated


def rollback_change(change):
    """Revert a single ForgeSmith change."""
    if change["change_type"] == "config_tune":
        target = Path(change["target_file"])
        if target.exists():
            with open(target) as f:
                config = json.load(f)
            # Find the key by comparing old/new values
            for key, val in config.items():
                if str(val) == change["new_value"]:
                    config[key] = json.loads(change["old_value"]) if change["old_value"].isdigit() else change["old_value"]
                    try:
                        config[key] = int(change["old_value"])
                    except (ValueError, TypeError):
                        config[key] = change["old_value"]
                    break
            with open(target, "w") as f:
                json.dump(config, f, indent=4)
                f.write("\n")

    elif change["change_type"] == "prompt_patch":
        target = Path(change["target_file"])
        if target.exists():
            content = target.read_text(encoding="utf-8")
            marker = "\n## ForgeSmith Tuning\n"
            if marker in content:
                content = content.split(marker)[0].rstrip() + "\n"
                target.write_text(content, encoding="utf-8")

    # Mark as reverted
    conn = get_db(write=True)
    conn.execute(
        "UPDATE forgesmith_changes SET reverted_at = datetime('now') WHERE id = ?",
        (change["id"],),
    )
    conn.commit()
    conn.close()
    log(f"  [ROLLBACK] Reverted change #{change['id']}: {change['change_type']}")


# ============================================================
# PHASE 4.5: RUBRIC SCORING — Structured per-role evaluation
# ============================================================

def ensure_rubric_scores_table():
    """Create rubric_scores table if it doesn't exist. Idempotent."""
    conn = get_db(write=True)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rubric_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id INTEGER NOT NULL,
            task_id INTEGER,
            project_id INTEGER,
            role TEXT NOT NULL,
            rubric_version INTEGER DEFAULT 1,
            criteria_scores TEXT NOT NULL,
            total_score REAL NOT NULL,
            max_possible REAL NOT NULL,
            normalized_score REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
        )
    """)
    conn.commit()
    conn.close()


def _get_agent_run_output(agent_run_id):
    """Try to get agent output text from checkpoint files.

    Agents save checkpoint files in .forge-checkpoints/. We look for
    the most recent one matching the task_id.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT task_id, role FROM agent_runs WHERE id = ?", (agent_run_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None

    task_id = row["task_id"]
    checkpoint_dir = SCRIPT_DIR / ".forge-checkpoints"
    if not checkpoint_dir.exists():
        return None

    # Look for checkpoint files matching this task
    candidates = list(checkpoint_dir.glob(f"task_{task_id}_*.txt"))
    if not candidates:
        return None

    # Return the most recent one
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].read_text(encoding="utf-8", errors="replace")


def _parse_result_block(text):
    """Parse the RESULT/SUMMARY/FILES_CHANGED/BLOCKERS/DECISIONS block from agent output.

    Returns a dict with parsed fields.
    """
    parsed = {
        "result": None,
        "summary": None,
        "files_changed": [],
        "blockers": None,
        "decisions": None,
        "reflection": None,
    }
    if not text:
        return parsed

    lines = text.splitlines()
    current_section = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("RESULT:"):
            parsed["result"] = stripped.split(":", 1)[1].strip().lower()
            current_section = None
        elif stripped.startswith("SUMMARY:"):
            parsed["summary"] = stripped.split(":", 1)[1].strip()
            current_section = None
        elif stripped.startswith("BLOCKERS:"):
            val = stripped.split(":", 1)[1].strip()
            parsed["blockers"] = val if val.lower() != "none" else None
            current_section = None
        elif stripped.startswith("DECISIONS:"):
            parsed["decisions"] = stripped.split(":", 1)[1].strip()
            current_section = None
        elif stripped.startswith("REFLECTION:"):
            parsed["reflection"] = stripped.split(":", 1)[1].strip()
            current_section = None
        elif stripped.startswith("FILES_CHANGED:"):
            current_section = "files_changed"
            val = stripped.split(":", 1)[1].strip()
            if val and val.lower() != "none":
                parsed["files_changed"].append(val)
        elif current_section == "files_changed":
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                if item.lower() != "none":
                    parsed["files_changed"].append(item)
            elif stripped and not stripped.startswith("-"):
                current_section = None

    return parsed


def _score_developer(run, parsed_output, cfg):
    """Score a developer run against the developer rubric.

    Criteria:
    - result_success: Did the agent report RESULT: success? (binary)
    - files_changed: Did the agent actually change files? (binary)
    - tests_written: Did the agent write/modify test files? (heuristic)
    - turns_efficiency: How efficiently did it use its turn budget? (gradient)
    - output_compliance: Did it produce the structured output block? (binary)
    """
    rubric = cfg.get("rubric_definitions", {}).get("developer", {})
    scores = {}

    # result_success: RESULT: success in output OR success=1 in agent_runs
    weight = rubric.get("result_success", 5)
    if run["success"] or (parsed_output["result"] == "success"):
        scores["result_success"] = weight
    else:
        scores["result_success"] = 0

    # files_changed: agent produced FILES_CHANGED with actual file names
    weight = rubric.get("files_changed", 3)
    has_files = bool(parsed_output["files_changed"]) or (run.get("files_changed_count") or 0) > 0
    scores["files_changed"] = weight if has_files else 0

    # tests_written: any test file in FILES_CHANGED list
    weight = rubric.get("tests_written", 3)
    test_files = [f for f in parsed_output["files_changed"]
                  if "test" in f.lower() or "spec" in f.lower()]
    scores["tests_written"] = weight if test_files else 0

    # turns_efficiency: ratio of turns used vs max allowed
    # Best score if 30-80% of budget used. Penalty for >95% or <20%
    weight = rubric.get("turns_efficiency", 2)
    if run["num_turns"] and run["max_turns_allowed"] and run["max_turns_allowed"] > 0:
        utilization = run["num_turns"] / run["max_turns_allowed"]
        if 0.3 <= utilization <= 0.8:
            scores["turns_efficiency"] = weight  # Sweet spot
        elif utilization < 0.2:
            scores["turns_efficiency"] = round(weight * 0.5, 1)  # Too few turns — maybe skipped work
        elif utilization > 0.95:
            scores["turns_efficiency"] = 0  # Hit the ceiling — bad planning
        else:
            scores["turns_efficiency"] = round(weight * 0.75, 1)  # Acceptable
    else:
        scores["turns_efficiency"] = 0

    # output_compliance: has RESULT: block
    weight = rubric.get("output_compliance", 2)
    scores["output_compliance"] = weight if parsed_output["result"] is not None else 0

    return scores


def _score_tester(run, parsed_output, cfg):
    """Score a tester/integration-tester run.

    Criteria:
    - tests_pass: Did the tests pass? (binary from outcome)
    - edge_cases: Were multiple test scenarios covered? (heuristic: test count)
    - coverage_meaningful: Did it test real behavior vs trivial assertions?
    - false_positives: Penalty if tester reported failures that were wrong
    """
    role_key = "integration-tester" if run["role"] == "integration-tester" else "tester"
    rubric = cfg.get("rubric_definitions", {}).get(role_key, {})
    scores = {}

    # tests_pass: outcome indicates tests passed
    weight = rubric.get("tests_pass", 5)
    if run["outcome"] == "tests_passed":
        scores["tests_pass"] = weight
    elif run["success"]:
        scores["tests_pass"] = round(weight * 0.5, 1)
    else:
        scores["tests_pass"] = 0

    # edge_cases: look for test count indicators in output
    weight = rubric.get("edge_cases", 3)
    # Look for "X tests" or "TESTS_RUN:" patterns in output
    test_count = 0
    if parsed_output.get("summary"):
        count_match = re.search(r"(\d+)\s*tests?", parsed_output["summary"] or "")
        if count_match:
            test_count = int(count_match.group(1))
    if test_count >= 5:
        scores["edge_cases"] = weight
    elif test_count >= 2:
        scores["edge_cases"] = round(weight * 0.5, 1)
    else:
        scores["edge_cases"] = 0

    # coverage_meaningful: heuristic — did it produce files and have a real framework
    weight = rubric.get("coverage_meaningful", 2)
    has_files = bool(parsed_output["files_changed"]) or (run.get("files_changed_count") or 0) > 0
    scores["coverage_meaningful"] = weight if has_files else 0

    # false_positives: penalty if tester reported failure but task later succeeded
    weight = rubric.get("false_positives", -2)
    # This is a penalty (negative weight). Apply if tester reported fail but
    # outcome was actually fine (developer fixed it trivially).
    # For now, only apply if tester blocked but task eventually completed.
    if run["outcome"] in ("tester_blocked",) and not run["success"]:
        # Check if task eventually succeeded
        conn = get_db()
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (run["task_id"],)
        ).fetchone()
        conn.close()
        if row and row["status"] == "done":
            scores["false_positives"] = weight  # Negative penalty
        else:
            scores["false_positives"] = 0
    else:
        scores["false_positives"] = 0

    return scores


def _score_reviewer(run, parsed_output, cfg):
    """Score a code-reviewer or security-reviewer run.

    Criteria:
    - issues_found / vulns_found: Did it find actionable issues?
    - actionable_feedback / severity_accuracy: Quality of findings
    - false_alarms: Penalty for non-issues reported as issues
    """
    role = run["role"]
    rubric = cfg.get("rubric_definitions", {}).get(role, {})
    scores = {}

    is_security = role == "security-reviewer"

    # issues_found / vulns_found: heuristic from output text
    weight = rubric.get("vulns_found" if is_security else "issues_found", 3)
    # Look for finding patterns in output summary/text
    summary = (parsed_output.get("summary") or "").lower()
    if any(word in summary for word in ("found", "identified", "detected", "vulnerability", "issue")):
        scores["vulns_found" if is_security else "issues_found"] = weight
    elif run["success"]:
        scores["vulns_found" if is_security else "issues_found"] = round(weight * 0.5, 1)
    else:
        scores["vulns_found" if is_security else "issues_found"] = 0

    # actionable_feedback / severity_accuracy: output quality
    weight = rubric.get("severity_accuracy" if is_security else "actionable_feedback", 2)
    # Heuristic: has output compliance + non-trivial summary
    has_output = parsed_output["result"] is not None
    has_summary = bool(parsed_output.get("summary"))
    if has_output and has_summary:
        scores["severity_accuracy" if is_security else "actionable_feedback"] = weight
    elif has_output:
        scores["severity_accuracy" if is_security else "actionable_feedback"] = round(weight * 0.5, 1)
    else:
        scores["severity_accuracy" if is_security else "actionable_feedback"] = 0

    # false_alarms: penalty (negative weight)
    weight = rubric.get("false_alarms", -1)
    # Only penalize if reviewer reported RESULT: failed but task succeeded anyway
    if parsed_output["result"] == "failed":
        conn = get_db()
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (run["task_id"],)
        ).fetchone()
        conn.close()
        if row and row["status"] == "done":
            scores["false_alarms"] = weight  # Penalty
        else:
            scores["false_alarms"] = 0
    else:
        scores["false_alarms"] = 0

    return scores


def _score_generic(run, parsed_output, cfg):
    """Score any other role with basic criteria."""
    role = run["role"]
    rubric = cfg.get("rubric_definitions", {}).get(role, {})
    if not rubric:
        # Fallback rubric for unknown roles
        rubric = {"result_success": 5, "output_compliance": 3, "turns_efficiency": 2}

    scores = {}

    if "result_success" in rubric:
        weight = rubric["result_success"]
        scores["result_success"] = weight if run["success"] else 0

    if "output_compliance" in rubric:
        weight = rubric["output_compliance"]
        scores["output_compliance"] = weight if parsed_output["result"] is not None else 0

    if "actionable_feedback" in rubric:
        weight = rubric["actionable_feedback"]
        has_summary = bool(parsed_output.get("summary"))
        scores["actionable_feedback"] = weight if has_summary else 0

    if "files_changed" in rubric:
        weight = rubric["files_changed"]
        has_files = bool(parsed_output["files_changed"]) or (run.get("files_changed_count") or 0) > 0
        scores["files_changed"] = weight if has_files else 0

    if "turns_efficiency" in rubric:
        weight = rubric["turns_efficiency"]
        if run["num_turns"] and run["max_turns_allowed"] and run["max_turns_allowed"] > 0:
            utilization = run["num_turns"] / run["max_turns_allowed"]
            if 0.3 <= utilization <= 0.8:
                scores["turns_efficiency"] = weight
            elif utilization > 0.95:
                scores["turns_efficiency"] = 0
            else:
                scores["turns_efficiency"] = round(weight * 0.5, 1)
        else:
            scores["turns_efficiency"] = 0

    return scores


def compute_rubric_score(run, cfg):
    """Compute rubric score for a single agent_run.

    Parses agent output from checkpoint files or episode data, then
    applies the appropriate role-specific rubric.

    Returns (criteria_scores, total_score, max_possible, normalized_score)
    or None if scoring isn't possible.
    """
    role = run["role"]

    # Try to get parsed output from checkpoint files
    output_text = _get_agent_run_output(run["id"])

    # Also try agent_episodes for approach_summary
    conn = get_db()
    episode = conn.execute(
        """SELECT approach_summary, outcome FROM agent_episodes
           WHERE task_id = ? AND role = ?
           ORDER BY id DESC LIMIT 1""",
        (run["task_id"], role),
    ).fetchone()
    conn.close()

    parsed = _parse_result_block(output_text)

    # Enrich parsed output with episode data if available
    if episode and not parsed["summary"]:
        parsed["summary"] = episode["approach_summary"]

    # Use the outcome from agent_runs as the result if not parsed from text
    if parsed["result"] is None:
        outcome = run["outcome"]
        if outcome in ("tests_passed", "no_tests"):
            parsed["result"] = "success"
        elif outcome in ("developer_failed", "developer_blocked", "tester_blocked"):
            parsed["result"] = "blocked"
        else:
            parsed["result"] = "failed"

    # Route to role-specific scorer
    if role == "developer":
        scores = _score_developer(run, parsed, cfg)
    elif role in ("tester", "integration-tester"):
        scores = _score_tester(run, parsed, cfg)
    elif role in ("code-reviewer", "security-reviewer"):
        scores = _score_reviewer(run, parsed, cfg)
    else:
        scores = _score_generic(run, parsed, cfg)

    if not scores:
        return None

    # Compute totals
    total = sum(scores.values())
    # Max possible = sum of all positive weights in the rubric
    rubric = cfg.get("rubric_definitions", {}).get(role, {})
    if not rubric:
        rubric = {"result_success": 5, "output_compliance": 3, "turns_efficiency": 2}
    max_possible = sum(w for w in rubric.values() if w > 0)
    normalized = round(min(total / max_possible, 1.0), 3) if max_possible > 0 else 0.0

    return scores, round(total, 1), max_possible, round(normalized, 3)


def score_completed_runs(runs, cfg):
    """Score all completed agent_runs that haven't been scored yet.

    Called during the nightly ForgeSmith pipeline. Only scores runs
    that don't already have a rubric_scores entry.

    Returns list of (agent_run_id, normalized_score) tuples.
    """
    ensure_rubric_scores_table()
    rubric_version = cfg.get("rubric_version", 1)

    conn = get_db()
    scored_ids = set(
        row["agent_run_id"] for row in conn.execute(
            "SELECT agent_run_id FROM rubric_scores"
        ).fetchall()
    )
    conn.close()

    results = []
    for run in runs:
        if run["id"] in scored_ids:
            continue

        result = compute_rubric_score(run, cfg)
        if result is None:
            continue

        scores, total, max_possible, normalized = result

        # Store in DB with rubric version for tracking weight evolution
        conn = get_db(write=True)
        conn.execute(
            """INSERT INTO rubric_scores
               (agent_run_id, task_id, project_id, role, rubric_version,
                criteria_scores, total_score, max_possible, normalized_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run["id"], run["task_id"], run.get("project_id"), run["role"],
             rubric_version, json.dumps(scores), total, max_possible, normalized),
        )
        conn.commit()
        conn.close()

        results.append((run["id"], normalized))

    return results


def analyze_rubric_correlations(cfg):
    """Analyze which rubric criteria correlate with task success.

    Compares criteria scores between successful and failed runs to
    identify which criteria are most predictive of success.

    Returns a dict: {role: {criterion: correlation_score, ...}}
    """
    evolution_cfg = cfg.get("rubric_evolution", {})
    min_samples = evolution_cfg.get("min_sample_size", 10)
    lookback_days = evolution_cfg.get("evolution_lookback_days", 30)

    conn = get_db()
    rows = conn.execute(
        """SELECT rs.role, rs.criteria_scores, ar.success
           FROM rubric_scores rs
           JOIN agent_runs ar ON ar.id = rs.agent_run_id
           WHERE rs.created_at >= datetime('now', ?)""",
        (f"-{lookback_days} days",),
    ).fetchall()
    conn.close()

    if not rows:
        return {}

    # Group by role
    by_role = {}
    for row in rows:
        role = row["role"]
        if role not in by_role:
            by_role[role] = {"success": [], "failure": []}
        scores = json.loads(row["criteria_scores"])
        if row["success"]:
            by_role[role]["success"].append(scores)
        else:
            by_role[role]["failure"].append(scores)

    correlations = {}
    for role, groups in by_role.items():
        if len(groups["success"]) < min_samples or len(groups["failure"]) < min_samples:
            continue

        correlations[role] = {}

        # For each criterion, compare avg score in success vs failure
        all_criteria = set()
        for scores in groups["success"] + groups["failure"]:
            all_criteria.update(scores.keys())

        for criterion in all_criteria:
            success_vals = [s.get(criterion, 0) for s in groups["success"]]
            failure_vals = [s.get(criterion, 0) for s in groups["failure"]]

            avg_success = sum(success_vals) / len(success_vals)
            avg_failure = sum(failure_vals) / len(failure_vals)

            # Correlation = difference in means (positive = criterion predicts success)
            diff = round(avg_success - avg_failure, 3)
            correlations[role][criterion] = diff

    return correlations


def ensure_rubric_evolution_table():
    """Create rubric_evolution_history table if it doesn't exist. Idempotent."""
    conn = get_db(write=True)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rubric_evolution_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rubric_version INTEGER NOT NULL,
            role TEXT NOT NULL,
            criterion TEXT NOT NULL,
            old_weight REAL NOT NULL,
            new_weight REAL NOT NULL,
            correlation REAL NOT NULL,
            sample_size_success INTEGER,
            sample_size_failure INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def evolve_rubric_weights(cfg):
    """Evolve rubric weights based on correlation analysis.

    Increases weight of criteria that correlate with success,
    decreases weight of criteria that don't differentiate.
    Max change per evolution: 10% of current weight.

    Records all weight changes in rubric_evolution_history for auditing.

    Returns dict of changes made: {role: {criterion: (old, new)}}
    """
    correlations = analyze_rubric_correlations(cfg)
    if not correlations:
        return {}

    evolution_cfg = cfg.get("rubric_evolution", {})
    max_change_pct = evolution_cfg.get("max_weight_change_pct", 10) / 100.0

    rubrics = cfg.get("rubric_definitions", {})
    changes = {}

    # Get sample sizes for recording in history
    sample_sizes = _get_correlation_sample_sizes(cfg)

    for role, criteria_correlations in correlations.items():
        if role not in rubrics:
            continue

        rubric = rubrics[role]
        role_changes = {}

        for criterion, correlation in criteria_correlations.items():
            if criterion not in rubric:
                continue

            current_weight = rubric[criterion]

            # Skip negative weights (penalties) — don't evolve those
            if current_weight < 0:
                continue

            # Determine direction: positive correlation = increase weight
            if abs(correlation) < 0.1:
                continue  # Not significant enough to change

            max_delta = max(abs(current_weight) * max_change_pct, 0.1)

            if correlation > 0:
                # Criterion predicts success — increase weight
                delta = min(correlation, max_delta)
                new_weight = round(current_weight + delta, 1)
            else:
                # Criterion doesn't help — decrease weight
                delta = min(abs(correlation), max_delta)
                new_weight = round(max(current_weight - delta, 0.5), 1)

            if new_weight != current_weight:
                role_changes[criterion] = (current_weight, new_weight, correlation)
                rubric[criterion] = new_weight

        if role_changes:
            changes[role] = {k: (old, new) for k, (old, new, _) in role_changes.items()}

    # Persist evolved weights to config file and record history
    if changes:
        # Bump rubric version
        rubric_version = cfg.get("rubric_version", 1) + 1
        cfg["rubric_version"] = rubric_version

        # Write updated config
        config_data = {}
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                config_data = json.load(f)
        config_data["rubric_definitions"] = rubrics
        config_data["rubric_version"] = rubric_version
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_data, f, indent=4)
            f.write("\n")

        # Record evolution history in DB
        ensure_rubric_evolution_table()
        conn = get_db(write=True)
        for role, criteria_correlations in correlations.items():
            if role not in changes:
                continue
            for criterion in changes[role]:
                old_w, new_w = changes[role][criterion]
                corr = criteria_correlations.get(criterion, 0.0)
                sizes = sample_sizes.get(role, {})
                conn.execute(
                    """INSERT INTO rubric_evolution_history
                       (rubric_version, role, criterion, old_weight, new_weight,
                        correlation, sample_size_success, sample_size_failure)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rubric_version, role, criterion, old_w, new_w, corr,
                     sizes.get("success", 0), sizes.get("failure", 0)),
                )
        conn.commit()
        conn.close()

    return changes


def _get_correlation_sample_sizes(cfg):
    """Get sample sizes per role for correlation analysis (for auditing).

    Returns {role: {"success": N, "failure": N}}
    """
    evolution_cfg = cfg.get("rubric_evolution", {})
    lookback_days = evolution_cfg.get("evolution_lookback_days", 30)

    conn = get_db()
    rows = conn.execute(
        """SELECT rs.role, ar.success, COUNT(*) as cnt
           FROM rubric_scores rs
           JOIN agent_runs ar ON ar.id = rs.agent_run_id
           WHERE rs.created_at >= datetime('now', ?)
           GROUP BY rs.role, ar.success""",
        (f"-{lookback_days} days",),
    ).fetchall()
    conn.close()

    sizes = {}
    for row in rows:
        role = row["role"]
        if role not in sizes:
            sizes[role] = {"success": 0, "failure": 0}
        if row["success"]:
            sizes[role]["success"] = row["cnt"]
        else:
            sizes[role]["failure"] = row["cnt"]

    return sizes


def get_rubric_report(role=None, limit=20):
    """Get rubric scores summary for reporting.

    Returns recent scores grouped by role.
    """
    conn = get_db()
    if role:
        rows = conn.execute(
            """SELECT rs.*, ar.outcome, ar.success
               FROM rubric_scores rs
               JOIN agent_runs ar ON ar.id = rs.agent_run_id
               WHERE rs.role = ?
               ORDER BY rs.created_at DESC LIMIT ?""",
            (role, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT rs.*, ar.outcome, ar.success
               FROM rubric_scores rs
               JOIN agent_runs ar ON ar.id = rs.agent_run_id
               ORDER BY rs.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# ============================================================
# PHASE 2.95: GHOST VERIFICATION — Verify resolved security findings
# ============================================================

GHOST_SKILL_PATH = SCRIPT_DIR / "skills" / "security-reviewer" / "skills" \
    / "ghost-verification" / "SKILL.md"

MAX_GHOST_VERIFICATIONS_PER_RUN = 5
GHOST_SCOUT_MODEL = "haiku"
GHOST_SCOUT_MAX_TURNS = 10
GHOST_SCOUT_TIMEOUT = 180  # seconds


def fetch_unverified_resolved_findings() -> list[dict]:
    """Fetch security findings that are resolved but not yet verified.

    Returns decisions where:
    - decision_type = 'security_finding'
    - status = 'resolved'
    - verified_at IS NULL
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT d.id, d.project_id, d.topic, d.decision, d.rationale,
                      d.alternatives_considered, d.resolved_by_task_id
               FROM decisions d
               WHERE d.decision_type = 'security_finding'
                 AND d.status = 'resolved'
                 AND d.verified_at IS NULL
               ORDER BY d.decided_at DESC
               LIMIT ?""",
            (MAX_GHOST_VERIFICATIONS_PER_RUN,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_ghost_skill_prompt() -> str | None:
    """Load the ghost-verification SKILL.md prompt template.

    Returns the prompt content (after the YAML frontmatter) or None if
    the file cannot be read.
    """
    try:
        text = GHOST_SKILL_PATH.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        log("  [GHOST] Could not read ghost-verification SKILL.md")
        return None

    # Strip YAML frontmatter (between --- delimiters)
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


def build_ghost_prompt(skill_prompt: str, finding: dict) -> str:
    """Build the full prompt for the ghost verification scout.

    Injects the finding description, code path, and resolution context
    into the skill template.
    """
    # Extract code path from rationale/alternatives (where agents typically
    # store file paths) or from the decision field itself
    code_path = finding.get("alternatives_considered", "") or ""
    finding_description = finding.get("topic", "")
    decision_text = finding.get("decision", "")
    rationale = finding.get("rationale", "")
    resolved_by = finding.get("resolved_by_task_id")

    context_block = (
        f"## Finding to Verify\n\n"
        f"**Finding ID:** {finding['id']}\n"
        f"**Finding description:** {finding_description}\n"
        f"**Decision/Resolution:** {decision_text}\n"
        f"**Rationale:** {rationale}\n"
        f"**Code path / files:** {code_path}\n"
    )
    if resolved_by:
        context_block += f"**Resolved by task:** #{resolved_by}\n"

    return f"{skill_prompt}\n\n{context_block}"


def dispatch_ghost_scout(prompt: str) -> str | None:
    """Dispatch the security-reviewer agent with the ghost verification
    skill context using the Haiku model for cost efficiency.

    Returns the agent's raw output text or None on failure.
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", GHOST_SCOUT_MODEL,
        "--max-turns", str(GHOST_SCOUT_MAX_TURNS),
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=GHOST_SCOUT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("  [GHOST] Scout timed out")
        return None
    except FileNotFoundError:
        log("  [GHOST] 'claude' command not found")
        return None

    if result.returncode != 0:
        log(f"  [GHOST] Scout returned exit code {result.returncode}")
        if result.stderr:
            log(f"  [GHOST] stderr: {result.stderr[:200]}")
        return None

    raw = result.stdout.strip()
    if not raw:
        log("  [GHOST] Empty response from scout")
        return None

    # Parse JSON wrapper from --output-format json
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
        return raw
    except json.JSONDecodeError:
        return raw


def parse_ghost_verdict(output: str) -> tuple[str | None, str | None, str | None]:
    """Parse the scout output for VERDICT, EVIDENCE, and SEVERITY.

    Returns (verdict, evidence, severity) where verdict is
    'VERIFIED' or 'STILL_PRESENT', or (None, None, None) on parse failure.
    """
    verdict_match = re.search(
        r"VERDICT:\s*(VERIFIED|STILL_PRESENT)", output, re.IGNORECASE
    )
    if not verdict_match:
        return None, None, None

    verdict = verdict_match.group(1).upper()

    evidence_match = re.search(
        r"EVIDENCE:\s*(.+?)(?:\n(?:SEVERITY:|$)|\Z)",
        output, re.DOTALL | re.IGNORECASE,
    )
    evidence = evidence_match.group(1).strip() if evidence_match else ""

    severity = None
    if verdict == "STILL_PRESENT":
        sev_match = re.search(
            r"SEVERITY:\s*(critical|high|medium|low)", output, re.IGNORECASE
        )
        severity = sev_match.group(1).lower() if sev_match else "high"

    return verdict, evidence, severity


def apply_ghost_verdict(
    finding: dict, verdict: str, evidence: str, severity: str | None
) -> dict | None:
    """Apply the ghost verification result to the database.

    - VERIFIED: sets verified_at = datetime('now')
    - STILL_PRESENT: sets status to 'failed_resolution' and creates a
      high-priority follow-up task.

    Returns a summary dict of the action taken, or None on error.
    """
    conn = get_db(write=True)
    try:
        if verdict == "VERIFIED":
            conn.execute(
                "UPDATE decisions SET verified_at = datetime('now') WHERE id = ?",
                (finding["id"],),
            )
            conn.commit()
            return {
                "finding_id": finding["id"],
                "verdict": "VERIFIED",
                "action": "marked_verified",
                "evidence": evidence,
            }

        elif verdict == "STILL_PRESENT":
            conn.execute(
                "UPDATE decisions SET status = 'failed_resolution' WHERE id = ?",
                (finding["id"],),
            )

            # Create a high-priority follow-up task
            task_title = (
                f"Ghost finding: {finding['topic'][:80]}"
            )
            task_desc = (
                f"Security finding #{finding['id']} was marked resolved but "
                f"ghost verification found it STILL PRESENT.\n\n"
                f"**Original finding:** {finding['topic']}\n"
                f"**Evidence:** {evidence}\n"
                f"**Severity:** {severity or 'high'}\n\n"
                f"Re-investigate and fix the vulnerability."
            )
            cursor = conn.execute(
                """INSERT INTO tasks
                   (project_id, title, description, status, priority)
                   VALUES (?, ?, ?, 'todo', 'high')""",
                (finding["project_id"], task_title, task_desc),
            )
            new_task_id = cursor.lastrowid
            conn.commit()

            return {
                "finding_id": finding["id"],
                "verdict": "STILL_PRESENT",
                "action": "failed_resolution_task_created",
                "new_task_id": new_task_id,
                "severity": severity,
                "evidence": evidence,
            }

        return None
    except sqlite3.Error as exc:
        log(f"  [GHOST] DB error applying verdict for finding #{finding['id']}: {exc}")
        return None
    finally:
        conn.close()


def run_ghost_verification(dry_run: bool = False) -> dict:
    """Run ghost finding verification for unverified resolved security findings.

    Pipeline:
    1. Fetch unverified resolved security findings (max 5)
    2. Load ghost-verification skill prompt
    3. For each finding: dispatch scout, parse verdict, apply result

    Returns a summary dict with counts and details.
    """
    result = {
        "findings_checked": 0,
        "verified": 0,
        "still_present": 0,
        "parse_failures": 0,
        "dispatch_failures": 0,
        "details": [],
    }

    # Step 1: Fetch candidates
    findings = fetch_unverified_resolved_findings()
    if not findings:
        log("  [GHOST] No unverified resolved security findings found")
        return result

    log(f"  [GHOST] Found {len(findings)} unverified resolved findings")

    # Step 2: Load skill prompt
    skill_prompt = load_ghost_skill_prompt()
    if not skill_prompt:
        log("  [GHOST] Cannot proceed without skill prompt")
        return result

    # Step 3: Verify each finding
    for finding in findings:
        finding_id = finding["id"]
        log(f"  [GHOST] Verifying finding #{finding_id}: "
            f"{finding['topic'][:60]}")

        if dry_run:
            result["findings_checked"] += 1
            result["details"].append({
                "finding_id": finding_id,
                "action": "dry_run_skipped",
            })
            continue

        # Build prompt and dispatch scout
        prompt = build_ghost_prompt(skill_prompt, finding)
        scout_output = dispatch_ghost_scout(prompt)

        if not scout_output:
            result["dispatch_failures"] += 1
            result["findings_checked"] += 1
            continue

        # Parse verdict
        verdict, evidence, severity = parse_ghost_verdict(scout_output)
        if not verdict:
            log(f"  [GHOST] Could not parse verdict for finding #{finding_id}")
            result["parse_failures"] += 1
            result["findings_checked"] += 1
            continue

        # Apply verdict
        action_result = apply_ghost_verdict(finding, verdict, evidence, severity)
        if action_result:
            result["details"].append(action_result)
            if verdict == "VERIFIED":
                result["verified"] += 1
                log(f"  [GHOST] Finding #{finding_id}: VERIFIED")
            else:
                result["still_present"] += 1
                log(f"  [GHOST] Finding #{finding_id}: STILL_PRESENT "
                    f"(task #{action_result.get('new_task_id')})")

        result["findings_checked"] += 1

    return result


# ============================================================
# PHASE 4.7: PROPOSE — OPRO-style LLM-driven prompt optimization
# ============================================================

def collect_role_metrics(runs, cfg):
    """Aggregate per-role metrics from the lookback window for OPRO context.

    Returns {role: {success_rate, avg_turns, max_turns_hit_rate,
                    total_runs, common_errors, avg_duration_s}}
    """
    by_role = {}
    for r in runs:
        role = r["role"]
        if role not in by_role:
            by_role[role] = {
                "total": 0, "success": 0, "turns": [], "durations": [],
                "max_turns_hits": 0, "errors": {},
            }
        info = by_role[role]
        info["total"] += 1
        if r["success"]:
            info["success"] += 1
        if r["num_turns"]:
            info["turns"].append(r["num_turns"])
        if r.get("duration_seconds"):
            info["durations"].append(r["duration_seconds"])
        if r["num_turns"] and r["max_turns_allowed"]:
            if r["num_turns"] >= r["max_turns_allowed"] - 1:
                info["max_turns_hits"] += 1
        if r.get("error_summary"):
            sig = r["error_summary"][:120].strip().lower()
            info["errors"][sig] = info["errors"].get(sig, 0) + 1

    metrics = {}
    for role, info in by_role.items():
        top_errors = sorted(info["errors"].items(), key=lambda x: -x[1])[:5]
        metrics[role] = {
            "total_runs": info["total"],
            "success_rate": round(info["success"] / max(info["total"], 1), 2),
            "avg_turns": round(sum(info["turns"]) / max(len(info["turns"]), 1), 1),
            "max_turns_hit_rate": round(
                info["max_turns_hits"] / max(info["total"], 1), 2
            ),
            "avg_duration_s": round(
                sum(info["durations"]) / max(len(info["durations"]), 1), 0
            ),
            "common_errors": [
                {"error": err, "count": cnt} for err, cnt in top_errors
            ],
        }
    return metrics


def collect_recent_reflections(cfg, role=None, limit=10):
    """Fetch recent agent episode reflections for OPRO context.

    Returns list of {role, task_type, outcome, reflection, turns_used}.
    """
    conn = get_db()
    lookback = cfg["lookback_days"]
    if role:
        rows = conn.execute(
            """SELECT role, task_type, outcome, reflection, turns_used
               FROM agent_episodes
               WHERE reflection IS NOT NULL
                 AND created_at >= datetime('now', ?)
                 AND role = ?
               ORDER BY created_at DESC LIMIT ?""",
            (f"-{lookback} days", role, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT role, task_type, outcome, reflection, turns_used
               FROM agent_episodes
               WHERE reflection IS NOT NULL
                 AND created_at >= datetime('now', ?)
               ORDER BY created_at DESC LIMIT ?""",
            (f"-{lookback} days", limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_opro_prompt(role, current_prompt, metrics, reflections, cfg):
    """Construct the OPRO meta-prompt for Claude to propose prompt modifications.

    Provides the current prompt, metrics context, and recent reflections,
    then asks for specific, testable modifications with rationale.
    """
    opro_cfg = cfg.get("opro", {})
    max_proposals = opro_cfg.get("max_proposals_per_role", 3)

    # Format metrics summary
    role_metrics = metrics.get(role, {})
    metrics_text = json.dumps(role_metrics, indent=2) if role_metrics else "No metrics available."

    # Format reflections
    role_reflections = [r for r in reflections if r["role"] == role]
    if role_reflections:
        reflections_text = "\n".join(
            f"- [{r['outcome']}] (turns: {r['turns_used']}) {r['reflection']}"
            for r in role_reflections[:7]
        )
    else:
        reflections_text = "No reflections available for this role."

    # Format lessons — sanitize before injection into meta-prompt (PM-28)
    lessons = get_relevant_lessons(role=role, limit=5)
    if lessons:
        lessons_text = "\n".join(
            f"- (seen {l['times_seen']}x) {sanitize_lesson_content(l['lesson'])}"
            for l in lessons
        )
    else:
        lessons_text = "No lessons available."

    prompt = f"""You are an expert prompt engineer optimizing agent system prompts for a multi-agent software development system called EQUIPA.

## Your Task
Analyze the current prompt for the "{role}" role and propose specific, testable modifications to improve agent performance based on the metrics and reflections provided.

## Current Prompt for "{role}"
```
{current_prompt}
```

## Performance Metrics (last {cfg['lookback_days']} days)
{metrics_text}

## Recent Agent Reflections
{reflections_text}

## Lessons Learned
{lessons_text}

## Rules for Proposals
1. Each proposal MUST be a specific, concrete change — not vague advice
2. Proposals must be TESTABLE: after applying, we can measure whether the metric improved
3. NEVER propose removing the "RESULT:" output block format, the "Output Requirements" section, or the "Output Format" section — these are critical for the orchestrator
4. NEVER propose removing git commit requirements
5. Focus on the biggest performance gaps shown in the metrics
6. Prefer adding guidance or restructuring existing text over removing sections
7. Keep proposals targeted — each should address ONE specific issue
8. Do NOT propose changes to sections marked "(auto-tuned)" — ForgeSmith manages those

## Output Format
Respond with ONLY a JSON array of proposals. Each proposal must have these fields:
- "target_section": Which section of the prompt to modify (e.g., "Workflow", "Developer Rules", "Handling Build Errors")
- "action": One of "add", "replace", "reorder" (never "delete")
- "old_text": The exact text being replaced (empty string for "add" actions)
- "new_text": The new/replacement text
- "rationale": Why this change should improve performance (reference specific metrics)
- "expected_improvement": Which metric this targets and by how much (e.g., "success_rate +5%")
- "confidence": "high", "medium", or "low"

Return at most {max_proposals} proposals, ordered by expected impact (highest first).
Respond with ONLY the JSON array, no other text."""

    return prompt


OPRO_PROTECTED_PATTERNS = [
    r"RESULT:\s*success\s*\|\s*blocked\s*\|\s*failed",
    r"SUMMARY:",
    r"FILES_CHANGED:",
    r"BLOCKERS:",
    r"Output Requirements",
    r"Output Format",
    r"git\s+add.*git\s+commit",
    r"Git Commit Requirements",
]


def validate_opro_proposal(proposal, current_prompt, cfg):
    """Validate an OPRO proposal for safety.

    Returns (is_valid, rejection_reason).
    Rejects proposals that:
    - Would delete protected sections (RESULT block, Output Requirements)
    - Target protected files
    - Have no concrete new_text
    - Are too vague to be testable
    """
    action = proposal.get("action", "")
    old_text = proposal.get("old_text", "")
    new_text = proposal.get("new_text", "")
    target = proposal.get("target_section", "")

    # Action must be one of the allowed types
    if action not in ("add", "replace", "reorder"):
        return False, f"Invalid action '{action}' — must be add, replace, or reorder"

    # new_text must be non-empty
    if not new_text or not new_text.strip():
        return False, "Proposal has empty new_text"

    # For replace actions, old_text must exist in the current prompt
    if action == "replace":
        if not old_text or not old_text.strip():
            return False, "Replace action requires non-empty old_text"
        if old_text.strip() not in current_prompt:
            return False, "old_text not found in current prompt — cannot apply"

    # Check that new_text doesn't remove protected patterns
    # For replace: ensure protected patterns in old_text are preserved in new_text
    if action == "replace" and old_text:
        for pattern in OPRO_PROTECTED_PATTERNS:
            if re.search(pattern, old_text, re.IGNORECASE | re.DOTALL):
                if not re.search(pattern, new_text, re.IGNORECASE | re.DOTALL):
                    return False, f"Proposal would remove protected pattern: {pattern}"

    # Reject proposals that are just vague advice (< 20 chars of actual content)
    content_lines = [l.strip() for l in new_text.split("\n") if l.strip()]
    if len(content_lines) < 1 or all(len(l) < 15 for l in content_lines):
        return False, "Proposal too vague — needs concrete, specific text"

    # Don't allow proposals targeting auto-tuned sections
    if "(auto-tuned)" in new_text and "(auto-tuned)" in old_text:
        pass  # Modifying auto-tuned content is OK if keeping the marker
    elif "(auto-tuned)" in (old_text or "") and "(auto-tuned)" not in new_text:
        return False, "Cannot remove auto-tuned sections — ForgeSmith manages those"

    return True, None


def call_claude_for_proposals(prompt, cfg):
    """Call Claude CLI to generate OPRO proposals.

    Uses subprocess to invoke `claude -p` with the OPRO prompt.
    Returns the parsed JSON response or None on failure.
    """
    opro_cfg = cfg.get("opro", {})
    model = opro_cfg.get("model", "sonnet")
    timeout = opro_cfg.get("timeout_seconds", 120)

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", "2",
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        log("  [OPRO] Claude call timed out")
        return None
    except FileNotFoundError:
        log("  [OPRO] 'claude' command not found — cannot generate proposals")
        return None

    if result.returncode != 0:
        log(f"  [OPRO] Claude returned non-zero exit code: {result.returncode}")
        if result.stderr:
            log(f"  [OPRO] stderr: {result.stderr[:200]}")
        return None

    raw = result.stdout.strip()
    if not raw:
        log("  [OPRO] Empty response from Claude")
        return None

    # Parse the JSON output — Claude's --output-format json wraps in {"result": "..."}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "result" in outer:
            inner = outer["result"]
        else:
            inner = raw
    except json.JSONDecodeError:
        inner = raw

    # The inner content should be a JSON array of proposals
    # Try to extract JSON array from the text (Claude may include markdown fences)
    if isinstance(inner, str):
        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", inner.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try:
            proposals = json.loads(cleaned)
        except json.JSONDecodeError:
            log(f"  [OPRO] Failed to parse proposals JSON: {cleaned[:200]}")
            return None
    elif isinstance(inner, list):
        proposals = inner
    else:
        log(f"  [OPRO] Unexpected response format: {type(inner)}")
        return None

    if not isinstance(proposals, list):
        log(f"  [OPRO] Expected list of proposals, got {type(proposals)}")
        return None

    return proposals


def generate_opro_proposals(runs, cfg, dry_run=False):
    """Run the OPRO proposal phase for all roles with sufficient data.

    For each role:
    1. Collect metrics and reflections
    2. Read current prompt
    3. Call Claude to propose modifications
    4. Validate proposals
    5. Store valid proposals in forgesmith_changes

    Rate limit: max 1 OPRO proposal per role per night.

    Returns list of proposal dicts.
    """
    opro_cfg = cfg.get("opro", {})
    if not opro_cfg.get("enabled", True):
        log("  [OPRO] Disabled in config")
        return []

    min_runs = opro_cfg.get("min_runs_for_proposal", 5)
    min_improvement = opro_cfg.get("min_predicted_improvement", 0.10)

    # Collect metrics and reflections
    metrics = collect_role_metrics(runs, cfg)
    reflections = collect_recent_reflections(cfg)

    # Check which roles already had an OPRO proposal today (rate limit)
    conn = get_db()
    today_proposals = conn.execute(
        """SELECT DISTINCT target_file FROM forgesmith_changes
           WHERE change_type = 'opro_proposal'
             AND created_at >= date('now')"""
    ).fetchall()
    conn.close()
    already_proposed = set()
    for row in today_proposals:
        # Extract role from target_file path (e.g., .../prompts/developer.md -> developer)
        fname = Path(row["target_file"]).stem if row["target_file"] else ""
        already_proposed.add(fname)

    all_proposals = []

    for role, role_metrics in metrics.items():
        # Rate limit: 1 proposal per role per day
        if role in already_proposed:
            log(f"  [OPRO] Skipping {role} — already proposed today")
            continue

        # Minimum sample size
        if role_metrics["total_runs"] < min_runs:
            log(f"  [OPRO] Skipping {role} — only {role_metrics['total_runs']} runs "
                f"(need {min_runs})")
            continue

        # Read current prompt
        prompt_file = PROMPTS_DIR / f"{role}.md"
        if not prompt_file.exists():
            continue
        if prompt_file.name in cfg["protected_files"]:
            continue

        current_prompt = prompt_file.read_text(encoding="utf-8")

        # Build the OPRO meta-prompt
        opro_prompt = build_opro_prompt(
            role, current_prompt, metrics, reflections, cfg
        )

        log(f"  [OPRO] Generating proposals for role: {role} "
            f"(success={role_metrics['success_rate']:.0%}, "
            f"runs={role_metrics['total_runs']})")

        if dry_run:
            log(f"  [OPRO/DRY-RUN] Would call Claude for {role} proposals")
            all_proposals.append({
                "role": role, "proposals": [], "dry_run": True,
                "metrics": role_metrics,
            })
            continue

        # Call Claude
        raw_proposals = call_claude_for_proposals(opro_prompt, cfg)
        if not raw_proposals:
            log(f"  [OPRO] No proposals returned for {role}")
            continue

        # Validate each proposal
        valid_proposals = []
        for i, prop in enumerate(raw_proposals):
            is_valid, reason = validate_opro_proposal(prop, current_prompt, cfg)
            if is_valid:
                valid_proposals.append(prop)
                log(f"  [OPRO] Proposal {i+1} for {role}: VALID — "
                    f"{prop.get('target_section', '?')}: "
                    f"{prop.get('rationale', '')[:80]}")
            else:
                log(f"  [OPRO] Proposal {i+1} for {role}: REJECTED — {reason}")

        if valid_proposals:
            all_proposals.append({
                "role": role, "proposals": valid_proposals,
                "metrics": role_metrics,
            })

    return all_proposals


def apply_opro_proposals(proposals, run_id, cfg, dry_run=False):
    """Store OPRO proposals in forgesmith_changes and optionally apply the best one.

    In auto mode: applies the top-confidence proposal for each role
    if the predicted improvement exceeds the minimum threshold.
    In dry-run mode: stores proposals for review without applying.

    Returns list of applied changes.
    """
    opro_cfg = cfg.get("opro", {})
    min_improvement = opro_cfg.get("min_predicted_improvement", 0.10)
    changes = []

    for role_result in proposals:
        role = role_result["role"]
        role_proposals = role_result.get("proposals", [])

        if not role_proposals:
            continue

        prompt_file = PROMPTS_DIR / f"{role}.md"
        if not prompt_file.exists():
            continue

        current_prompt = prompt_file.read_text(encoding="utf-8")

        # Store ALL valid proposals in DB for tracking
        for prop in role_proposals:
            evidence = json.dumps({
                "metrics": role_result.get("metrics", {}),
                "target_section": prop.get("target_section", ""),
                "action": prop.get("action", ""),
                "expected_improvement": prop.get("expected_improvement", ""),
                "confidence": prop.get("confidence", "low"),
            })
            rationale = (
                f"OPRO proposal for {role}: {prop.get('rationale', 'No rationale')}"
            )
            new_value = json.dumps({
                "action": prop.get("action"),
                "old_text": prop.get("old_text", ""),
                "new_text": prop.get("new_text", ""),
                "target_section": prop.get("target_section", ""),
            })

            if not dry_run:
                conn = get_db(write=True)
                conn.execute(
                    """INSERT INTO forgesmith_changes
                       (run_id, change_type, target_file, old_value, new_value,
                        rationale, evidence)
                       VALUES (?, 'opro_proposal', ?, ?, ?, ?, ?)""",
                    (run_id, str(prompt_file),
                     prop.get("old_text", "")[:500],
                     new_value, rationale, evidence),
                )
                conn.commit()
                conn.close()

        # In auto mode, apply the top-confidence proposal
        if not dry_run:
            # Sort by confidence: high > medium > low
            confidence_order = {"high": 3, "medium": 2, "low": 1}
            sorted_props = sorted(
                role_proposals,
                key=lambda p: confidence_order.get(p.get("confidence", "low"), 0),
                reverse=True,
            )

            best = sorted_props[0]
            confidence = best.get("confidence", "low")

            # Parse expected improvement percentage
            expected = best.get("expected_improvement", "")
            improvement_pct = _parse_improvement_pct(expected)

            if confidence in ("high", "medium") and improvement_pct >= min_improvement:
                applied = _apply_single_opro_proposal(
                    best, role, prompt_file, current_prompt, run_id, cfg
                )
                if applied:
                    changes.append(applied)
                    log(f"  [OPRO] Applied top proposal for {role}: "
                        f"{best.get('target_section', '?')} "
                        f"(confidence={confidence}, expected={expected})")
            else:
                log(f"  [OPRO] Top proposal for {role} not applied — "
                    f"confidence={confidence}, expected_improvement={expected} "
                    f"(threshold={min_improvement:.0%})")
        else:
            for prop in role_proposals:
                log(f"  [OPRO/DRY-RUN] {role} -> {prop.get('target_section', '?')}: "
                    f"{prop.get('rationale', '')[:100]}")

    return changes


def _parse_improvement_pct(expected_str):
    """Parse an expected improvement string like 'success_rate +5%' into a float (0.05).

    Returns 0.0 if parsing fails.
    """
    if not expected_str:
        return 0.0
    match = re.search(r'[+-]?\s*(\d+(?:\.\d+)?)\s*%', expected_str)
    if match:
        return float(match.group(1)) / 100.0
    return 0.0


def _apply_single_opro_proposal(proposal, role, prompt_file, current_prompt, run_id, cfg):
    """Apply a single validated OPRO proposal to a prompt file.

    Returns the change dict if successful, None otherwise.
    """
    action = proposal.get("action", "")
    old_text = proposal.get("old_text", "")
    new_text = proposal.get("new_text", "")

    # Backup first
    backup_file(prompt_file, cfg)

    if action == "add":
        # Append to the end, before any ForgeSmith Tuning section
        marker = "\n## ForgeSmith Tuning\n"
        if marker in current_prompt:
            parts = current_prompt.split(marker)
            updated = parts[0].rstrip() + f"\n\n{new_text}\n" + marker + parts[1]
        else:
            updated = current_prompt.rstrip() + f"\n\n{new_text}\n"

    elif action == "replace":
        if old_text not in current_prompt:
            log(f"  [OPRO] Cannot apply replace — old_text not found in {role}.md")
            return None
        updated = current_prompt.replace(old_text, new_text, 1)

    elif action == "reorder":
        # For reorder, new_text contains the full reordered section
        # old_text is the section to replace
        if old_text not in current_prompt:
            log(f"  [OPRO] Cannot apply reorder — old_text not found in {role}.md")
            return None
        updated = current_prompt.replace(old_text, new_text, 1)

    else:
        return None

    # Final safety check: ensure protected patterns still exist
    for pattern in OPRO_PROTECTED_PATTERNS:
        if re.search(pattern, current_prompt, re.IGNORECASE | re.DOTALL):
            if not re.search(pattern, updated, re.IGNORECASE | re.DOTALL):
                log(f"  [OPRO] SAFETY: Proposal would remove protected pattern "
                    f"'{pattern}' — aborting")
                return None

    # Write the updated prompt
    prompt_file.write_text(updated, encoding="utf-8")
    log(f"  [OPRO] Applied {action} to {prompt_file.name}")

    return {
        "change_type": "opro_applied",
        "target_file": str(prompt_file),
        "old_value": old_text[:500] if old_text else "",
        "new_value": new_text[:500],
        "rationale": f"OPRO {action} for {role}: {proposal.get('rationale', '')[:200]}",
        "run_id": run_id,
    }


def run_propose_only(cfg, dry_run=False):
    """Run ONLY the OPRO proposal step (for --propose CLI flag).

    Collects data, generates proposals, and optionally applies them.
    Does NOT run the standard analysis/apply pipeline.
    """
    mode = "dry_run" if dry_run else "propose"
    run_id = f"fs-opro-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    log(f"ForgeSmith OPRO run {run_id} ({mode})")
    log(f"{'='*60}")

    # Collect runs for metrics
    log("PHASE 1: COLLECT (for OPRO)")
    runs = collect_agent_runs(cfg)
    log(f"  Collected {len(runs)} agent runs")

    if not runs:
        log("  No data to analyze. Nothing to propose.")
        return

    # Generate proposals
    log("\nPHASE 4.7: PROPOSE (OPRO)")
    proposals = generate_opro_proposals(runs, cfg, dry_run=dry_run)

    if not proposals:
        log("  No proposals generated.")
        if not dry_run:
            log_run(run_id, len(runs), [], "OPRO: no proposals generated.", mode, cfg)
        return

    # Apply proposals
    changes = apply_opro_proposals(proposals, run_id, cfg, dry_run=dry_run)

    # Summary
    total_proposals = sum(len(r.get("proposals", [])) for r in proposals)
    log(f"\n  Generated {total_proposals} proposals for "
        f"{len(proposals)} roles, applied {len(changes)}")

    if not dry_run:
        summary = (f"OPRO: {total_proposals} proposals for {len(proposals)} roles, "
                   f"{len(changes)} applied")
        log_run(run_id, len(runs), changes, summary, mode, cfg)

    log(f"\n{'='*60}")
    log(f"ForgeSmith OPRO run complete: {run_id}")


# ============================================================
# PHASE 5: LOG — Record the run and decisions
# ============================================================

def log_run(run_id, runs_analyzed, changes, summary, mode, cfg):
    """Record the ForgeSmith run in the database."""
    conn = get_db(write=True)
    conn.execute(
        """INSERT INTO forgesmith_runs
           (run_id, completed_at, agent_runs_analyzed, changes_made, summary, mode)
           VALUES (?, datetime('now'), ?, ?, ?, ?)""",
        (run_id, runs_analyzed, len(changes), summary, mode),
    )
    conn.commit()

    # Also log as a decision in TheForge
    project_id = cfg["forgesmith_project_id"]
    if changes:
        change_summary = "; ".join(
            f"{c['change_type']}: {c.get('rationale', '')[:100]}" for c in changes
        )
        conn.execute(
            """INSERT INTO decisions
               (project_id, topic, decision, rationale, decision_type, status)
               VALUES (?, 'forgesmith-auto', ?, ?, 'general', 'open')""",
            (project_id,
             f"ForgeSmith run {run_id}: {len(changes)} changes applied",
             change_summary[:1000]),
        )
        conn.commit()

    conn.close()


# ============================================================
# MAIN ENTRY POINTS
# ============================================================

def run_report(cfg):
    """Generate a JSON analysis report without applying changes."""
    findings, runs, blocked = run_analysis(cfg)

    report = {
        "timestamp": datetime.now().isoformat(),
        "lookback_days": cfg["lookback_days"],
        "total_runs": len(runs),
        "total_blocked": len(blocked),
        "findings": findings,
        "summary": {
            "by_role": {},
            "by_outcome": {},
            "by_model": {},
        },
    }

    # Build summary stats
    for r in runs:
        role = r["role"]
        outcome = r["outcome"]
        model = r["model"]

        if role not in report["summary"]["by_role"]:
            report["summary"]["by_role"][role] = {"total": 0, "success": 0}
        report["summary"]["by_role"][role]["total"] += 1
        if r["success"]:
            report["summary"]["by_role"][role]["success"] += 1

        report["summary"]["by_outcome"][outcome] = \
            report["summary"]["by_outcome"].get(outcome, 0) + 1

        if model not in report["summary"]["by_model"]:
            report["summary"]["by_model"][model] = {"total": 0, "cost": 0.0}
        report["summary"]["by_model"][model]["total"] += 1
        report["summary"]["by_model"][model]["cost"] += r.get("cost_usd") or 0

    print(json.dumps(report, indent=2, default=str))
    return report


def run_full(cfg, dry_run=False):
    """Full ForgeSmith pipeline: COLLECT → ANALYZE → DECIDE → APPLY → LOG."""
    mode = "dry_run" if dry_run else "auto"
    run_id = f"fs-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    log(f"ForgeSmith run {run_id} ({mode})")
    log(f"{'='*60}")

    # Collect + Analyze
    findings, runs, blocked = run_analysis(cfg)

    if not findings:
        log("No actionable patterns found. Nothing to do.")
        if not dry_run:
            log_run(run_id, len(runs), [], "No actionable patterns found.", mode, cfg)
        return

    # Evaluate previous changes (only in auto mode)
    if not dry_run and runs:
        log("\nPHASE 2.5: EVALUATE PREVIOUS CHANGES")
        evaluated = evaluate_previous_changes(runs, cfg)
        if evaluated:
            for e in evaluated:
                rubric_info = ""
                if e.get("rubric_delta") is not None:
                    rubric_info = f" (rubric_delta={e['rubric_delta']:+.3f}, heuristic={e['heuristic_score']:+.2f})"
                log(f"  Change #{e['change_id']}: score={e['score']}{rubric_info}")
        else:
            log("  No previous changes to evaluate.")

    # Extract lessons from runs (auto mode only)
    if not dry_run and runs:
        log("\nPHASE 2.7: EXTRACT LESSONS")
        lessons_added = extract_lessons(runs, cfg)
        log(f"  {lessons_added} new lessons extracted")

    # SIMBA: targeted rule generation from failure patterns
    if not dry_run and runs:
        log("\nPHASE 2.75: SIMBA (Targeted Rule Generation)")
        simba_results = run_simba(cfg, dry_run=dry_run)
        if simba_results["rules_generated"] > 0:
            log(f"  Generated {simba_results['rules_generated']} new rules")
            for role, detail in simba_results["details"].items():
                if detail.get("stored", 0) > 0:
                    log(f"    [{role}] {detail['stored']} rules stored")
        if simba_results["rules_pruned"] > 0:
            log(f"  Pruned {simba_results['rules_pruned']} stale rules")

    # Rubric scoring (auto mode only)
    if not dry_run and runs:
        log("\nPHASE 2.8: RUBRIC SCORING")
        scored = score_completed_runs(runs, cfg)
        if scored:
            log(f"  Scored {len(scored)} agent runs")
            # Show a few examples
            for scored_run_id, norm_score in scored[:5]:
                log(f"    run #{scored_run_id}: normalized={norm_score:.2f}")
        else:
            log("  No new runs to score.")

        # Evolve rubric weights if enough data
        log("\nPHASE 2.9: RUBRIC EVOLUTION")
        weight_changes = evolve_rubric_weights(cfg)
        if weight_changes:
            for role, criteria in weight_changes.items():
                for criterion, (old, new) in criteria.items():
                    log(f"  [{role}] {criterion}: {old} -> {new}")
        else:
            log("  No rubric weight changes (insufficient data or no significant correlations).")

    # Ghost finding verification (auto mode only)
    if not dry_run:
        log("\nPHASE 2.95: GHOST VERIFICATION")
        ghost_results = run_ghost_verification(dry_run=dry_run)
        if ghost_results["findings_checked"] > 0:
            log(f"  Checked {ghost_results['findings_checked']} findings: "
                f"{ghost_results['verified']} verified, "
                f"{ghost_results['still_present']} still present")
        else:
            log("  No unverified resolved security findings.")

    # Apply changes
    log(f"\nPHASE 3: {'PROPOSE' if dry_run else 'APPLY'} CHANGES")
    changes = apply_changes(findings, run_id, cfg, dry_run=dry_run)

    if not changes:
        log("No changes to apply.")
    else:
        log(f"\n{'Proposed' if dry_run else 'Applied'} {len(changes)} changes:")
        for i, c in enumerate(changes, 1):
            log(f"  {i}. [{c['change_type']}] {c.get('rationale', '')[:120]}")

    # OPRO-style LLM-driven proposal phase
    opro_cfg = cfg.get("opro", {})
    if opro_cfg.get("enabled", True) and runs:
        log(f"\nPHASE 4.7: PROPOSE (OPRO)")
        opro_proposals = generate_opro_proposals(runs, cfg, dry_run=dry_run)
        if opro_proposals:
            opro_changes = apply_opro_proposals(
                opro_proposals, run_id, cfg, dry_run=dry_run
            )
            changes.extend(opro_changes)
            total_props = sum(len(r.get("proposals", [])) for r in opro_proposals)
            log(f"  OPRO: {total_props} proposals for {len(opro_proposals)} roles, "
                f"{len(opro_changes)} applied")
        else:
            log("  OPRO: No proposals generated.")

    # GEPA: DSPy-based automatic prompt evolution (weekly)
    gepa_cfg = cfg.get("gepa", {})
    if gepa_cfg.get("enabled", False) and runs:
        log(f"\nPHASE 4.8: EVOLVE (GEPA)")
        gepa_results = run_gepa(cfg, dry_run=dry_run, run_id=run_id)
        if gepa_results.get("roles_evolved", 0) > 0:
            log(f"  GEPA: {gepa_results['roles_evolved']} roles evolved")
            for role, detail in gepa_results.get("details", {}).items():
                if detail.get("evolved"):
                    log(f"    [{role}] v{detail['version']} "
                        f"(diff: {detail['diff_ratio']:.1%})")
        if gepa_results.get("rollbacks", 0) > 0:
            log(f"  GEPA: {gepa_results['rollbacks']} rollbacks (underperformers)")
        if not gepa_results.get("roles_evolved") and not gepa_results.get("rollbacks"):
            log("  GEPA: No evolutions or rollbacks.")

    # LITM: Self-tuning Lost-in-the-Middle attention weights (weekly)
    if not dry_run:
        log(f"\nPHASE 4.9: LITM WEIGHT AUDIT")
        try:
            litm_report = run_litm_audit(
                db_path=THEFORGE_DB,
                dispatch_config_path=DISPATCH_CONFIG,
                lookback_days=cfg.get("lookback_days", 7),
                threshold=5,
                dry_run=False
            )
            if litm_report["changes"]:
                log(f"  LITM: {len(litm_report['changes'])} weight adjustments")
                for change in litm_report["changes"]:
                    log(f"    {change}")
            else:
                log(f"  LITM: No adjustments needed ({litm_report['total_misses']} misses)")
        except sqlite3.Error as exc:
            # Schema drift or DB issue must not crash the whole nightly run.
            log(f"  LITM: SKIPPED — database error: {exc}")

    # Log the run
    if not dry_run:
        summary = f"{len(findings)} patterns found, {len(changes)} changes applied"
        log_run(run_id, len(runs), changes, summary, mode, cfg)

    log(f"\n{'='*60}")
    log(f"ForgeSmith run complete: {run_id}")


def run_rollback(run_id_to_revert):
    """Revert all changes from a specific ForgeSmith run."""
    changes = collect_previous_changes(run_id=run_id_to_revert)
    if not changes:
        log(f"No changes found for run {run_id_to_revert}")
        return

    log(f"Rolling back {len(changes)} changes from run {run_id_to_revert}")
    for change in changes:
        if change["reverted_at"]:
            log(f"  Change #{change['id']} already reverted. Skipping.")
            continue
        rollback_change(change)

    log("Rollback complete.")


def main():
    parser = argparse.ArgumentParser(
        description="ForgeSmith — Self-learning agent tuning for EQUIPA")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auto", action="store_true",
                       help="Full run: analyze + apply changes")
    group.add_argument("--dry-run", action="store_true",
                       help="Show proposed changes without applying")
    group.add_argument("--report", action="store_true",
                       help="JSON analysis report only")
    group.add_argument("--rollback", metavar="RUN_ID",
                       help="Revert all changes from a specific run")
    group.add_argument("--lessons", nargs="?", const="all", metavar="ROLE",
                       help="Show active lessons (optionally filter by role)")
    group.add_argument("--rubrics", nargs="?", const="all", metavar="ROLE",
                       help="Show rubric scores (optionally filter by role)")
    group.add_argument("--propose", action="store_true",
                       help="Run OPRO proposal step only (generate LLM-driven prompt proposals)")
    group.add_argument("--simba", nargs="?", const="all", metavar="ROLE",
                       help="Run SIMBA rule generation only (optionally filter by role)")
    group.add_argument("--gepa", nargs="?", const="all", metavar="ROLE",
                       help="Run GEPA prompt evolution only (optionally filter by role)")

    args = parser.parse_args()
    cfg = load_config()

    if args.propose:
        run_propose_only(cfg, dry_run=False)
    elif args.gepa is not None:
        role = args.gepa if args.gepa != "all" else None
        log("ForgeSmith GEPA — DSPy Prompt Evolution")
        log(f"{'='*60}")
        results = run_gepa(cfg, dry_run=False, role_filter=role)
        log(f"\n{'='*60}")
        print(json.dumps(results, indent=2, default=str))
    elif args.simba is not None:
        role = args.simba if args.simba != "all" else None
        log("ForgeSmith SIMBA — Targeted Rule Generation")
        log(f"{'='*60}")
        results = run_simba(cfg, dry_run=False, role_filter=role)
        log(f"\n{'='*60}")
        print(json.dumps(results, indent=2, default=str))
    elif args.report:
        run_report(cfg)
    elif args.rollback:
        run_rollback(args.rollback)
    elif args.rubrics is not None:
        role = args.rubrics if args.rubrics != "all" else None
        scores = get_rubric_report(role=role)
        if scores:
            # Print header
            print(f"{'ID':>4} {'Run':>4} {'Role':<22} {'Score':>6} {'Max':>4} {'Norm':>5} {'OK':>3} {'Criteria'}")
            print("-" * 90)
            for s in scores:
                criteria = json.loads(s["criteria_scores"])
                criteria_str = ", ".join(f"{k}={v}" for k, v in criteria.items())
                ok = "Y" if s["success"] else "N"
                print(f"{s['id']:>4} {s['agent_run_id']:>4} {s['role']:<22} "
                      f"{s['total_score']:>6.1f} {s['max_possible']:>4.0f} "
                      f"{s['normalized_score']:>5.2f} {ok:>3} {criteria_str}")

            # Show rubric definitions
            print(f"\nCurrent rubric weights:")
            rubrics = cfg.get("rubric_definitions", {})
            for rname, rcriteria in sorted(rubrics.items()):
                print(f"  {rname}: {rcriteria}")

            # Show correlations if available
            correlations = analyze_rubric_correlations(cfg)
            if correlations:
                print(f"\nCorrelation analysis (success vs failure, higher = more predictive):")
                for rname, criteria in sorted(correlations.items()):
                    sorted_criteria = sorted(criteria.items(), key=lambda x: -x[1])
                    print(f"  {rname}: {', '.join(f'{k}={v:+.2f}' for k, v in sorted_criteria)}")

            # Show evolution history if available
            conn = get_db()
            try:
                evo_rows = conn.execute(
                    """SELECT rubric_version, role, criterion, old_weight, new_weight,
                              correlation, created_at
                       FROM rubric_evolution_history
                       ORDER BY created_at DESC LIMIT 20"""
                ).fetchall()
                if evo_rows:
                    print(f"\nRubric evolution history (last {len(evo_rows)} changes):")
                    for row in evo_rows:
                        print(f"  v{row['rubric_version']} [{row['role']}] "
                              f"{row['criterion']}: {row['old_weight']} -> {row['new_weight']} "
                              f"(corr={row['correlation']:+.3f}) @ {row['created_at']}")
            except sqlite3.OperationalError:
                pass  # Table doesn't exist yet
            finally:
                conn.close()
        else:
            print("No rubric scores found.")
    elif args.lessons is not None:
        role = args.lessons if args.lessons != "all" else None
        lessons = get_relevant_lessons(role=role, limit=20)
        if lessons:
            for l in lessons:
                print(f"[{l['id']}] (seen {l['times_seen']}x) {l['lesson']}")
        else:
            print("No lessons found.")
    else:
        run_full(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

