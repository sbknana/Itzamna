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
from dataclasses import dataclass, field
from pathlib import Path

from equipa.git_ops import git_run_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateDecision:
    """The single, ground-truth merge-gate decision (task #2706).

    Prior to task #2706 the gated merge relied on three interacting
    *caller-supplied* signals — a tri-state ``review_blocks_merge``, an
    ``expect_artifact`` doc-only hint, and the ``outcome`` — whose correct
    combination lived only in prose comments across ``dispatch``/``cli``.
    The ``expect_artifact=False`` doc-only short-circuit skipped the
    fail-closed invariant *entirely*, so a future caller passing it wrongly
    would silently disable the last line of defence — exactly the
    caller-trust hole the invariant was built to close.

    A ``GateDecision`` is now computed by :func:`decide_merge_gate` from
    GROUND TRUTH INSIDE ``dispatch._gated_merge_task``:

      * ``changed_files`` — the ACTUAL branch diff (``git diff base...branch``),
        never a caller flag.
      * ``doc_only`` — re-derived by calling :func:`is_doc_only_diff` on that
        real file list (which fails closed on an empty list).
      * ``counts`` / ``blocks_merge`` — read from the on-disk
        ``SECURITY-REVIEW-<task>.md`` artifact, fail-closed on missing.

    The dataclass is frozen so a decision cannot be mutated after it is
    computed. ``expect_artifact`` is derived (``not doc_only``) so the
    defensive invariant in ``_merge_task_branch`` receives an internally
    computed value, not a caller assertion.
    """

    blocks_merge: bool
    doc_only: bool
    counts: dict | None
    reason: str
    changed_files: list[str] = field(default_factory=list)

    @property
    def expect_artifact(self) -> bool:
        """Whether the defensive invariant must demand a review artifact.

        A provably doc-only diff cannot introduce a code-level finding, so
        no ``SECURITY-REVIEW-<task>.md`` is expected. Every other diff
        (including an empty/failed one, which :func:`is_doc_only_diff`
        classifies as NOT doc-only) demands the artifact — fail closed.
        """
        return not self.doc_only


def decide_merge_gate(
    changed_files: list[str],
    *,
    security_review_blocks_merge,
    project_dir: str,
    task_id: int,
    block_on_missing: bool = True,
) -> GateDecision:
    """Compute the single :class:`GateDecision` from ground truth.

    This is the ONE place the gate policy is decided. It takes the real
    branch diff (``changed_files``, produced by
    :func:`get_changed_files_for_branch`) plus a callable that reads the
    on-disk security-review artifact, and returns an immutable decision.

    NO caller-supplied booleans participate:

      * doc-only-ness is re-derived here via :func:`is_doc_only_diff` on the
        real file list — a caller can no longer assert ``doc_only`` to
        disable the gate (task #2706).
      * when the diff is NOT doc-only, the artifact is (re-)read through the
        injected ``security_review_blocks_merge`` callable, which fails
        closed on a missing/unparseable artifact when ``block_on_missing``.

    ``security_review_blocks_merge`` is injected (rather than imported) so
    this leaf module stays free of a circular import on ``dispatch`` while
    the decision logic remains unit-testable in isolation. It must have the
    signature ``(project_dir, task_id, *, block_on_missing) ->
    tuple[bool, dict | None]`` — i.e. ``dispatch._security_review_blocks_merge``.
    """
    doc_only = is_doc_only_diff(changed_files)
    if doc_only:
        return GateDecision(
            blocks_merge=False,
            doc_only=True,
            counts=None,
            reason="doc-only-diff",
            changed_files=list(changed_files),
        )
    blocks, counts = security_review_blocks_merge(
        project_dir, task_id, block_on_missing=block_on_missing,
    )
    reason = "security-review-blocked" if blocks else "clean"
    return GateDecision(
        blocks_merge=blocks,
        doc_only=False,
        counts=counts,
        reason=reason,
        changed_files=list(changed_files),
    )


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
    head_ref: str = "HEAD",
) -> list[str]:
    """Return file paths changed on ``head_ref`` vs ``base_ref``.

    Uses ``git diff --name-only base_ref...head_ref`` (three-dot syntax) so
    the comparison is against the merge base, not the literal tip of
    ``base_ref`` — this matches what the eventual ``git merge`` will
    actually examine.

    ``head_ref`` defaults to ``HEAD`` (the caller's checked-out branch), but
    the unified merge gate (task #2706) passes the explicit
    ``forge-task-<id>`` branch name so it computes the SAME diff whether it
    runs in single-task mode (main checkout sits on the task branch) or
    parallel mode (main checkout sits on the default branch while the work
    lives on a shared ``forge-task-<id>`` ref). Because git worktrees share
    one object/ref store, the branch ref is visible from ``project_dir`` in
    both modes.

    When ``base_ref`` is ``None`` (the default), the repository's default
    branch is auto-detected via :func:`equipa.git_ops.get_default_branch`
    so this helper works on both ``master``- and ``main``-defaulted repos
    (task #2479).

    On any failure (timeout, missing git, ref unknown), returns an empty
    list. Callers MUST treat an empty list as "could not determine
    doc-only-ness" — :func:`is_doc_only_diff` already returns False for
    an empty list precisely so a failed lookup never silently disables
    the gate.
    """
    if base_ref is None:
        from equipa.git_ops import get_default_branch
        base_ref = get_default_branch(project_dir)
    try:
        result = await git_run_async(
            ["diff", "--name-only", f"{base_ref}...{head_ref}"],
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
