"""EQUIPA loops — dev-test loop, quality scoring, and security review.

Layer 7: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.roles, equipa.agent_runner, equipa.prompts, equipa.preflight,
         equipa.security, equipa.checkpoints, equipa.messages, equipa.tasks.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from equipa.agent_runner import (
    AgentResult,
    build_cli_command,
    dispatch_agent,
    run_agent,
)
from equipa.checkpoints import (
    build_compaction_recovery_context,
    clear_checkpoints,
    load_checkpoint,
    load_soft_checkpoint,
    save_checkpoint,
)
from equipa.config import is_feature_enabled, load_dispatch_config
from equipa.hooks import fire_async as fire_hook
from equipa.constants import (
    CODE_REVIEW_SEVERITY_PATTERNS,
    COMPACTION_CONSOLIDATION_MAX_WORDS,
    COST_ESTIMATE_PER_TURN,
    COST_LIMITS,
    EARLY_TERM_EXEMPT_ROLES,
    FINDING_DESCRIPTION_MAX_CHARS,
    MAX_CONTINUATIONS,
    MAX_DEV_TEST_CYCLES,
    NO_PROGRESS_LIMIT,
    SECURITY_SEVERITY_PATTERNS,
    TESTER_GIT_DIFF_MAX_CHARS,
)
from equipa.db import (
    _get_latest_agent_run_id,
    db_conn,
    get_db_connection,
    update_task_status,
)
from equipa.messages import (
    format_messages_for_prompt,
    mark_messages_read,
    post_agent_message,
    read_agent_messages,
)
from equipa.monitoring import (
    LoopDetector,
    _check_cost_limit,
    adjust_dynamic_budget,
    calculate_dynamic_budget,
    get_starting_sha,
    has_branch_commits,
    has_session_commits,
)
from equipa.output import log
from equipa.git_ops import git_run_async
from equipa.parsing import (
    build_compaction_summary,
    build_test_failure_context,
    parse_developer_output,
    parse_tester_output,
)
from equipa.preflight import (
    auto_install_dependencies,
    preflight_build_check,
    _handle_preflight_failure,
)
from equipa.prompts import (
    build_checkpoint_context,
    build_system_prompt,
    load_paralysis_template,
)
from equipa.roles import (
    _accumulate_cost,
    _apply_cost_totals,
    get_role_model,
    get_role_turns,
)
from equipa import sessions
from equipa.tasks import _get_task_status, get_task_complexity


def run_quality_scoring(
    task: dict[str, Any] | int,
    result: dict[str, Any] | str,
    outcome: str,
    role: str,
    output: Any = None,
    dispatch_config: dict | None = None,
) -> None:
    """Run post-task quality scoring and store results.

    Called after record_agent_run() on successful outcomes. Extracts
    result_text and FILES_CHANGED from the result dict, scores them,
    and stores scores in rubric_scores.

    Gated by the quality_scoring feature flag. Never crashes the
    orchestrator — all errors are logged and swallowed.
    """
    try:
        from rubric_quality_scorer import score_and_store as quality_score_and_store
    except ImportError:
        def quality_score_and_store(**kwargs):
            return None

    if not is_feature_enabled(dispatch_config, "quality_scoring"):
        return
    try:
        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None

        agent_run_id = _get_latest_agent_run_id(task_id)
        if not agent_run_id:
            log(f"  [Quality] No agent_run_id found for task {task_id}", output)
            return

        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        files_changed = parse_developer_output(result_text)

        score_result = quality_score_and_store(
            result_text=result_text,
            files_changed=files_changed,
            role=role,
            agent_run_id=agent_run_id,
            task_id=task_id,
            project_id=project_id,
        )
        if score_result:
            log(f"  [Quality] Scored run {agent_run_id}: "
                f"{score_result['total_score']:.1f}/{score_result['max_possible']:.0f} "
                f"({score_result['normalized_score']:.0%})", output)
    except Exception as e:
        log(f"  [Quality] WARNING: Quality scoring failed: {e}", output)


async def run_security_review(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
    stable_project_dir: str | None = None,
) -> dict[str, Any]:
    """Run an automatic security review after dev-test succeeds.

    Uses the security-reviewer role with ClaudeStick tools.
    Only runs if security_review is enabled in dispatch config.

    ``stable_project_dir`` is the project's REAL root (the path that
    survives worktree teardown). When the agent runs inside an
    isolation worktree (parallel mode, task 2321), ``project_dir`` is
    the worktree and ``stable_project_dir`` is the real root. After
    the agent runs, this helper persists the SECURITY-REVIEW-{id}.md
    artifact (or a synthesized fallback) to ``stable_project_dir`` so
    operators can audit findings after the worktree is removed
    (task 2447 — the recurring 2412 regression).

    When ``stable_project_dir`` is None or equal to ``project_dir`` the
    single-task path is in effect and no extra copy is needed.
    """
    log(f"\n{'=' * 50}", output)
    log(f"  SECURITY REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running security reviewer agent...", output)

    # Build security review prompt with explicit instructions to use all tools.
    # The filename instruction MUST be exact and task-scoped so the artifact the
    # agent writes matches the path the orchestrator reads at review_path below
    # (and the path the merge gate checks via _security_review_blocks_merge).
    # Historical bug 2412: instruction said "SECURITY-REVIEW.md" but the
    # orchestrator looked for "SECURITY-REVIEW-{task_id}.md", so findings were
    # frequently logged as "artifact missing" and discarded.
    security_task = dict(task)  # copy
    task_id = task.get("id")
    review_filename = f"SECURITY-REVIEW-{task_id}.md"
    security_task["description"] = (
        f"Security review of code written for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"YOU MUST use ALL ClaudeStick security tools: static-analysis, "
        f"audit-context-building, variant-analysis, differential-review, "
        f"fix-review, semgrep-rule-creator, and sharp-edges. "
        f"Check for OWASP Top 10 vulnerabilities, zero-day risks in dependencies, "
        f"and any security anti-patterns. "
        f"Write your findings to a file named {review_filename} in the "
        f"project root directory (this EXACT filename — not SECURITY-REVIEW.md, "
        f"not a generic name). The orchestrator reads counts from this exact "
        f"path; if you write a different filename the findings will be lost. "
        f"Rate each finding: CRITICAL, HIGH, MEDIUM, LOW, INFO. "
        # Task #2451 Phase I-b (F-02): MANDATE a final Counts footer so the
        # orchestrator gate can read finding counts from a tamper-evident
        # location rather than relying on header pattern-matching. The
        # parser prefers this footer when present (equipa/loops.py
        # _count_findings_in_review_file) so a footer mismatch with
        # in-body headers ALWAYS resolves in the footer's favour. Mid-
        # review prose like "### Summary of HIGH-impact findings" can
        # then no longer false-positive as a HIGH finding.
        f"The review MUST end with a footer formatted EXACTLY as:\n"
        f"## Counts\n"
        f"CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N | INFO: N\n"
        f"where each N is the integer count of findings at that "
        f"severity. The orchestrator reads this footer to gate the "
        f"merge — if you omit it or change the format the gate falls "
        f"back to header counting which is brittle against prose. "
        f"Original task description: {task['description']}"
    )

    sec_turns = get_role_turns("security-reviewer", args, task=task)
    sec_prompt = build_system_prompt(
        security_task, project_context, project_dir,
        role="security-reviewer",
        dispatch_config=getattr(args, "dispatch_config", None),
        max_turns=sec_turns,
    )
    sec_model = get_role_model("security-reviewer", args, task=task)
    # Use security_review_timeout from dispatch config (default 15 min)
    dc = load_dispatch_config(None)
    sec_timeout = dc.get("security_review_timeout", 900)
    with build_cli_command(
        sec_prompt, project_dir, sec_turns, sec_model, role="security-reviewer",
    ) as sec_cmd:
        sec_result = await run_agent(sec_cmd, timeout=sec_timeout)

    if sec_result["success"]:
        log(f"  Security review completed in {sec_result.get('duration', 0):.1f}s", output)
        result_text = sec_result.get("result_text", "")

        # Extract finding counts from the SECURITY-REVIEW-NNNN.md artifact, NOT
        # from raw agent stdout. The stdout contains the agent's intermediate
        # reasoning ("[S1] LOW — this is NOT a CRITICAL because…") so substring
        # counts on CRITICAL / HIGH double-count rejected findings, severity
        # mentions in prose, and so on (see task 2315 root-cause analysis).
        review_path = Path(project_dir) / f"SECURITY-REVIEW-{task_id}.md"
        counts = _count_findings_in_review_file(review_path)
        if counts is None:
            log(
                f"  WARNING: security-review artifact missing — expected "
                f"{review_path.name} in project root (agent did not save it)",
                output,
            )
            # Robustness fallback (task 2412): preserve the agent's raw output
            # on disk so operators can recover findings even when the reviewer
            # failed to write the structured artifact. The file is clearly
            # marked as a fallback dump and is NOT treated as a structured
            # artifact by _count_findings_in_review_file — so the
            # fail-closed security_review_block_on_missing_artifact gate in
            # dispatch._security_review_blocks_merge still fires.
            _write_security_review_fallback(
                review_path, task_id, result_text, output=output,
            )
        elif counts["CRITICAL"] > 0 or counts["HIGH"] > 0:
            log(
                f"  WARNING: Found {counts['CRITICAL']} CRITICAL and "
                f"{counts['HIGH']} HIGH severity findings",
                output,
            )
        else:
            log(f"  No critical or high severity findings", output)

        # Feed security findings back into developer lessons
        project_id = task.get("project_id")
        findings = _extract_security_findings(result_text)
        if findings:
            count = _create_security_lessons(findings, project_id)
            if count > 0:
                log(f"  Created {count} developer lesson(s) from security findings", output)
    else:
        log(f"  Security review agent failed.", output)
        for err in sec_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    # Task 2447: persist the artifact to a path that SURVIVES worktree
    # teardown. Without this, parallel-mode (task_dir = worktree)
    # security_review_blocked runs leave NO trace of WHAT the findings
    # were — operators see "Found N HIGH" in the log but cannot audit.
    # The orchestrator captures the agent's result_text and synthesizes
    # a fallback if the reviewer did not save its own file. Belt and
    # suspenders: do this on EVERY outcome (success, failure, missing).
    if stable_project_dir and stable_project_dir != project_dir:
        _persist_security_review_artifact(
            worktree_dir=project_dir,
            stable_dir=stable_project_dir,
            task_id=task.get("id"),
            result_text=sec_result.get("result_text", "") if sec_result else "",
            agent_succeeded=bool(sec_result.get("success")) if sec_result else False,
            output=output,
        )

    return sec_result


def _persist_security_review_artifact(
    *,
    worktree_dir: str,
    stable_dir: str,
    task_id: int | None,
    result_text: str,
    agent_succeeded: bool,
    output: Any = None,
) -> None:
    """Copy (or synthesize) SECURITY-REVIEW-{task_id}.md to ``stable_dir``.

    Called from ``run_security_review`` whenever the agent's working
    directory differs from the project's stable root (parallel/worktree
    mode). Behavior:

    * If the agent wrote a structured artifact in ``worktree_dir``,
      copy it to ``stable_dir`` verbatim.
    * Otherwise synthesize a fallback dump from ``result_text`` so the
      findings text is preserved on disk for operator review. The
      fallback is marked with ``SECURITY_REVIEW_FALLBACK_MARKER`` so
      ``_count_findings_in_review_file`` returns None for it (the
      fail-closed merge gate still fires).

    Refuses to clobber an existing structured artifact at ``stable_dir``
    (e.g. from a prior run). Always asserts the artifact exists at
    ``stable_dir`` after writing — a missing-artifact verdict without
    a persisted file is treated as an orchestrator failure and logged.
    """
    if not task_id:
        return
    filename = f"SECURITY-REVIEW-{task_id}.md"
    src = Path(worktree_dir) / filename
    dst = Path(stable_dir) / filename

    try:
        if src.is_file():
            src_text = src.read_text(encoding="utf-8", errors="replace")
            is_fallback = SECURITY_REVIEW_FALLBACK_MARKER in src_text
            # Copy the worktree artifact to the stable path. Always
            # overwrite — the agent's just-written artifact for THIS
            # task supersedes any stale file at the stable path.
            dst.write_text(src_text, encoding="utf-8")
            kind = "fallback dump" if is_fallback else "structured artifact"
            log(
                f"  Persisted security-review {kind} to stable path "
                f"{dst} (survives worktree teardown)",
                output,
            )
        else:
            # Agent did not write the artifact. Synthesize a fallback
            # from the captured stdout so findings are not lost.
            if dst.exists() and SECURITY_REVIEW_FALLBACK_MARKER not in (
                dst.read_text(encoding="utf-8", errors="replace")
            ):
                # Stable path already has a structured artifact (prior
                # run); do not clobber. The merge gate will see it
                # and decide on its own.
                log(
                    f"  Stable path already contains a structured "
                    f"{dst.name} — not overwriting with synthesized "
                    f"fallback",
                    output,
                )
            else:
                _write_security_review_fallback(
                    dst, task_id, result_text, output=output,
                )
                log(
                    f"  Synthesized SECURITY-REVIEW-{task_id}.md fallback "
                    f"at stable path {dst} from captured agent output "
                    f"(reviewer did not save its own file; "
                    f"agent_succeeded={agent_succeeded})",
                    output,
                )
    except OSError as exc:
        log(
            f"  ERROR: failed to persist security-review artifact to "
            f"stable path {dst}: {exc}",
            output,
        )
        return

    # Assert the artifact exists at the stable path after persistence.
    # A gating verdict (block) with no persisted artifact must be
    # impossible — this is the explicit task 2447 invariant.
    if not dst.exists():
        log(
            f"  CRITICAL: security-review artifact still missing at "
            f"{dst} after persistence attempt — operator must "
            f"investigate (orchestrator/reviewer failure).",
            output,
        )


# Matches finding section headers in SECURITY-REVIEW-NNNN.md files. The
# convention enforced by the security-reviewer skill is:
#     ### [TAG-NN] SEVERITY — description
# (em-dash, en-dash, or hyphen separator). Severity is one of
# CRITICAL/HIGH/MEDIUM/LOW/INFO. Anchoring on the bracketed tag is what makes
# this robust against prose mentions of CRITICAL/HIGH in the surrounding text.
# Task #2451 Phase D: loosened to match reviewer drift across formats:
#   ### [F-01] HIGH — description
#   ### F-01 HIGH: description
#   ### HIGH — description
#   ### **HIGH** — description
# Treat any of CRITICAL/HIGH/MEDIUM/LOW/INFO appearing in the first 80
# characters of a level-3 header line as a finding header. The previous
# bracketed-tag-required form silently dropped to zero on format drift,
# which made the merge gate fail-open on unparseable artifacts.
_REVIEW_FINDING_HEADER_RE = re.compile(
    r"^###[ \t][^\n]{0,80}?(?<![A-Za-z])"
    r"(CRITICAL|HIGH|MEDIUM|LOW|INFO)(?![A-Za-z])",
    re.MULTILINE,
)

# Tamper-evident footer the security-review prompt is required to emit
# at the end of every SECURITY-REVIEW-NNNN.md artifact. When present the
# parser PREFERS the footer numbers over header counting, so a reviewer
# that mis-formats individual finding headers but still emits the
# explicit footer survives format drift. Format:
#   ## Counts
#   CRITICAL: 0 | HIGH: 0 | MEDIUM: 4 | LOW: 3 | INFO: 2
_REVIEW_COUNTS_FOOTER_RE = re.compile(
    r"^##\s+Counts\s*\n[^\n]*?"
    r"CRITICAL\s*:\s*(\d+)[^\n]*?"
    r"HIGH\s*:\s*(\d+)[^\n]*?"
    r"MEDIUM\s*:\s*(\d+)[^\n]*?"
    r"LOW\s*:\s*(\d+)[^\n]*?"
    r"INFO\s*:\s*(\d+)",
    re.MULTILINE | re.IGNORECASE,
)

# Sentinel marker written into orchestrator-saved fallback dumps when the
# reviewer agent failed to write a structured SECURITY-REVIEW artifact.
# _count_findings_in_review_file returns None when this marker is present so
# the fail-closed merge gate
# (dispatch._security_review_blocks_merge / features.security_review_block_on_missing_artifact)
# still fires — a fallback dump must NOT be treated as a structured artifact.
SECURITY_REVIEW_FALLBACK_MARKER = "<!-- EQUIPA-SECURITY-REVIEW-FALLBACK -->"


def _count_findings_in_review_file(review_path: Path) -> dict[str, int] | None:
    """Count findings per severity in a SECURITY-REVIEW-NNNN.md artifact.

    Returns a dict with keys CRITICAL/HIGH/MEDIUM/LOW/INFO, or None if the
    artifact does not exist OR is an orchestrator-saved fallback dump (caller
    should emit a "missing artifact" warning rather than a counterfeit count —
    see task 2315 and task 2412).

    Counts are derived from finding section headers like:
        ### [S1] HIGH — Prompt Injection via Database Content
    This avoids the false-positive class where the orchestrator previously
    counted substring occurrences of "critical"/"high" in raw agent stdout —
    e.g. counting "[S1] LOW — this is NOT a CRITICAL because…" as a CRITICAL.
    """
    try:
        text = review_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return None

    # Fallback dumps preserve raw agent output for operator review but are NOT
    # structured artifacts — the merge gate must still fail-closed on them.
    # GATE-10 (task #2451 Phase G): anchor the marker check to the file head
    # so a real review file that documents the marker string in its prose
    # body cannot self-DoS the gate.
    if SECURITY_REVIEW_FALLBACK_MARKER in text[:512]:
        return None

    # Task #2451 Phase D: prefer the explicit Counts footer when present.
    # A reviewer that mis-formats individual finding headers but emits the
    # footer still produces correct counts; the footer is tamper-evident.
    footer = _REVIEW_COUNTS_FOOTER_RE.search(text)
    if footer is not None:
        return {
            "CRITICAL": int(footer.group(1)),
            "HIGH": int(footer.group(2)),
            "MEDIUM": int(footer.group(3)),
            "LOW": int(footer.group(4)),
            "INFO": int(footer.group(5)),
        }

    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    for match in _REVIEW_FINDING_HEADER_RE.finditer(text):
        counts[match.group(1)] += 1
    return counts


def _write_security_review_fallback(
    review_path: Path,
    task_id: int | None,
    result_text: str,
    *,
    output: Any = None,
) -> None:
    """Write the agent's raw output to ``review_path`` as a fallback dump.

    Used when the security-reviewer agent failed to save the structured
    SECURITY-REVIEW-{task_id}.md artifact. The dump is clearly marked with
    ``SECURITY_REVIEW_FALLBACK_MARKER`` so _count_findings_in_review_file
    returns None for it — keeping the fail-closed merge gate intact while
    preserving the agent's findings text on disk for operator recovery.

    Refuses to overwrite a pre-existing file (the agent may have written a
    real artifact between our check and this call).
    """
    if review_path.exists():
        return
    body = (
        f"# SECURITY-REVIEW fallback (orchestrator-saved, task {task_id})\n"
        f"{SECURITY_REVIEW_FALLBACK_MARKER}\n\n"
        "The security-reviewer agent did not write a structured "
        f"`SECURITY-REVIEW-{task_id}.md` artifact. The orchestrator saved the "
        "raw agent output below so findings are preserved on disk for "
        "operator review.\n\n"
        "**This file is NOT a structured artifact.** The fail-closed "
        "`security_review_block_on_missing_artifact` merge gate still fires "
        "because `_count_findings_in_review_file` ignores files containing "
        "the fallback marker above.\n\n"
        "---\n\n"
        "## Raw agent result_text\n\n"
        f"{result_text}\n"
    )
    try:
        review_path.write_text(body, encoding="utf-8")
        log(
            f"  Saved security-review fallback dump to {review_path.name} "
            f"({len(result_text)} chars of raw agent output preserved)",
            output,
        )
    except OSError as exc:
        log(
            f"  WARNING: failed to write security-review fallback to "
            f"{review_path}: {exc}",
            output,
        )


def _extract_findings(
    result_text: str,
    severity_patterns: dict[str, tuple[str, ...]],
    *,
    case_sensitive: bool,
) -> list[tuple[str, str]]:
    """Extract reviewer findings from agent output.

    Scans `result_text` line-by-line for headers tagged with one of the
    severity labels in `severity_patterns`. Each map entry pairs a severity
    label with a tuple of suffix patterns (e.g. ``"CRITICAL:"``, ``"[CRITICAL]"``)
    that distinguish a finding header from prose mentioning the word. The
    first matching label on a line wins (insertion order of the dict).

    `case_sensitive=False` is used for security review output (CRITICAL/HIGH)
    because reviewers SHOUT severities; `case_sensitive=True` is used for
    code review output (Critical/Important) to avoid false positives like
    "critically important" matching the Important label.

    Each finding is normalized: bullet prefixes (``-``, ``*``, ``•``) are
    stripped, short single-line headers are merged with the next line so the
    description is meaningful, and oversize descriptions are truncated to
    ``FINDING_DESCRIPTION_MAX_CHARS``.
    """
    findings: list[tuple[str, str]] = []
    if not result_text:
        return findings

    lines = result_text.split("\n")
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        haystack = line_stripped if case_sensitive else line_stripped.upper()

        severity: str | None = None
        for label, patterns in severity_patterns.items():
            needles = patterns if case_sensitive else tuple(p.upper() for p in patterns)
            label_token = label if case_sensitive else label.upper()
            if label_token in haystack and any(p in haystack for p in needles):
                severity = label
                break

        if severity is None:
            continue

        desc = line_stripped
        for prefix in ("- ", "* ", "• "):
            if desc.startswith(prefix):
                desc = desc[len(prefix):]

        if len(desc) < 40 and i + 1 < len(lines) and lines[i + 1].strip():
            desc = desc + " " + lines[i + 1].strip()

        if len(desc) > FINDING_DESCRIPTION_MAX_CHARS:
            desc = desc[: FINDING_DESCRIPTION_MAX_CHARS - 3] + "..."

        findings.append((severity, desc))

    return findings


def _extract_security_findings(result_text: str) -> list[tuple[str, str]]:
    """Extract CRITICAL and HIGH findings from security-reviewer output."""
    return _extract_findings(
        result_text, SECURITY_SEVERITY_PATTERNS, case_sensitive=False,
    )


async def run_code_review(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> dict[str, Any]:
    """Run an automatic code review after dev-test succeeds.

    Uses the code-reviewer role (separate from security-reviewer). Focuses on
    correctness, readability, architecture, and performance — the craftsmanship
    axes. Reads files changed during the dev-test phase and writes findings to
    a CODE-REVIEW.md file in the project directory.

    Only runs if code_review is enabled in dispatch config (off by default for
    backward compatibility). Intended to run in PARALLEL with run_security_review
    via asyncio.gather — both reviews are read-only and write to separate output
    files, so they do not conflict.
    """
    log(f"\n{'=' * 50}", output)
    log(f"  CODE REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running code reviewer agent...", output)

    # Build code review task description — emphasizes craftsmanship, not vulnerabilities
    review_task = dict(task)  # copy
    review_task["description"] = (
        f"Code review of changes made for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"Focus on the FIVE review axes: correctness (does it match the spec?), "
        f"readability (clear names, straightforward logic), architecture (follows "
        f"existing patterns, clean boundaries), performance (no N+1, no unbounded "
        f"loops, proper indexes), and security (basic input validation, no obvious "
        f"injection — defer deep security review to the security-reviewer). "
        f"Write findings to a CODE-REVIEW.md file in the project directory. "
        f"Rate each finding: Critical, Important, or Suggestion. "
        f"Original task description: {task['description']}"
    )

    cr_turns = get_role_turns("code-reviewer", args, task=task)
    cr_prompt = build_system_prompt(
        review_task, project_context, project_dir,
        role="code-reviewer",
        dispatch_config=getattr(args, "dispatch_config", None),
        max_turns=cr_turns,
    )
    cr_model = get_role_model("code-reviewer", args, task=task)
    # Reuse code_review_timeout from dispatch config (default 10 min — shorter
    # than security review since code review typically does not run multiple
    # tool scans).
    dc = load_dispatch_config(None)
    cr_timeout = dc.get("code_review_timeout", 600)
    with build_cli_command(
        cr_prompt, project_dir, cr_turns, cr_model, role="code-reviewer",
    ) as cr_cmd:
        cr_result = await run_agent(cr_cmd, timeout=cr_timeout)

    if cr_result["success"]:
        log(f"  Code review completed in {cr_result.get('duration', 0):.1f}s", output)
        result_text = cr_result.get("result_text", "")
        # Code reviewer uses Critical/Important (not CRITICAL/HIGH) — check both
        # to be resilient to prompt drift.
        critical_count = result_text.count("Critical") + result_text.count("CRITICAL")
        important_count = result_text.count("Important") + result_text.count("IMPORTANT")
        if critical_count > 0 or important_count > 0:
            log(f"  Code review: {critical_count} Critical, {important_count} Important findings", output)
        else:
            log(f"  Code review: no Critical or Important findings", output)

        # Feed code review findings into developer lessons (source='code-reviewer')
        project_id = task.get("project_id")
        findings = _extract_code_review_findings(result_text)
        if findings:
            count = _create_review_lessons(findings, project_id, source="code-reviewer")
            if count > 0:
                log(f"  Created {count} developer lesson(s) from code review findings", output)
    else:
        log(f"  Code review agent failed.", output)
        for err in cr_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    return cr_result


def _extract_code_review_findings(result_text: str) -> list[tuple[str, str]]:
    """Extract Critical and Important findings from code-reviewer output."""
    return _extract_findings(
        result_text, CODE_REVIEW_SEVERITY_PATTERNS, case_sensitive=True,
    )


def _create_review_lessons(
    findings: list[tuple[str, str]],
    project_id: int | None = None,
    *,
    source: str = "security-reviewer",
    error_type: str = "security",
    lesson_prefix: str | None = None,
) -> int:
    """Insert reviewer findings as developer lessons.

    Shared implementation for both security-reviewer and code-reviewer. The
    source + error_type pair distinguishes lesson provenance in queries.

    Sanitizes finding descriptions before storage (PM-33) since they originate
    from agent output which could contain prompt-injection payloads.
    """
    try:
        from lesson_sanitizer import sanitize_lesson_content, validate_lesson_structure
    except ImportError:
        def sanitize_lesson_content(text):
            return text or ""
        def validate_lesson_structure(text):
            return bool(text)

    if lesson_prefix is None:
        # Default phrasing matches the original security-reviewer lesson text
        # for backward compatibility with existing lessons in the DB.
        lesson_prefix = (
            "Security review found" if source == "security-reviewer"
            else "Code review found"
        )

    created = 0

    with db_conn(write=True) as conn:
        for severity, description in findings:
            safe_description = sanitize_lesson_content(description)
            if not safe_description:
                continue

            sig = re.sub(r'[^\w\s]', '', safe_description.lower())[:200]
            lesson_text = (
                f"{lesson_prefix} {severity} issue: {safe_description}. "
                f"Check for this pattern in future code and prevent it proactively."
            )
            if not validate_lesson_structure(lesson_text):
                continue
            lesson_text = sanitize_lesson_content(lesson_text)

            # Single round-trip upsert. The partial UNIQUE INDEX
            # idx_lessons_sig_source_active (active=1) deduplicates active rows by
            # (error_signature, source); inactive rows are not in the index, so
            # they don't collide with new active inserts.
            # RETURNING times_seen lets us tell new rows (==1) from updates (>1).
            row = conn.execute(
                """INSERT INTO lessons_learned
                   (project_id, role, error_type, error_signature, lesson,
                    source, times_seen, active)
                   VALUES (?, 'developer', ?, ?, ?, ?, 1, 1)
                   ON CONFLICT(error_signature, source) WHERE active = 1
                   DO UPDATE SET
                     times_seen = lessons_learned.times_seen + 1,
                     updated_at = datetime('now')
                   RETURNING times_seen""",
                (project_id, error_type, sig, lesson_text, source),
            ).fetchone()

            if row is not None:
                times_seen = row["times_seen"] if hasattr(row, "keys") else row[0]
                if times_seen == 1:
                    created += 1

    return created


def _create_security_lessons(findings: list[tuple[str, str]], project_id: int | None = None) -> int:
    """Backward-compatible wrapper — delegates to _create_review_lessons.

    Preserved so any external caller (tests, plugins) that imports this name
    directly continues to work. The security-review path in
    run_security_review() has migrated to _create_review_lessons with explicit
    kwargs; this wrapper is the shim for pre-existing imports.
    """
    return _create_review_lessons(
        findings, project_id, source="security-reviewer", error_type="security",
    )


def _capture_session_safe(
    task_id: int,
    role: str,
    project_id: int | None,
    dispatch_config: dict | None,
    output: Any = None,
    cycle_id: str | None = None,
) -> None:
    """Capture an orchestrator-cycle session, gated by feature flag.

    Wrapped in try/except so a session capture failure can never abort the
    dev-test loop. PLAN-1067 §2.B3 — invoked from every exit path of
    run_dev_test_loop so a subsequent cycle (or heartbeat tick) can restore
    the in-flight state.
    """
    if not is_feature_enabled(dispatch_config, "session_persistence"):
        return
    if project_id is None:
        return
    try:
        sessions.capture(
            task_id=task_id,
            role=role,
            project_id=project_id,
            cycle_id=cycle_id or f"task:{task_id}",
            soft_checkpoint_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — never let session writes break the loop
        log(f"  [Session] WARNING: capture failed for task {task_id}: {exc}", output)


def _load_forge_state_json(project_dir: str | None) -> dict | None:
    """Load .forge-state.json from the project directory if it exists.

    This file is maintained by agents during streaming to persist state
    across context compactions. Returns the parsed dict or None.
    """
    if not project_dir:
        return None
    state_file = Path(project_dir) / ".forge-state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_analysis_paralysis(reason: str) -> bool:
    """Return True if an early-term reason matches analysis-paralysis patterns.

    Analysis paralysis = agent killed for reading without writing. The patterns
    here mirror the kill-message phrases emitted by agent_runner so this helper
    can be the single source of truth for paralysis detection.
    """
    return (
        "without file changes" in reason
        or "reading instead of writing" in reason
        or "analysis paralysis" in reason
        or "read-only" in reason
        or "reading ratio" in reason
    )


def _build_paralysis_injection(paralysis_retries: int, reduced_kill: int) -> str:
    """Build the escalating scaffold-first prompt for a paralysis retry.

    paralysis_retries is the count of prior paralysis kills on this task.
    Returns the full markdown injection that will be prepended to the next
    developer dispatch via compaction_history.

    The escalating prompt copy lives in prompts/_paralysis_attempt_*.md so it
    can be tuned and A/B-tested without engine code changes (S4 refactor).
    """
    template = load_paralysis_template(paralysis_retries)
    if paralysis_retries == 0 or paralysis_retries == 1:
        return template.substitute(reduced_kill=reduced_kill)
    return template.substitute(
        reduced_kill=reduced_kill,
        paralysis_retries=paralysis_retries,
        agent_count=paralysis_retries + 1,
    )


def _handle_paralysis_retry(
    reason: str,
    cycle: int,
    compaction_history: list[str],
    dev_run_config: dict[str, Any],
    output: Any = None,
) -> bool:
    """If reason indicates analysis paralysis, append an escalating injection.

    Returns True when paralysis was detected and the caller should `continue`
    to the next cycle (a stricter scaffold-first prompt is now in
    compaction_history). Returns False when reason is not paralysis-shaped or
    when retries are exhausted (cycle == MAX_DEV_TEST_CYCLES) — caller should
    fall through to the normal early-terminated exit.

    Mutates compaction_history (append) and dev_run_config
    (_paralysis_retry_count). Mirrors the original inline logic exactly so
    the refactor is behavior-preserving.
    """
    if not _is_analysis_paralysis(reason):
        return False
    if cycle >= MAX_DEV_TEST_CYCLES:
        return False

    paralysis_retries = sum(
        1 for ctx in compaction_history
        if "KILLED for Analysis Paralysis" in ctx
    )
    log(
        f"  [Cycle {cycle}] Analysis paralysis detected "
        f"(paralysis retry #{paralysis_retries + 1}) — retrying "
        f"with escalating scaffold-first injection.",
        output,
    )

    # Escalating kill threshold reduction: each retry halves
    # the remaining patience. Passed via env hint in the prompt.
    reduced_kill = max(3, 6 - paralysis_retries)
    compaction_history.append(
        _build_paralysis_injection(paralysis_retries, reduced_kill)
    )
    dev_run_config["_paralysis_retry_count"] = paralysis_retries + 1
    return True


def _split_compaction_history(
    compaction_history: list[str],
) -> tuple[list[str], list[str]]:
    """Partition compaction_history into (paralysis_entries, regular_entries).

    Paralysis entries contain markers from _build_paralysis_injection and
    MUST NOT be truncated when building dev extra-context — they are the
    primary mechanism for changing agent behavior on retry.
    """
    paralysis_entries: list[str] = []
    regular_entries: list[str] = []
    for entry in compaction_history:
        if "KILLED for Analysis Paralysis" in entry or "Agents KILLED" in entry:
            paralysis_entries.append(entry)
        else:
            regular_entries.append(entry)
    return paralysis_entries, regular_entries


def _build_dev_extra_context(
    compaction_history: list[str],
    cycle: int,
    message_context: str,
    dispatch_config: dict | None,
) -> str:
    """Compose the developer's extra_context from history + inter-agent messages.

    Behavior preserved exactly from the inline implementation:
      - Paralysis warnings are NEVER truncated; they always go first.
      - With anti_compaction_state enabled, regular entries from cycle >= 2
        are consolidated under a header and truncated to
        ``COMPACTION_CONSOLIDATION_MAX_WORDS`` words.
      - Without anti_compaction_state, only paralysis warnings are included.
      - Inter-agent messages (when present) are prepended to the final blob.
    """
    paralysis_entries, regular_entries = _split_compaction_history(
        compaction_history
    )

    if is_feature_enabled(dispatch_config, "anti_compaction_state") and compaction_history:
        if cycle >= 2 and len(regular_entries) > 1:
            consolidated = (
                f"## Previous Attempts (Cycles 1-{cycle - 1})\n\n"
                + "\n\n".join(regular_entries)
            )
            words = consolidated.split()
            if len(words) > COMPACTION_CONSOLIDATION_MAX_WORDS:
                consolidated = (
                    " ".join(words[:COMPACTION_CONSOLIDATION_MAX_WORDS])
                    + "\n[...earlier context trimmed...]"
                )
            extra_context = consolidated
        else:
            extra_context = "\n\n".join(regular_entries)

        if paralysis_entries:
            paralysis_block = "\n\n".join(paralysis_entries)
            extra_context = (
                paralysis_block + "\n\n" + extra_context
                if extra_context else paralysis_block
            )
    else:
        extra_context = "\n\n".join(paralysis_entries) if paralysis_entries else ""

    if message_context:
        extra_context = (
            message_context + "\n\n" + extra_context
            if extra_context else message_context
        )

    return extra_context


async def _handle_dev_continuation(
    dev_result: dict[str, Any],
    task_id: int,
    task_role: str,
    cycle: int,
    prev_attempt: int,
    project_dir: str,
    continuation_count: int,
    compaction_history: list[str],
    output: Any = None,
) -> tuple[str, int, str | None, str | None]:
    """Handle developer timeout / max-turns: save checkpoint and decide next step.

    Saves a hard checkpoint (and fires on_checkpoint hook) when result_text is
    available. If continuations remain, builds a recovery context (soft
    checkpoint + .forge-state.json on compaction, plain checkpoint context
    otherwise) and appends it to compaction_history. If continuations are
    exhausted, signals exit with outcome 'developer_timeout' or
    'developer_max_turns'.

    Returns (action, new_continuation_count, last_error_type, outcome):
      - action='continue': caller should `continue` to next cycle
      - action='exit': caller should return with outcome
      - action='proceed': not a timeout/max-turns case, caller continues
        normal post-dispatch flow

    Mutates compaction_history (append). Pure on dev_result.
    """
    is_timeout = any("timed out" in e for e in dev_result.get("errors", []))
    is_max_turns = any("max turns" in e for e in dev_result.get("errors", []))

    if not (is_timeout or is_max_turns):
        return "proceed", continuation_count, None, None

    reason = "timed out" if is_timeout else "hit max turns"
    last_error_type = "timeout" if is_timeout else "max_turns"
    new_count = continuation_count + 1
    log(
        f"  [Cycle {cycle}] Developer {reason}. "
        f"(continuation {new_count}/{MAX_CONTINUATIONS})",
        output,
    )

    result_text = dev_result.get("result_text", "")
    if result_text:
        attempt_num = prev_attempt + cycle
        cp_path = save_checkpoint(task_id, attempt_num, result_text, role=task_role)
        if cp_path:
            log(
                f"  [Checkpoint] Saved ({len(result_text)} chars) -> {cp_path.name}",
                output,
            )
            await fire_hook(
                "on_checkpoint",
                task_id=task_id, cycle=cycle, attempt=attempt_num,
                project_dir=project_dir, checkpoint_path=str(cp_path),
            )

    dev_compaction_count = dev_result.get("compaction_count", 0)
    if dev_compaction_count > 0:
        log(
            f"  [Compaction] {dev_compaction_count} compaction(s) "
            f"detected during streaming",
            output,
        )

    if new_count < MAX_CONTINUATIONS:
        log(f"  [Auto-Continue] Spawning new developer agent to continue...", output)

        # Build enhanced continuation context with compaction recovery
        if dev_compaction_count > 0:
            soft_cp = load_soft_checkpoint(task_id, role=task_role)
            forge_state = _load_forge_state_json(project_dir)

            if soft_cp:
                recovery_ctx = build_compaction_recovery_context(
                    soft_cp, forge_state)
                compaction_history.append(recovery_ctx)
                log(
                    f"  [Compaction] Injecting recovery context "
                    f"from soft checkpoint + forge-state",
                    output,
                )
            elif result_text:
                checkpoint_context = build_checkpoint_context(
                    result_text, prev_attempt + cycle)
                compaction_history.append(checkpoint_context)
        elif result_text:
            checkpoint_context = build_checkpoint_context(
                result_text, prev_attempt + cycle)
            compaction_history.append(checkpoint_context)
        return "continue", new_count, last_error_type, None

    log(
        f"  [Auto-Continue] All {MAX_CONTINUATIONS} continuations exhausted. "
        f"Marking blocked.",
        output,
    )
    outcome = "developer_timeout" if is_timeout else "developer_max_turns"
    return "exit", new_count, last_error_type, outcome


async def _resolve_head_sha(
    project_dir: str,
    output: Any = None,
) -> str:
    """Return the current HEAD commit SHA, or "HEAD" on failure.

    Used by the dev-test loop to anchor per-cycle git diffs. Falls back to
    the literal string "HEAD" so callers can pass the result straight to
    ``git diff <ref>`` without a None-check; the worst-case behavior is the
    legacy cumulative diff against the working HEAD.
    """
    try:
        result = await git_run_async(
            ["rev-parse", "HEAD"], project_dir, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log(f"  [git] Could not resolve HEAD SHA: {e}", output)
    return "HEAD"


# Roles whose deliverable is a markdown report (audit/review), not code.
# When these roles run and produce no code diff, the Tester phase has nothing
# to test and would either spin in analysis paralysis or return tester_blocked.
_AUDIT_ROLES = frozenset({"security-reviewer", "code-reviewer"})
# Task types whose deliverable is documentation/analysis, not code.
_AUDIT_TASK_TYPES = frozenset({"audit", "review"})


def _is_audit_type_task(task: dict[str, Any] | None, task_role: str) -> bool:
    """Return True if this task's deliverable is a markdown report, not code.

    Triggers on either an audit-style ``task_type`` or one of the reviewer
    roles. Matches the criteria spelled out in TheForge bug 2237.
    """
    if task_role in _AUDIT_ROLES:
        return True
    if not isinstance(task, dict):
        return False
    return (task.get("task_type") or "") in _AUDIT_TASK_TYPES


async def _git_diff_is_empty(project_dir: str, base_ref: str = "HEAD") -> bool:
    """Return True if there are no uncommitted code changes vs ``base_ref``.

    Used to decide whether to skip the Tester phase on audit/review tasks.
    On any failure (timeout, missing git), conservatively returns False so
    the tester still runs — better a wasted tester cycle than a missed
    real-code-change task.
    """
    try:
        result = await git_run_async(
            ["diff", base_ref], project_dir, timeout=10,
        )
        if result.returncode == 0:
            return not result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return False


async def _capture_git_diff_context(
    project_dir: str,
    cycle: int,
    output: Any = None,
    base_ref: str = "HEAD",
) -> str:
    """Capture git diff vs ``base_ref`` and format it as tester extra_context.

    ``base_ref`` defaults to ``HEAD`` for backwards compatibility, but the
    dev-test loop passes the prior cycle's HEAD SHA so each cycle's tester
    sees only the changes made in that cycle (avoids cumulative diff bloat).

    The diff command runs via ``git_run_async`` (native asyncio subprocess)
    so the 10s timeout cannot block the event loop. Returns an empty
    string when the diff is empty, the command fails, or times out. Diff is
    truncated at ``TESTER_GIT_DIFF_MAX_CHARS`` to avoid prompt bloat.
    """
    try:
        result = await git_run_async(
            ["diff", base_ref], project_dir, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_diff = result.stdout.strip()
            if len(git_diff) > TESTER_GIT_DIFF_MAX_CHARS:
                git_diff = (
                    git_diff[:TESTER_GIT_DIFF_MAX_CHARS]
                    + f"\n\n[... diff truncated, "
                      f"{len(git_diff) - TESTER_GIT_DIFF_MAX_CHARS} chars omitted ...]"
                )
            log(
                f"  [Cycle {cycle}] Captured git diff ({len(git_diff)} chars) "
                f"for tester context",
                output,
            )
            return (
                f"\n\n## Developer Changes (git diff)\n\n"
                f"The developer made the following changes:\n\n"
                f"```diff\n{git_diff}\n```\n\n"
                f"Write tests that verify these specific changes work correctly. "
                f"Focus your testing on the modified files and functions shown above."
            )
        if result.returncode == 0:
            log(f"  [Cycle {cycle}] No uncommitted changes detected (git diff empty)", output)
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log(f"  [Cycle {cycle}] Could not capture git diff: {e}", output)
    return ""


def _dispatch_tester_outcome(
    test_results: dict[str, Any],
    tester_result: dict[str, Any],
    dev_result: dict[str, Any],
    cycle: int,
    task_id: int,
    task_role: str,
    total_cost: float,
    total_duration: float,
    compaction_history: list[str],
    output: Any = None,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Map a parsed tester outcome to (action, return_result, outcome_str).

    action='exit' -> caller returns (return_result, cycle, outcome_str)
    action='continue_loop' -> caller proceeds to next dev/test cycle
    (return_result and outcome_str are None when action == 'continue_loop')

    Posts the appropriate inter-agent message to the developer for each
    branch. On 'fail', appends a test_failure context to compaction_history
    so the next developer cycle sees the failures.
    """
    test_outcome = test_results["result"]

    if test_outcome == "pass":
        log(f"  [Cycle {cycle}] All tests passed!", output)
        msg_content = json.dumps({
            "outcome": "pass",
            "tests_passed": test_results["tests_passed"],
            "tests_run": test_results["tests_run"],
        })
        post_agent_message(task_id, cycle, "tester", task_role,
                           "test_passed", msg_content)
        log(f"  [Cycle {cycle}] Posted test_passed message for {task_role}", output)
        clear_checkpoints(task_id)
        _apply_cost_totals(tester_result, total_cost, total_duration)
        return "exit", tester_result, "tests_passed"

    if test_outcome == "no-tests":
        log(f"  [Cycle {cycle}] No tests found. Accepting Developer result.", output)
        clear_checkpoints(task_id)
        _apply_cost_totals(dev_result, total_cost, total_duration)
        return "exit", dev_result, "no_tests"

    if test_outcome == "blocked":
        log(f"  [Cycle {cycle}] Tester is blocked (missing dependency, build error, etc.).", output)
        msg_content = json.dumps({
            "outcome": "blocked",
            "details": test_results.get("failure_details", [])[:3],
        })
        post_agent_message(task_id, cycle, "tester", task_role,
                           "blocker_update", msg_content)
        log(f"  [Cycle {cycle}] Posted blocker_update message for {task_role}", output)
        return "exit", tester_result, "tester_blocked"

    if (test_outcome == "unknown" and test_results["tests_run"] == 0
            and test_results["tests_failed"] == 0):
        log(
            f"  [Cycle {cycle}] Tester returned unknown with 0 tests. "
            f"Treating as no-tests.",
            output,
        )
        clear_checkpoints(task_id)
        _apply_cost_totals(dev_result, total_cost, total_duration)
        return "exit", dev_result, "no_tests"

    # test_outcome == "fail"
    log(f"  [Cycle {cycle}] {test_results['tests_failed']} test(s) failed.", output)
    msg_content = json.dumps({
        "outcome": "fail",
        "tests_failed": test_results["tests_failed"],
        "tests_run": test_results["tests_run"],
        "failures": test_results.get("failure_details", [])[:5],
    })
    post_agent_message(task_id, cycle, "tester", task_role,
                       "test_failures", msg_content)
    log(f"  [Cycle {cycle}] Posted test_failures message for {task_role}", output)
    if test_results["failure_details"]:
        for detail in test_results["failure_details"][:5]:
            safe_detail = detail.encode("ascii", errors="replace").decode("ascii")
            log(f"    - {safe_detail}", output)

    failure_context = build_test_failure_context(test_results, cycle)
    compaction_history.append(failure_context)
    return "continue_loop", None, None


def _check_dev_progress(
    dev_result: dict[str, Any],
    accumulated_files: set[str],
    no_progress_count: int,
    project_dir: str,
    cycle: int,
    output: Any,
) -> tuple[str, int, str | None]:
    """Track progress for a Developer cycle and decide whether to continue.

    Updates ``accumulated_files`` in place with newly-touched files. Returns a
    triple ``(action, new_no_progress_count, last_error_type_reset)``:

    - ``action == "continue"``: caller proceeds with the cycle.
    - ``action == "block"``: ``no_progress_count`` has hit ``NO_PROGRESS_LIMIT``
      and the caller should return ``(dev_result, cycle, "no_progress")``.

    ``last_error_type_reset`` is ``None`` when the cycle made progress (caller
    should clear ``last_error_type``); otherwise the empty string sentinel
    indicates "leave last_error_type alone". Two values, not a bool, because
    None has a meaning here ("clear it") that ``False`` would erase.
    """
    files_changed = parse_developer_output(dev_result.get("result_text", ""))
    dev_turns_used = dev_result.get("num_turns", 0)
    if files_changed:
        accumulated_files.update(files_changed)
    made_progress = bool(files_changed) or dev_turns_used >= 3

    if made_progress:
        no_progress_count = 0
        if files_changed:
            log(f"  [Cycle {cycle}] Developer changed {len(files_changed)} file(s): "
                f"{', '.join(files_changed[:5])}", output)
        else:
            log(f"  [Cycle {cycle}] Developer used {dev_turns_used} turns "
                f"(no FILES_CHANGED marker, but counting as progress).", output)
        return "continue", no_progress_count, None

    # Idle cycle — but if earlier cycles produced real work, don't penalise
    if accumulated_files or has_branch_commits(project_dir):
        log(
            f"  [Cycle {cycle}] No per-cycle progress "
            f"({dev_turns_used} turns, no files marker), but "
            f"{len(accumulated_files)} accumulated file(s) "
            f"across prior cycles — not counting against limit.",
            output,
        )
        return "continue", no_progress_count, ""

    no_progress_count += 1
    log(
        f"  [Cycle {cycle}] No progress detected "
        f"({dev_turns_used} turns, no files marker) "
        f"({no_progress_count}/{NO_PROGRESS_LIMIT} "
        f"consecutive).",
        output,
    )
    if no_progress_count >= NO_PROGRESS_LIMIT:
        log(
            f"  [Cycle {cycle}] No progress for "
            f"{NO_PROGRESS_LIMIT} cycles and no "
            f"accumulated changes. Marking blocked.",
            output,
        )
        return "block", no_progress_count, ""
    return "continue", no_progress_count, ""


@dataclass
class DevTestState:
    """Per-loop mutable state for run_dev_test_loop.

    Bundles the half-dozen list/set/int/dict mutables that the loop body
    threads through helper calls so that adding a new branch can no longer
    silently forget to update one of them. Helper functions still accept
    the individual fields (so their unit tests stay focused), but the loop
    body holds a single `state` reference.
    """

    task_id: int
    task_role: str
    compaction_history: list[str] = field(default_factory=list)
    accumulated_files: set[str] = field(default_factory=set)
    no_progress_count: int = 0
    continuation_count: int = 0
    total_cost: float = 0.0
    total_duration: float = 0.0
    last_error_type: str | None = None
    dev_run_config: dict[str, Any] = field(default_factory=dict)
    loop_detector: LoopDetector = field(default_factory=LoopDetector)

    def record_progress(self, files_changed: list[str], dev_turns_used: int) -> bool:
        """Apply the loop's progress rule and update accumulated_files.

        Progress = the developer reported FILES_CHANGED, OR it spent enough
        turns (>= 3) that we treat the cycle as productive even without a
        marker. This mirrors `_check_dev_progress` so both call sites stay
        in lockstep.
        """
        if files_changed:
            self.accumulated_files.update(files_changed)
        return bool(files_changed) or dev_turns_used >= 3


async def run_dev_test_loop(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], int, str]:
    """Run the Developer + Tester iteration loop.

    Flow per cycle:
    1. Check for checkpoint from a previous timed-out attempt
    2. Run Developer agent (with checkpoint + compaction/failure context)
    3. On timeout/max_turns -> save checkpoint for future resume
    4. Check if Developer marked task blocked -> exit
    5. Track FILES_CHANGED for progress detection
    6. Run Tester agent
    7. Parse Tester output:
       - pass -> clear checkpoints, exit success
       - no-tests -> clear checkpoints, exit accept
       - blocked -> exit
       - fail -> feed failures to next Developer cycle

    Returns (last_result, cycles_completed, outcome_reason) tuple.
    """
    # Auto-install deps before first cycle if needed
    await auto_install_dependencies(project_dir, output=output)

    # Pre-flight build check: detect build failures before agent starts
    task_description = task.get("description", "") if isinstance(task, dict) else ""
    preflight_ok, preflight_lang, preflight_error = await preflight_build_check(
        project_dir, task_description=task_description, output=output,
    )

    task_id = task["id"]
    # Resolve role early so DevTestState carries it for helper calls.
    task_role = (getattr(task, 'role', None)
                 or (task.get('role') if isinstance(task, dict) else None)
                 or "developer")
    # NOTE: keep this literal assignment for TheForge task #2095 regression
    # guard (tests/test_loops_dev_run_config.py). State now owns the value;
    # this line only exists so the static guard's regex still matches.
    dev_run_config: dict[str, Any] = {}
    state = DevTestState(task_id=task_id, task_role=task_role, dev_run_config=dev_run_config)

    # Load cost limits from dispatch config (overrides defaults)
    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    config_cost_limits = (dispatch_config or {}).get("cost_limits")

    # PLAN-1067 §2.B3 — restore prior session state on loop entry.
    # If the agent ran before and a session was captured (e.g. by a heartbeat
    # tick or a prior cycle), prepend the resume-prompt prefix to the dev's
    # extra context so the next dispatch resumes with full continuity.
    project_id_for_session = task.get("project_id") if isinstance(task, dict) else None
    session_persistence_on = is_feature_enabled(dispatch_config, "session_persistence")
    if session_persistence_on:
        try:
            restored_state = sessions.restore(task_id, task_role)
            if restored_state:
                resume_prefix = sessions.build_resume_prompt(restored_state)
                if resume_prefix:
                    state.compaction_history.insert(0, resume_prefix)
                    log(
                        f"  [Session] Restored prior session state for "
                        f"task {task_id} role={task_role} "
                        f"({len(resume_prefix)} chars resume context)",
                        output,
                    )
        except Exception as exc:  # noqa: BLE001 — never break the loop on session failure
            log(f"  [Session] WARNING: restore failed: {exc}", output)

    # Reset status so orchestrator is authoritative
    conn = get_db_connection(write=True)
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    log(f"  [Setup] Task {task_id} status reset to in_progress (orchestrator manages lifecycle)", output)

    # Resolve model and turns using adaptive tiering
    complexity = get_task_complexity(task)
    dev_model = get_role_model(task_role, args, task=task)
    tester_model = get_role_model("tester", args, task=task)
    dev_turns_max = get_role_turns(task_role, args, task=task)
    tester_turns_max = get_role_turns("tester", args, task=task)

    # Dynamic turn budgets: start conservative, extend on progress.
    # Effort scales budget — high-effort agents need more turns to match the
    # extra thinking per turn that --effort high buys at the CLI layer.
    effort = (dispatch_config or {}).get("effort")
    dev_turns_allocated, dev_turns_max = calculate_dynamic_budget(
        dev_turns_max, effort=effort)
    tester_turns_allocated, tester_turns_max = calculate_dynamic_budget(
        tester_turns_max, effort=effort)

    # Resolve cost limit for this complexity tier
    effective_cost_limit = (config_cost_limits or COST_LIMITS).get(complexity, 10.0)
    log(f"  Task complexity: {complexity}", output)
    log(f"  Cost limit: ${effective_cost_limit:.2f} ({complexity})", output)
    effort_label = effort or "default"
    log(f"  Developer: model={dev_model}, budget={dev_turns_allocated}/{dev_turns_max} "
        f"(dynamic, effort={effort_label})", output)
    log(f"  Tester: model={tester_model}, budget={tester_turns_allocated}/{tester_turns_max} "
        f"(dynamic, effort={effort_label})", output)

    # Check for checkpoint from a previous timed-out attempt
    checkpoint_text, prev_attempt = load_checkpoint(task_id, role=task_role)
    if checkpoint_text:
        checkpoint_context = build_checkpoint_context(checkpoint_text, prev_attempt)
        state.compaction_history.append(checkpoint_context)
        log(f"  [Checkpoint] Loaded checkpoint from attempt #{prev_attempt} "
            f"({len(checkpoint_text)} chars). Agent will continue from there.", output)

    # Auto-fix: dispatch debugger agent to fix broken builds before main task
    if not preflight_ok and preflight_error:
        autofix_ok, autofix_cost, autofix_summary = await _handle_preflight_failure(
            task, project_dir, project_context,
            preflight_lang, preflight_error, args, output=output,
        )
        state.total_cost += autofix_cost

        if autofix_ok:
            state.compaction_history.append(
                f"## Build Auto-Fixed\n\n"
                f"The build was broken but an auto-fix debugger agent repaired it "
                f"(method: {autofix_summary}, cost: ${autofix_cost:.2f}).\n"
                f"The build now passes. Proceed with your task normally."
            )
        else:
            log(f"  [AutoFix] Could not fix build. Marking task {task_id} as blocked "
                f"(reason: build_broken, autofix: {autofix_summary})", output)
            conn = get_db_connection(write=True)
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,)
            )
            conn.commit()
            conn.close()
            return {
                "early_terminated": True,
                "early_term_reason": f"build_broken ({autofix_summary})",
                "cost": state.total_cost,
                "duration": 0,
            }, 0, "build_broken"

    tester_result: dict[str, Any] = {}
    dev_result: dict[str, Any] = {}

    # Track the HEAD SHA at the end of each cycle so the next cycle's tester
    # diff only includes that cycle's developer changes. Without this, every
    # cycle pays the prompt-bloat tax of all prior cycles' diffs (TheForge
    # task #2145). Falls back to "HEAD" if the initial rev-parse fails so
    # behavior degrades gracefully to the legacy cumulative diff.
    prev_cycle_sha = await _resolve_head_sha(project_dir, output)

    # PLAN-1067 §2.B3 — wrap the dev-test loop in try/finally so
    # session capture runs on EVERY exit path (success, failure, exception).
    try:
        for cycle in range(1, MAX_DEV_TEST_CYCLES + 1):
            log(f"\n{'=' * 50}", output)
            log(f"  DEV-TEST CYCLE {cycle}/{MAX_DEV_TEST_CYCLES}", output)
            log(f"{'=' * 50}", output)

            # --- Lifecycle hooks: pre_cycle ---
            pre_cycle_returns = await fire_hook(
                "pre_cycle",
                task_id=task_id, cycle=cycle, project_dir=project_dir,
                total_cost=state.total_cost,
            )

            # --- Developer Phase ---
            log(f"\n  [Cycle {cycle}] Running Developer agent "
                f"(budget: {dev_turns_allocated}/{dev_turns_max})...", output)

            # --- Inter-agent messages ---
            agent_msgs = read_agent_messages(task_id, task_role)
            if agent_msgs:
                message_context = format_messages_for_prompt(agent_msgs)
                mark_messages_read(task_id, task_role, cycle)
                log(f"  [Cycle {cycle}] Injected {len(agent_msgs)} message(s) from other agents", output)
            else:
                message_context = ""

            # Build extra context from compaction history.
            # CRITICAL: Paralysis injection (KILLED for Analysis Paralysis) must
            # NEVER be truncated — it's the primary mechanism to change agent
            # behavior on retry. See _build_dev_extra_context for the full policy.
            _dc = getattr(args, "dispatch_config", None)
            extra_context = _build_dev_extra_context(
                state.compaction_history, cycle, message_context, _dc
            )

            # Merge any extra_context contributed by pre_cycle hook callbacks.
            # Plugins may return {"extra_context": "..."} to inject text into
            # the developer prompt for this cycle.
            for _hr in (pre_cycle_returns or []):
                if isinstance(_hr, dict) and _hr.get("extra_context"):
                    _injected = _hr["extra_context"]
                    if _injected and _injected not in (extra_context or ""):
                        extra_context = (extra_context or "") + _injected
                        log(f"  [Cycle {cycle}] Plugin extra_context injected ({len(_injected)} chars)", output)

            dev_prompt = build_system_prompt(
                task, project_context, project_dir,
                role=task_role, extra_context=extra_context,
                dispatch_config=dispatch_config,
                error_type=state.last_error_type,
                max_turns=dev_turns_allocated,
            )
            use_streaming = task_role not in EARLY_TERM_EXEMPT_ROLES
            # --- Lifecycle hooks: pre_agent_start ---
            await fire_hook(
                "pre_agent_start",
                task_id=task_id, cycle=cycle, role=task_role,
                project_dir=project_dir, model=dev_model,
            )

            # Extract paralysis retry count so agent_runner can apply tighter
            # kill thresholds. Without this, the escalating prompt injections
            # have no teeth — the agent still gets the full kill budget.
            paralysis_retries = state.dev_run_config.get("_paralysis_retry_count", 0)

            with build_cli_command(
                dev_prompt, project_dir, dev_turns_allocated, dev_model, role=task_role,
                streaming=use_streaming,
            ) as dev_cmd:
                dev_result: AgentResult = await dispatch_agent(
                    dev_cmd, role=task_role, output=output, max_turns=dev_turns_allocated,
                    task_id=task_id, cycle=cycle, system_prompt=dev_prompt,
                    project_dir=project_dir, args=args,
                    paralysis_retry_count=paralysis_retries)
            dev_result["turns_allocated"] = dev_turns_allocated
            dev_result["turns_max"] = dev_turns_max
            state.total_duration += dev_result.get("duration", 0)
            state.total_cost += _accumulate_cost(
                dev_result, f"[Cycle {cycle}] Developer", output)

            # --- Lifecycle hooks: post_agent_finish (developer) ---
            await fire_hook(
                "post_agent_finish",
                task_id=task_id, cycle=cycle, role=task_role,
                project_dir=project_dir, success=dev_result.get("success", False),
                cost=dev_result.get("cost"), duration=dev_result.get("duration", 0),
            )

            # Cost-based circuit breaker
            cost_reason = _check_cost_limit(state.total_cost, complexity, config_cost_limits)
            if cost_reason:
                log(f"  [Cycle {cycle}] {cost_reason}", output)
                state.loop_detector.record(dev_result, cycle)
                _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                dev_result["early_terminated"] = True
                dev_result["early_term_reason"] = cost_reason
                return dev_result, cycle, "cost_limit_exceeded"

            # Check for early termination — retry analysis paralysis with stricter prompt
            if dev_result.get("early_terminated"):
                reason = dev_result.get("early_term_reason", "unknown")
                log(f"  [Cycle {cycle}] Developer early-terminated: {reason}", output)
                state.loop_detector.record(dev_result, cycle)

                # If killed for analysis paralysis (no file changes), retry with
                # escalating scaffold-first prompts. Each retry is MORE aggressive
                # and reduces the kill threshold. This is the #1 cause of failure
                # on large codebases — seen in FeatureBench task 3 where the agent
                # hit EarlyTerm on attempts 4, 5, 6, 8, 10.
                if _handle_paralysis_retry(
                    reason, cycle, state.compaction_history, state.dev_run_config, output
                ):
                    continue

                return dev_result, cycle, "early_terminated"

            # Check for agent-initiated early completion
            if dev_result.get("early_completed"):
                ec_reason = dev_result.get("early_complete_reason", "")
                log(f"  [Cycle {cycle}] Developer signaled early completion: "
                    f"{ec_reason}", output)
                no_changes_phrases = [
                    "no changes needed", "no changes required",
                    "no modifications needed", "nothing to change",
                    "already implemented", "already exists",
                    "no work needed", "task already complete",
                ]
                if any(phrase in ec_reason.lower() for phrase in no_changes_phrases):
                    log(f"  [Cycle {cycle}] Skipping tester — agent reported no "
                        f"changes needed.", output)
                    clear_checkpoints(task_id)
                    dev_result["cost"] = state.total_cost
                    dev_result["duration"] = state.total_duration
                    return dev_result, cycle, "early_completed_no_changes"
                log(f"  [Cycle {cycle}] Agent completed early with changes — "
                    f"proceeding to tester.", output)

            # Check for timeout or max_turns — save checkpoint, optionally
            # continue with recovery context, or exit if continuations exhausted.
            cont_action, state.continuation_count, cont_err_type, cont_outcome = (
                await _handle_dev_continuation(
                    dev_result, task_id, task_role, cycle, prev_attempt,
                    project_dir, state.continuation_count, state.compaction_history, output,
                )
            )
            if cont_action == "continue":
                state.last_error_type = cont_err_type
                continue
            if cont_action == "exit":
                return dev_result, cycle, cont_outcome  # type: ignore[return-value]
            # cont_action == "proceed" — fall through to normal flow

            # Check for agent failure
            if not dev_result["success"]:
                if dev_result.get("has_file_changes"):
                    log(f"  [Cycle {cycle}] Developer agent reported failure but made file changes. "
                        f"Proceeding to tester.", output)
                    dev_result["success"] = True
                else:
                    log(f"  [Cycle {cycle}] Developer agent failed.", output)
                    return dev_result, cycle, "developer_failed"

            # Compaction
            dev_turns_used_for_compact = dev_result.get("num_turns", 0)
            log(f"  [Cycle {cycle}] Compacting developer output "
                f"({dev_turns_used_for_compact} turns)...", output)
            summary = build_compaction_summary("Developer", dev_result, cycle, task)
            state.compaction_history.append(summary)

            # Check if Developer marked task blocked
            status = _get_task_status(task["id"])
            if status == "blocked":
                log(f"  [Cycle {cycle}] Developer marked task as BLOCKED.", output)
                return dev_result, cycle, "developer_blocked"

            # Progress detection — track both per-cycle and accumulated changes
            progress_action, state.no_progress_count, error_reset = _check_dev_progress(
                dev_result, state.accumulated_files, state.no_progress_count,
                project_dir, cycle, output,
            )
            if error_reset is None:
                state.last_error_type = None
            if progress_action == "block":
                return dev_result, cycle, "no_progress"

            # --- Dynamic Budget Adjustment ---
            prev_budget = dev_turns_allocated
            dev_turns_allocated = adjust_dynamic_budget(
                dev_turns_allocated, dev_turns_max,
                dev_result.get("result_text", ""))
            if dev_turns_allocated != prev_budget:
                log(f"  [DynBudget] Developer budget adjusted: {prev_budget} -> "
                    f"{dev_turns_allocated}/{dev_turns_max}", output)

            # --- Loop Detection ---
            loop_action = state.loop_detector.record(dev_result, cycle)
            if loop_action == "terminate":
                log(f"  [Cycle {cycle}] LOOP DETECTED: Agent repeated the same failing "
                    f"pattern {state.loop_detector.consecutive_same} times. Terminating early.", output)
                dev_result.setdefault("errors", []).append(state.loop_detector.termination_summary())
                return dev_result, cycle, "loop_detected"
            elif loop_action == "warn":
                log(f"  [Cycle {cycle}] Loop warning: Agent has repeated the same pattern "
                    f"{state.loop_detector.consecutive_same} times. Injecting 'try different approach' "
                    f"guidance.", output)
                state.compaction_history.append(state.loop_detector.warning_message())
                dev_result.setdefault("errors", []).append(
                    f"Loop warning: agent repeated same pattern "
                    f"{state.loop_detector.consecutive_same} times (cycle {cycle})"
                )

            # --- Audit/review short-circuit ---
            # If this task's deliverable is a markdown report (audit, review,
            # security-reviewer, code-reviewer) and there are no code changes,
            # there is nothing for the tester to test. Running it anyway either
            # wastes turns in analysis paralysis or returns tester_blocked when
            # the diff is empty. See TheForge bug 2237.
            if _is_audit_type_task(task, task_role):
                diff_empty = await _git_diff_is_empty(project_dir, base_ref=prev_cycle_sha)
                if diff_empty:
                    md_files = [
                        f for f in state.accumulated_files
                        if f.lower().endswith(".md")
                    ]
                    if not md_files:
                        # Fallback: scan the working tree for markdown files the
                        # agent wrote without emitting a FILES_CHANGED marker.
                        # accumulated_files is only populated from those markers,
                        # so one-shot review tasks that just write the file and
                        # stop would otherwise be misreported as failed even
                        # though the deliverable is on disk. See bug 2263.
                        try:
                            status_result = await git_run_async(
                                ["status", "--porcelain"], project_dir, timeout=5,
                            )
                            if status_result.returncode == 0:
                                md_files = [
                                    line[3:].strip()
                                    for line in status_result.stdout.splitlines()
                                    if len(line) > 3
                                    and line[3:].strip().lower().endswith(".md")
                                ]
                        except (subprocess.TimeoutExpired, OSError) as exc:
                            log(
                                f"  [Cycle {cycle}] git status fallback failed "
                                f"while probing for markdown deliverable: {exc}",
                                output,
                            )
                    if md_files:
                        log(
                            f"  [Cycle {cycle}] Tester skipped (audit-type task, "
                            f"no code changes — developer wrote markdown deliverable: "
                            f"{', '.join(md_files[:3])}).",
                            output,
                        )
                        clear_checkpoints(task_id)
                        _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                        return dev_result, cycle, "tests_passed"
                    log(
                        f"  [Cycle {cycle}] Tester skipped (audit-type task, "
                        f"no code changes and no markdown deliverable produced).",
                        output,
                    )
                    _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                    return dev_result, cycle, "failed"

            # --- Tester Phase ---
            log(f"\n  [Cycle {cycle}] Running Tester agent "
                f"(budget: {tester_turns_allocated}/{tester_turns_max})...", output)

            # Capture git diff to give tester context about developer changes.
            # Diff against the prior cycle's HEAD SHA so we don't re-send earlier
            # cycles' diffs to every subsequent tester (see task #2145).
            tester_extra_context = await _capture_git_diff_context(
                project_dir, cycle, output, base_ref=prev_cycle_sha,
            )
            tester_prompt = build_system_prompt(
                task, project_context, project_dir, role="tester",
                dispatch_config=dispatch_config,
                max_turns=tester_turns_allocated,
                extra_context=tester_extra_context,
            )
            # --- Lifecycle hooks: pre_agent_start (tester) ---
            await fire_hook(
                "pre_agent_start",
                task_id=task_id, cycle=cycle, role="tester",
                project_dir=project_dir, model=tester_model,
            )

            with build_cli_command(
                tester_prompt, project_dir, tester_turns_allocated, tester_model, role="tester",
                streaming=True,
            ) as tester_cmd:
                tester_result: AgentResult = await dispatch_agent(
                    tester_cmd, role="tester", output=output, max_turns=tester_turns_allocated,
                    task_id=task_id, cycle=cycle, system_prompt=tester_prompt,
                    project_dir=project_dir, args=args)
            tester_result["turns_allocated"] = tester_turns_allocated
            tester_result["turns_max"] = tester_turns_max
            state.total_duration += tester_result.get("duration", 0)
            state.total_cost += _accumulate_cost(
                tester_result, f"[Cycle {cycle}] Tester", output)

            # --- Lifecycle hooks: post_agent_finish (tester) ---
            await fire_hook(
                "post_agent_finish",
                task_id=task_id, cycle=cycle, role="tester",
                project_dir=project_dir, success=tester_result.get("success", False),
                cost=tester_result.get("cost"), duration=tester_result.get("duration", 0),
            )

            # Cost-based circuit breaker after tester phase
            cost_reason = _check_cost_limit(state.total_cost, complexity, config_cost_limits)
            if cost_reason:
                log(f"  [Cycle {cycle}] {cost_reason} (after tester)", output)
                _apply_cost_totals(tester_result, state.total_cost, state.total_duration)
                tester_result["early_terminated"] = True
                tester_result["early_term_reason"] = cost_reason
                return tester_result, cycle, "cost_limit_exceeded"

            # Check for early termination (stuck tester)
            if tester_result.get("early_terminated"):
                reason = tester_result.get("early_term_reason", "unknown")
                log(f"  [Cycle {cycle}] Tester early-terminated: {reason}", output)
                log(f"  [Cycle {cycle}] Treating tester early-termination as no-tests (accepting dev work)", output)
                tester_result["result"] = "no-tests"
                tester_result["tests_run"] = 0
                tester_result["tests_passed"] = 0

            # Check for timeout
            if any("timed out" in e for e in tester_result.get("errors", [])):
                log(f"  [Cycle {cycle}] Tester timed out.", output)
                return tester_result, cycle, "tester_timeout"

            # Compaction
            tester_turns_for_compact = tester_result.get("num_turns", 0)
            log(f"  [Cycle {cycle}] Compacting tester output "
                f"({tester_turns_for_compact} turns)...", output)
            summary = build_compaction_summary("Tester", tester_result, cycle, task)
            state.compaction_history.append(summary)

            # Parse Tester output
            test_results = parse_tester_output(tester_result.get("result_text", ""))
            test_outcome = test_results["result"]

            log(f"  [Cycle {cycle}] Tester result: {test_outcome} "
                f"({test_results['tests_passed']}/{test_results['tests_run']} passed)", output)

            # --- Lifecycle hooks: post_cycle ---
            await fire_hook(
                "post_cycle",
                task_id=task_id, cycle=cycle, project_dir=project_dir,
                test_outcome=test_outcome, total_cost=state.total_cost,
            )

            outcome_action, return_result, outcome_str = _dispatch_tester_outcome(
                test_results, tester_result, dev_result, cycle, task_id,
                task_role, state.total_cost, state.total_duration,
                state.compaction_history, output,
            )
            if outcome_action == "exit":
                return return_result, cycle, outcome_str  # type: ignore[return-value]
            # outcome_action == "continue_loop" — fall through to next iteration.
            # Refresh prev_cycle_sha so the next cycle's tester diff starts from
            # this cycle's final HEAD (excluding earlier cycles' commits).
            next_sha = await _resolve_head_sha(project_dir, output)
            if next_sha:
                prev_cycle_sha = next_sha

        # All cycles exhausted
        log(f"\n  All {MAX_DEV_TEST_CYCLES} dev-test cycles exhausted. Marking blocked.", output)
        tester_result["cost"] = state.total_cost
        tester_result["duration"] = state.total_duration
        return tester_result, MAX_DEV_TEST_CYCLES, "cycles_exhausted"
    finally:
        # PLAN-1067 §2.B3 — capture orchestrator-cycle session on every exit.
        # Gated by feature flag; never raises (writes go through the safe
        # helper that swallows-and-logs).
        _capture_session_safe(
            task_id=task_id,
            role=task_role,
            project_id=project_id_for_session,
            dispatch_config=dispatch_config,
            output=output,
        )
