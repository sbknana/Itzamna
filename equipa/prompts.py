"""EQUIPA prompts — system prompt construction and context engineering.

Layer 5: Imports from equipa.constants, equipa.parsing, equipa.lessons, equipa.security.

Implements a static/dynamic cache split strategy ported from Claude Code
(nirholas-claude-code/src/constants/prompts.ts). The system prompt is divided
into two sections separated by SYSTEM_PROMPT_DYNAMIC_BOUNDARY:

  Static (globally cacheable):  _common.md + role prompt
  Dynamic (per-task):           lessons, episodes, task description, language, budget

This allows API-level prompt caching across tasks that share the same role,
reducing cost by avoiding re-processing of the static prefix on every request.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import string
import sys
from pathlib import Path
from typing import Any

from equipa.config import is_feature_enabled
from equipa.constants import (
    BUDGET_CHECK_INTERVAL,
    PROMPTS_DIR,
    ROLE_PROMPTS,
    STANDING_ORDERS_DIR,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
)
from equipa.lessons import (
    _injected_episodes_by_task,
    format_episodes_for_injection,
    format_lessons_for_injection,
    get_relevant_episodes,
    update_episode_injection_count,
    update_lesson_injection_count,
)
from equipa.parsing import (
    CHARS_PER_TOKEN,
    EPISODE_REDUCTION_THRESHOLD,
    SYSTEM_PROMPT_TOKEN_HARD_LIMIT,
    SYSTEM_PROMPT_TOKEN_TARGET,
    _trim_prompt_section,
    build_compaction_summary,  # noqa: F401 — re-exported for convenience
    compact_agent_output,
    deduplicate_lessons,
    estimate_tokens,
)
from equipa.security import _make_untrusted_delimiter, wrap_untrusted


# Track which prompt version was used per role (for A/B testing telemetry).
# Set by build_system_prompt(), read by record_agent_run().
_last_prompt_version: dict[str, str] = {}


class PromptResult:
    """System prompt with static/dynamic cache split.

    The prompt is divided into two sections by SYSTEM_PROMPT_DYNAMIC_BOUNDARY:

    - **static_prefix**: Role-invariant content (_common.md + role prompt).
      Same for every task dispatched with the same role. Can be cached globally
      by the API provider to avoid re-tokenizing on each request.

    - **dynamic_suffix**: Per-task content (lessons, episodes, task description,
      language guidance, budget visibility, extra context). Changes every dispatch.

    Usage:
        result = build_system_prompt(...)
        # Full prompt (backward-compatible, includes boundary marker):
        full = result.full
        # Or use str() for the same:
        full = str(result)
        # Access split sections for cache-aware API calls:
        static = result.static_prefix
        dynamic = result.dynamic_suffix

    The boundary marker is included in the full prompt so callers that don't
    understand caching can pass the whole string as-is. Cache-aware callers
    can split on the marker to send static content with scope='global'.
    """

    __slots__ = ("static_prefix", "dynamic_suffix")

    def __init__(self, static_prefix: str, dynamic_suffix: str) -> None:
        self.static_prefix = static_prefix
        self.dynamic_suffix = dynamic_suffix

    @property
    def full(self) -> str:
        """Full prompt string with boundary marker between static and dynamic."""
        return (
            self.static_prefix
            + "\n\n"
            + SYSTEM_PROMPT_DYNAMIC_BOUNDARY
            + "\n\n"
            + self.dynamic_suffix
        )

    def __str__(self) -> str:
        """Backward-compatible: str(prompt_result) returns the full prompt."""
        return self.full

    def __len__(self) -> int:
        """Length of the full prompt string."""
        return len(self.full)

    def __contains__(self, item: str) -> bool:
        """Support 'in' operator for backward-compatible substring checks."""
        return item in self.full

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PromptResult):
            return (
                self.static_prefix == other.static_prefix
                and self.dynamic_suffix == other.dynamic_suffix
            )
        if isinstance(other, str):
            return self.full == other
        return NotImplemented

    def __repr__(self) -> str:
        static_len = len(self.static_prefix)
        dynamic_len = len(self.dynamic_suffix)
        return (
            f"PromptResult(static={static_len} chars, "
            f"dynamic={dynamic_len} chars, "
            f"total={static_len + dynamic_len} chars)"
        )

    def __getitem__(self, key: int | slice) -> str:
        """Support indexing and slicing (prompt[i] or prompt[i:j])."""
        return self.full[key]

    def __getattr__(self, name: str) -> Any:
        """Delegate string methods (lower, find, split, etc.) to full prompt.

        This ensures backward compatibility: existing code that calls
        prompt.lower(), prompt.find(), prompt.strip(), etc. continues
        to work transparently after the str→PromptResult migration.
        """
        full = self.full
        attr = getattr(full, name, None)
        if attr is not None:
            return attr
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )


def load_paralysis_template(retry_num: int) -> string.Template:
    """Load the escalating paralysis-retry prompt template.

    retry_num is the count of prior paralysis kills on this task.
    - retry_num == 0 -> prompts/_paralysis_attempt_1.md (first retry)
    - retry_num == 1 -> prompts/_paralysis_attempt_2.md (second retry)
    - retry_num >= 2 -> prompts/_paralysis_attempt_3.md (escalating)

    Returns a string.Template instance ready for substitute(). The trailing
    newline (if any) is stripped so output matches the prior inline strings
    byte-for-byte.

    Available substitution variables (caller must provide all referenced):
    - $reduced_kill (all attempts)
    - $paralysis_retries, $agent_count (attempt 3+ only)
    """
    if retry_num <= 0:
        path = PROMPTS_DIR / "_paralysis_attempt_1.md"
    elif retry_num == 1:
        path = PROMPTS_DIR / "_paralysis_attempt_2.md"
    else:
        path = PROMPTS_DIR / "_paralysis_attempt_3.md"
    text = path.read_text(encoding="utf-8").rstrip("\n")
    return string.Template(text)


def load_standing_orders(role: str) -> str:
    """Load the standing orders file for a given role.

    Standing orders define a role's permanent operating authority, approval
    gates, and escalation rules. They live in standing_orders/{role}.md and
    are injected into the static prefix of every dispatch for that role.

    Returns the standing orders text prefixed with a section separator,
    or an empty string if no standing orders file exists for the role.
    """
    orders_path = STANDING_ORDERS_DIR / f"{role}.md"
    if not orders_path.exists():
        return ""
    try:
        text = orders_path.read_text(encoding="utf-8")
        return "\n\n---\n\n" + text
    except OSError:
        return ""


def build_task_prompt(
    task: dict[str, Any],
    project_context: dict[str, Any],
    project_dir: str,
    delimiter: str | None = None,
) -> str:
    """Build the task-specific instruction block.

    Args:
        delimiter: Unpredictable boundary token from _make_untrusted_delimiter().
            When provided, all database-sourced content is additionally wrapped
            in <<<DELIMITER>>> ... <<<END_DELIMITER>>> markers so agents can
            distinguish data from instructions even if <task-input> tags are
            spoofed by injected content.
    """
    # Task metadata (safe — controlled by orchestrator, not user input)
    lines = [
        "## Assigned Task",
        f"- **Task ID:** {task['id']}",
        f"- **Project:** {task.get('project_name', 'Unknown')} (project_id: {task.get('project_id', '?')})",
        f"- **Priority:** {task.get('priority', 'medium')}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Helper: wrap content in both task-input tags AND unpredictable delimiter
    def _wrap(tag_type: str, content: str) -> str:
        inner = wrap_untrusted(content, delimiter) if delimiter else content
        return f'<task-input type="{tag_type}" trust="database">\n{inner}\n</task-input>'

    # Task title and description — from database, could contain injection
    lines.append(_wrap("task-title", task["title"]))
    lines.append("")
    lines.append(_wrap("task-description", task.get("description", "No description provided")))
    lines.append("")

    # Project context also wrapped — comes from database
    session = project_context.get("last_session")
    if session:
        ctx_lines = [f"Last session ({session.get('session_date', 'unknown')}):"]
        ctx_lines.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            ctx_lines.append(f"Next steps: {session['next_steps']}")
        lines.append("## Recent Project Context")
        lines.append(_wrap("session-context", "\n".join(ctx_lines)))
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        q_lines = []
        for q in questions:
            q_lines.append(f"- {q['question']}")
            if q.get("context"):
                q_lines.append(f"  Context: {q['context']}")
        lines.append("## Open Questions (unresolved)")
        lines.append(_wrap("open-questions", "\n".join(q_lines)))
        lines.append("")

    decisions = project_context.get("recent_decisions", [])
    if decisions:
        d_lines = []
        for d in decisions:
            d_lines.append(f"- {d['decision']} ({d.get('decided_at', 'unknown')})")
            if d.get("rationale"):
                d_lines.append(f"  Rationale: {d['rationale']}")
        lines.append("## Recent Decisions")
        lines.append(_wrap("decisions", "\n".join(d_lines)))
        lines.append("")

    return "\n".join(lines)


def _apply_litm_reordering(history: str, alpha: float = 0.92, beta: float = 0.50, gamma: float = 0.88) -> str:
    """Apply Lost-in-the-Middle U-curve positional weighting to compaction history.

    Reorders sections so that task description + recent failures go at START and END,
    with old history in the MIDDLE. Uses Claude profile weights: alpha=0.92, beta=0.50, gamma=0.88.

    Returns reordered history string.
    """
    if not history or "## Prior Work Summary" not in history:
        return history

    # Split into sections by ## headers
    sections = re.split(r'(## Prior Work Summary[^\n]*)', history)
    if len(sections) < 3:
        return history

    # Extract header + content pairs
    paired: list[tuple[str, str]] = []
    for i in range(1, len(sections), 2):
        if i + 1 < len(sections):
            paired.append((sections[i], sections[i+1]))

    if len(paired) <= 2:
        return history  # Not enough to reorder

    # Sort by cycle number (ascending = oldest first)
    def extract_cycle(header: str) -> int:
        match = re.search(r'Cycle (\d+)', header)
        return int(match.group(1)) if match else 0

    paired.sort(key=lambda x: extract_cycle(x[0]))

    # Apply U-curve: first + last at edges, middle in center
    n = len(paired)
    reordered: list[tuple[str, str]] = []

    # Most recent (highest attention)
    reordered.append(paired[-1])

    # Middle items (lowest attention)
    for item in paired[1:-1]:
        reordered.append(item)

    # Oldest (moderate attention, establishes context)
    reordered.append(paired[0])

    # Reconstruct
    result = sections[0]  # Preamble before first ##
    for header, content in reordered:
        result += header + content

    return result


def build_system_prompt(
    task: dict[str, Any],
    project_context: dict[str, Any],
    project_dir: str,
    role: str = "developer",
    extra_context: str = "",
    dispatch_config: dict | None = None,
    error_type: str | None = None,
    max_turns: int | None = None,
) -> PromptResult:
    """Read _common.md + role prompt, replace placeholders, append task prompt.

    Returns a PromptResult with static/dynamic cache split. The static prefix
    (_common.md + role prompt) is the same for every task with a given role and
    can be cached globally by the API provider. The dynamic suffix contains
    per-task content (lessons, episodes, task description, etc.).

    Applies context engineering principles:
    - **Static/dynamic cache split** (reduces API cost via prompt caching)
    - Token budget management (8K target, trimming in priority order)
    - Lesson deduplication (60%+ word overlap removed, max 5)
    - Episode relevance scoring (keyword overlap + recency weighting)
    - Token count logging per dispatch
    - A/B prompt version selection (GEPA-evolved prompts)
    - Budget visibility (max_turns info injected into prompt)

    extra_context: optional string appended after the task prompt (used for
    compaction history and test failure feedback in dev-test loop).
    dispatch_config: optional config dict for task-type-specific prompt injection.
    error_type: optional error type to filter relevant lessons (e.g. 'timeout', 'max_turns').
    max_turns: optional int — the turn budget allocated for this agent run.
        When provided, a budget visibility line is injected into the prompt so
        the agent can make rational decisions about depth vs breadth.

    Returns PromptResult with .static_prefix, .dynamic_suffix, and .full properties.
    str(result) returns the full prompt for backward compatibility.
    Also sets _last_prompt_version[role] for telemetry tracking.
    """
    # Late imports to avoid circular dependencies with monolith during transition
    from equipa.git_ops import detect_project_language


    try:
        from forgesmith import get_relevant_lessons
    except ImportError:
        def get_relevant_lessons(role=None, error_type=None, limit=5):
            return []

    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS.get(role)

    if not role_path:
        print(f"ERROR: Unknown role '{role}'. Available: {', '.join(ROLE_PROMPTS.keys())}")
        sys.exit(1)

    if not common_path.exists():
        print(f"ERROR: Common prompt not found at {common_path}")
        sys.exit(1)

    if not role_path.exists():
        print(f"ERROR: Role prompt not found at {role_path}")
        sys.exit(1)

    # A/B prompt version selection: try GEPA-evolved prompt if available
    # Gated by gepa_ab_testing feature flag
    prompt_version = "baseline"
    if is_feature_enabled(dispatch_config, "gepa_ab_testing"):
        try:
            from forgesmith_gepa import get_ab_prompt_for_role
            selected_path, prompt_version = get_ab_prompt_for_role(role)
            if selected_path.exists() and prompt_version != "baseline":
                role_path = selected_path
        except ImportError:
            pass  # forgesmith_gepa not available, use baseline

    # Track which version was used for telemetry
    _last_prompt_version[role] = prompt_version

    # Generate a per-prompt unpredictable delimiter for untrusted content
    # isolation.  This prevents injected content from spoofing or closing
    # its own boundary markers (addresses EQ-24, EQ-10, EQ-25).
    _untrusted_delimiter = _make_untrusted_delimiter()

    # =========================================================================
    # STATIC PREFIX — cacheable across ALL tasks for the same role
    # Contains: _common.md + role-specific prompt (NO per-task substitution)
    # This section is byte-identical for every task dispatched with the same
    # role, enabling global cache scope at the API level.
    #
    # Per-task values ({task_id}, {project_id}) appear in role prompt SQL
    # examples as literal placeholders.  The agent resolves them from the
    # "## Assigned Task" block in the dynamic suffix, which already contains
    # "Task ID: <N>" and "project_id: <N>".  Do NOT str.replace() here —
    # that would make every dispatch unique and defeat prompt caching.
    # =========================================================================
    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    standing_orders = load_standing_orders(role)
    static_prefix = common_text + "\n\n---\n\n" + role_text + standing_orders

    # =========================================================================
    # DYNAMIC SUFFIX — per-task content, changes every dispatch
    # Contains: lessons, episodes, task prompt, language guidance, budget, extra
    # =========================================================================
    dynamic_parts: list[str] = []

    # --- Lesson injection with deduplication ---
    # Gated by forgesmith_lessons feature flag
    if is_feature_enabled(dispatch_config, "forgesmith_lessons"):
        # Fetch more candidates than we'll inject, then deduplicate
        lessons = get_relevant_lessons(role=role, error_type=error_type, limit=10)
        if lessons:
            # Deduplicate: remove 60%+ word overlap, cap at 5
            lessons = deduplicate_lessons(lessons)
            lessons_text = format_lessons_for_injection(lessons, delimiter=_untrusted_delimiter)
            dynamic_parts.append(lessons_text)
            # Update times_injected counter for each lesson
            update_lesson_injection_count([l["id"] for l in lessons])

    # --- Episode injection with relevance scoring ---
    # Gated by forgesmith_episodes feature flag
    task_id = task.get("id") if isinstance(task, dict) else task
    project_id = task.get("project_id") if isinstance(task, dict) else None
    task_type = task.get("task_type", "feature") if isinstance(task, dict) else None
    task_description = task.get("description", "") if isinstance(task, dict) else ""

    if is_feature_enabled(dispatch_config, "forgesmith_episodes"):
        # Check token budget to decide episode limit (estimate from static only)
        current_tokens = estimate_tokens(static_prefix)
        episode_limit = 3
        if current_tokens > EPISODE_REDUCTION_THRESHOLD:
            episode_limit = 2  # Reduce episodes when prompt is already large

        if project_id:
            episodes = get_relevant_episodes(
                role=role, project_id=project_id, task_type=task_type,
                min_q_value=0.3, limit=episode_limit,
                task_description=task_description,
                dispatch_config=dispatch_config,
            )
            if episodes:
                episodes_text = format_episodes_for_injection(episodes, delimiter=_untrusted_delimiter)
                dynamic_parts.append(episodes_text)
                # Track injected episode IDs for q-value updates after task completion
                ep_ids = [ep["id"] for ep in episodes]
                _injected_episodes_by_task[task_id] = ep_ids
                update_episode_injection_count(ep_ids)

                # Create co-accessed edges in knowledge graph (if enabled)
                knowledge_graph_enabled = False
                try:
                    knowledge_graph_enabled = is_feature_enabled(dispatch_config, "knowledge_graph")
                except Exception:
                    pass

                if knowledge_graph_enabled and len(ep_ids) >= 2:
                    try:
                        from equipa import graph
                        graph.create_coaccessed_edges(ep_ids)
                        # Silent success — don't spam logs during prompt building
                    except Exception:
                        # Graph module unavailable or error — continue without graph updates
                        pass

    # Inject task-type-specific guidance if available
    if dispatch_config and "task_type_prompts" in dispatch_config:
        task_type = task.get("task_type", "feature") or "feature"
        task_type_prompts = dispatch_config["task_type_prompts"]
        if task_type in task_type_prompts:
            dynamic_parts.append(
                f"## Task Type Guidance ({task_type})\n\n"
                f"{task_type_prompts[task_type]}"
            )

    # Append task-specific instructions (never trimmed)
    task_prompt = build_task_prompt(task, project_context, project_dir, delimiter=_untrusted_delimiter)
    dynamic_parts.append("---\n\n" + task_prompt)

    # --- Initiative context (Phase 1) ---
    # If this task belongs to an initiative, inject the per-initiative
    # plan file as shared working memory plus the agent-output protocol
    # instruction. Initiative-less tasks (initiative_id IS NULL) take
    # the no-op branch and dispatch exactly as today.
    if isinstance(task, dict):
        initiative_id = task.get("initiative_id")
        if initiative_id and project_dir:
            try:
                from equipa.initiative import build_initiative_prompt_section
                from equipa.db import get_db_connection

                conn = get_db_connection(write=False)
                try:
                    section = build_initiative_prompt_section(
                        Path(project_dir),
                        int(initiative_id),
                        conn,
                        delimiter=_untrusted_delimiter,
                    )
                finally:
                    conn.close()
                if section:
                    dynamic_parts.append(section)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to inject initiative context for task=%s "
                    "initiative=%s; proceeding without it.",
                    task.get("id"), initiative_id,
                )

    # --- Language-specific prompt injection ---
    # Detect project language and load corresponding guidance if available
    # Gated by language_prompts feature flag
    if project_dir and is_feature_enabled(dispatch_config, "language_prompts"):
        lang_info = detect_project_language(project_dir)
        lang_prompts_dir = PROMPTS_DIR / "languages"
        injected_langs: set[str] = set()
        for lang_key in lang_info.get("languages", []):
            lang_prompt_path = lang_prompts_dir / f"{lang_key}.md"
            if lang_prompt_path.exists() and lang_key not in injected_langs:
                try:
                    lang_text = lang_prompt_path.read_text(encoding="utf-8")
                    frameworks_note = ""
                    if lang_info.get("frameworks"):
                        detected = [f for f in lang_info["frameworks"]
                                    if f not in ("dotnet", "maven", "gradle")]
                        if detected:
                            frameworks_note = (
                                f"\n\nDetected frameworks: {', '.join(detected)}. "
                                f"Apply framework-specific patterns where relevant."
                            )
                    dynamic_parts.append(lang_text + frameworks_note)
                    injected_langs.add(lang_key)
                except OSError:
                    pass  # File read failed, skip silently

    # Budget visibility: tell the agent how many turns it has
    if max_turns and max_turns > 0:
        dynamic_parts.append(
            f"You have {max_turns} turns for this task. "
            f"The orchestrator will log budget updates every "
            f"{BUDGET_CHECK_INTERVAL} turns."
        )

    # Append extra context (compaction history, test failures) if provided
    # Apply LITM U-curve positional weighting before appending
    if extra_context:
        extra_context = _apply_litm_reordering(extra_context)
        dynamic_parts.append("---\n\n" + extra_context)

    # Assemble dynamic suffix
    dynamic_suffix = "\n\n".join(dynamic_parts)

    # --- Token budget enforcement ---
    # Trim in priority order: old episodes first, then generic lessons
    # Never trim: role prompt (static), task description
    # Trimming operates on the dynamic suffix only (static is never trimmed)
    full_tokens = estimate_tokens(static_prefix) + estimate_tokens(dynamic_suffix)

    if full_tokens > SYSTEM_PROMPT_TOKEN_TARGET:
        # Priority 1: Trim old episodes (## Past Experience)
        dynamic_suffix = _trim_prompt_section(
            dynamic_suffix, "## Past Experience",
            max_chars=CHARS_PER_TOKEN * 500,
        )
        full_tokens = estimate_tokens(static_prefix) + estimate_tokens(dynamic_suffix)

        # Priority 2: Trim generic lessons (## Lessons from Previous Runs)
        if full_tokens > SYSTEM_PROMPT_TOKEN_HARD_LIMIT:
            dynamic_suffix = _trim_prompt_section(
                dynamic_suffix, "## Lessons from Previous Runs",
                max_chars=CHARS_PER_TOKEN * 300,
            )
            full_tokens = estimate_tokens(static_prefix) + estimate_tokens(dynamic_suffix)

        # Priority 3: Trim extra context (## Prior Work Summary, etc.)
        if full_tokens > SYSTEM_PROMPT_TOKEN_HARD_LIMIT:
            dynamic_suffix = _trim_prompt_section(
                dynamic_suffix, "## Prior Work Summary",
                max_chars=CHARS_PER_TOKEN * 400,
            )

    # Log token count with cache split visibility
    static_tokens = estimate_tokens(static_prefix)
    dynamic_tokens = estimate_tokens(dynamic_suffix)
    total_tokens = static_tokens + dynamic_tokens
    budget_status = "OK" if total_tokens <= SYSTEM_PROMPT_TOKEN_TARGET else "OVER"
    result = PromptResult(static_prefix=static_prefix, dynamic_suffix=dynamic_suffix)
    print(
        f"  [ContextEng] System prompt: {len(result)} chars, ~{total_tokens} tokens "
        f"({budget_status}, target: {SYSTEM_PROMPT_TOKEN_TARGET}) "
        f"[static: ~{static_tokens}t | dynamic: ~{dynamic_tokens}t]"
    )

    return result


def build_checkpoint_context(checkpoint_text: str, attempt: int) -> str:
    """Build context string from a checkpoint for the next agent attempt.

    Uses compact_agent_output() to extract structured data (RESULT, FILES_CHANGED,
    BLOCKERS, SUMMARY) instead of passing raw text, preventing context rot.
    """
    # Compact checkpoint to structured summary (max 200 words)
    compacted = compact_agent_output(checkpoint_text, max_words=200)

    return (
        f"## Previous Attempt (#{attempt}) — Continue From Here\n\n"
        f"**The previous agent ran out of turns. Start writing code IMMEDIATELY — "
        f"do not repeat the same research.**\n\n"
        f"**The previous agent FAILED because it spent all its time reading instead "
        f"of writing code. DO NOT make the same mistake. You are the replacement.**\n\n"
        f"Start writing code IMMEDIATELY. Your FIRST tool call must be Edit or Write — "
        f"not Read, not Glob, not Grep. You have the previous agent's summary below. "
        f"Use it to skip exploration entirely and go straight to implementation.\n\n"
        f"### Previous Agent Summary:\n"
        f"<task-input type=\"checkpoint\" trust=\"agent-output\">\n{compacted}\n</task-input>\n\n"
        f"**CRITICAL:** Do NOT repeat the previous agent's exploration. Do NOT re-read "
        f"files they already read. Do NOT analyze the codebase from scratch. Look at "
        f"what remains to be done and START CODING in your FIRST turn.\n\n"
        f"**You are a SENIOR engineer. Make decisions. Write code. Ship it.** "
        f"The orchestrator is watching — another failure means this task gets "
        f"permanently blocked and escalated to a human. Do not be the agent that "
        f"causes escalation."
    )


def build_planner_prompt(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
) -> PromptResult:
    """Build the system prompt for the Planner agent.

    The Planner gets the goal, project context, and codebase access.
    It does NOT get a task — it creates tasks.

    Returns PromptResult with static/dynamic cache split.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["planner"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    standing_orders = load_standing_orders("planner")

    # Static prefix: byte-identical across all planner dispatches.
    # Do NOT substitute {project_id}/{task_id} — they're in the dynamic suffix.
    static_prefix = common_text + "\n\n---\n\n" + role_text + standing_orders

    # Per-prompt unpredictable delimiter for untrusted content isolation
    _delim = _make_untrusted_delimiter()

    # Helper: wrap content in both task-input tags AND unpredictable delimiter
    def _wrap(tag_type: str, content: str) -> str:
        inner = wrap_untrusted(content, _delim)
        return f'<task-input type="{tag_type}" trust="user">\n{inner}\n</task-input>'

    # Dynamic suffix: goal and project context (includes project_id for agent)
    lines = [
        "## Goal",
        "",
        _wrap("goal", goal),
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Add project context
    session = project_context.get("last_session")
    if session:
        ctx_parts = [f"Last session ({session.get('session_date', 'unknown')}):"]
        ctx_parts.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            ctx_parts.append(f"Next steps: {session['next_steps']}")
        lines.append("## Recent Project Context")
        lines.append(_wrap("session-context", "\n".join(ctx_parts)))
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        q_lines = [f"- {q['question']}" for q in questions]
        lines.append("## Open Questions (unresolved)")
        lines.append(_wrap("open-questions", "\n".join(q_lines)))
        lines.append("")

    dynamic_suffix = "---\n\n" + "\n".join(lines)
    return PromptResult(static_prefix=static_prefix, dynamic_suffix=dynamic_suffix)


def build_evaluator_prompt(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    completed_tasks: list[dict],
    blocked_tasks: list[dict],
) -> PromptResult:
    """Build the system prompt for the Evaluator agent.

    The Evaluator gets the original goal, completed/blocked task info,
    and codebase access to verify work.

    Returns PromptResult with static/dynamic cache split.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["evaluator"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")

    # Static prefix: byte-identical across all evaluator dispatches.
    # Do NOT substitute {project_id}/{task_id} — they're in the dynamic suffix.
    static_prefix = common_text + "\n\n---\n\n" + role_text

    # Per-prompt unpredictable delimiter for untrusted content isolation
    _delim = _make_untrusted_delimiter()

    def _wrap(tag_type: str, content: str, trust: str = "user") -> str:
        inner = wrap_untrusted(content, _delim)
        return f'<task-input type="{tag_type}" trust="{trust}">\n{inner}\n</task-input>'

    # Dynamic suffix: goal, project info, task results
    lines = [
        "## Original Goal",
        "",
        _wrap("goal", goal),
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Show completed tasks — task titles/descriptions come from DB
    if completed_tasks:
        ct_lines = []
        for t in completed_tasks:
            ct_lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("## Completed Tasks")
        lines.append(_wrap("completed-tasks", "\n".join(ct_lines), trust="database"))
        lines.append("")

    # Show blocked tasks — task titles/descriptions come from DB
    if blocked_tasks:
        bt_lines = []
        for t in blocked_tasks:
            bt_lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("## Blocked Tasks")
        lines.append(_wrap("blocked-tasks", "\n".join(bt_lines), trust="database"))
        lines.append("")

    if not completed_tasks and not blocked_tasks:
        lines.append("## Task Results")
        lines.append("No tasks were completed or blocked. This is unexpected.")
        lines.append("")

    dynamic_suffix = "---\n\n" + "\n".join(lines)
    return PromptResult(static_prefix=static_prefix, dynamic_suffix=dynamic_suffix)
