"""Security-gate helpers — task #2360 + task #2451.

This module owns the small, pure decisions the parallel and single-task
dispatch paths need to make about the security-review gate:

  * ``is_doc_only_diff(changed_files)`` — True when every changed file is
    a documentation file (.md/.txt/.rst/...). Such diffs cannot introduce
    code-level vulnerabilities, so the security gate must not block them
    on prose-matching false positives. Concrete trigger: task #2358, a
    pure CRYPTOTRADER-V3-ARCHITECTURE.md spec, was blocked because the
    document used the word "HIGH" and discussed API-key auth.

  * ``SecurityGateBypassError`` — the defensive invariant raised by
    ``dispatch._merge_task_branch`` when its artifact re-check trips.

  * ``format_counts`` / ``_gate_audit_log`` — shared helpers for the
    [GATE-AUDIT] telemetry line both dispatch paths emit.

Task #2451 Phase J (F-03 fix): an earlier draft also exposed
``evaluate_gate`` + ``GateVerdict`` — a second open-coded gate policy
that lived in parallel with the one in ``dispatch._gated_merge_task``.
That duplication was the same architectural shape as the original
parent bug (decoupled single-task and parallel gates that drifted out
of sync), so it has been deleted. The gate policy now lives in EXACTLY
one place: ``dispatch._gated_merge_task`` + the defensive invariant in
``dispatch._merge_task_branch``. Future callers needing the gate decision
must go through that one path.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from equipa.git_ops import git_run_async

logger = logging.getLogger(__name__)


def _gate_audit_log(
    message: str,
    *,
    task_id: int | None = None,
    event: str | None = None,
    counts: dict | None = None,
) -> None:
    """Emit a ``[GATE-AUDIT]`` line and persist a durable audit row.

    Task #2451 Phase G — stderr behaviour (UNCHANGED by task #2702):
      * Lives in the leaf ``security_gate`` module so both ``dispatch`` and
        ``cli`` can import it without a circular dependency (GATE-07).
      * Writes ONLY to ``sys.stderr`` — the prior implementation also called
        ``logger.info`` which double-emitted in CI capture (GATE-06).
      * The ``EQUIPA_GATE_AUDIT_LOG`` env gate silences the stderr line
        (default ``"1"`` = emit) so CI capture does not double-emit.

    Task #2702 — durable persistence (NEW, additive):
      * In addition to the stderr line, each gate event is persisted to the
        ``agent_actions`` table so a post-hoc audit of "why did this branch
        merge or block" survives a lost nohup log.
      * Callers SUPPLY the structured ``task_id`` (and optional ``event`` /
        ``counts``) rather than this function parsing its own message string.
        As a compatibility net for callers that pre-date the kwargs, a narrow
        ``task=<n>`` fallback is parsed from ``message`` — the structured
        param always takes precedence.
      * Persistence is INDEPENDENT of ``EQUIPA_GATE_AUDIT_LOG``: that env gate
        only silences the stderr line (a CI double-emit concern); the audit
        trail must remain durable even when stderr is muted.
      * The DB write is best-effort FAIL-OPEN — any error is swallowed inside
        :func:`equipa.db.log_gate_audit` and can never alter the gate
        decision or raise into the merge path. ``equipa.db`` is imported
        lazily here so the leaf ``security_gate`` module keeps a clean import
        graph and an import failure cannot break the stderr path.

    Args:
        message: the human-readable audit line (emitted to stderr and stored
            verbatim as the durable record).
        task_id: task the event belongs to; enables the DB row. When ``None``
            a ``task=<n>`` token in ``message`` is used as a fallback.
        event: short event tag (e.g. ``"merge-succeeded"``) for queryable
            filtering in the persisted row.
        counts: finding counts dict, passed through to the persistence layer.
    """
    if os.environ.get("EQUIPA_GATE_AUDIT_LOG", "1") != "0":
        print(f"[GATE-AUDIT] {message}", file=sys.stderr, flush=True)

    resolved_task_id = task_id if task_id is not None else _parse_task_id(message)
    if resolved_task_id is None:
        return
    try:
        from equipa.db import log_gate_audit

        log_gate_audit(
            message, resolved_task_id, event=event, counts=counts,
        )
    except Exception:
        # Defence in depth: log_gate_audit is already fail-open, but even the
        # lazy import must never raise into the gate/merge path (task #2702).
        logger.exception("[GATE-AUDIT] audit persistence dispatch failed")


# Matches the ``task=<n>`` token that every _gate_audit_log message embeds,
# used only as a fallback when a caller does not pass the structured task_id.
_TASK_ID_RE = re.compile(r"\btask[=# ](\d+)\b")


def _parse_task_id(message: str) -> int | None:
    """Best-effort extraction of the task id from an audit ``message``.

    Fallback only — callers should pass ``task_id`` explicitly. Returns
    ``None`` when no ``task=<n>`` token is present so persistence is skipped
    rather than attributing the event to the wrong task.
    """
    match = _TASK_ID_RE.search(message or "")
    return int(match.group(1)) if match else None


def format_counts(counts: dict | None) -> str:
    """Render finding counts as ``C=N H=N M=N L=N I=N`` for audit lines.

    Defensive against ``None`` and missing keys — used at every audit site
    so future refactors cannot accidentally leak ``counts!r`` repr internals
    (GATE-09).
    """
    if counts is None:
        return "C=0 H=0 M=0 L=0 I=0"
    return (
        f"C={counts.get('CRITICAL', 0)} "
        f"H={counts.get('HIGH', 0)} "
        f"M={counts.get('MEDIUM', 0)} "
        f"L={counts.get('LOW', 0)} "
        f"I={counts.get('INFO', 0)}"
    )

# Extensions that carry executable code or executable configuration.
# A diff containing ANY file with one of these extensions cannot be
# considered "doc-only" — the security gate must run normally.
#
# Conservative by design: only well-known doc extensions skip the gate.
# An unfamiliar extension (Makefile, .toml, .yaml, .json, .lock, etc.)
# is treated as code so we never silently skip a gate the operator
# expected to run.
_DOC_EXTENSIONS: frozenset[str] = frozenset({
    ".md",
    ".markdown",
    ".rst",
    ".txt",
    ".adoc",
    ".asciidoc",
})


def is_doc_only_diff(changed_files: list[str]) -> bool:
    """Return True iff every path in ``changed_files`` is a documentation file.

    An empty list returns False on purpose: callers fetch the file list
    from ``git diff --name-only`` and an empty result usually means the
    diff couldn't be computed (worktree missing, base branch wrong, etc).
    Treating "no files reported" as "doc-only" would silently disable
    the gate on every such failure — exactly the silent-skip class of
    bug that task #2321 originally fixed.
    """
    if not changed_files:
        return False
    for path in changed_files:
        suffix = Path(path).suffix.lower()
        if suffix not in _DOC_EXTENSIONS:
            return False
    return True


class SecurityGateBypassError(RuntimeError):
    """Raised when ``_merge_task_branch`` is entered while the
    security-review artifact for the task reports a HIGH or CRITICAL
    finding. The defensive invariant (task #2451) is that no code path
    can ever ``git merge`` past a known-blocking review, even if a
    caller forgets to consult the gate first.
    """


async def get_changed_files_for_branch(
    project_dir: str,
    base_ref: str | None = None,
) -> list[str]:
    """Return file paths changed on the current branch vs ``base_ref``.

    Uses ``git diff --name-only base_ref...HEAD`` (three-dot syntax) so
    the comparison is against the merge base, not the literal tip of
    ``base_ref`` — this matches what the eventual ``git merge`` will
    actually examine.

    When ``base_ref`` is ``None`` (the default), the repository's default
    branch is auto-detected via :func:`equipa.git_ops.get_default_branch`
    so this helper works on both ``master``- and ``main``-defaulted repos
    (task #2479).

    On any failure (timeout, missing git, base_ref unknown), returns an
    empty list. Callers MUST treat an empty list as "could not determine
    doc-only-ness" — :func:`is_doc_only_diff` already returns False for
    an empty list precisely so a failed lookup never silently disables
    the gate.
    """
    if base_ref is None:
        from equipa.git_ops import get_default_branch
        base_ref = get_default_branch(project_dir)
    try:
        result = await git_run_async(
            ["diff", "--name-only", f"{base_ref}...HEAD"],
            project_dir,
            timeout=10,
        )
    except (TimeoutError, FileNotFoundError, OSError) as exc:
        logger.warning(
            "[security-gate] could not compute changed files vs %s: %s",
            base_ref, exc,
        )
        return []
    if result.returncode != 0:
        logger.warning(
            "[security-gate] git diff vs %s exited %d: %s",
            base_ref, result.returncode, (result.stderr or "")[:200],
        )
        return []
    return [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
