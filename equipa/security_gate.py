"""Security-gate helpers — task #2360.

This module owns the small, pure decisions the parallel and single-task
dispatch paths need to make about the security-review gate:

  * ``is_doc_only_diff(changed_files)`` — True when every changed file is
    a documentation file (.md/.txt/.rst/...). Such diffs cannot introduce
    code-level vulnerabilities, so the security gate must not block them
    on prose-matching false positives. Concrete trigger: task #2358, a
    pure CRYPTOTRADER-V3-ARCHITECTURE.md spec, was blocked because the
    document used the word "HIGH" and discussed API-key auth.

  * ``evaluate_gate(...)`` — composes the doc-only short-circuit, the
    artifact-required rule (task #2341 hardening) and the reviewer
    verdict into a single Verdict the dispatch layer can act on.

The functions here are deliberately I/O-light (only ``evaluate_gate``
touches the filesystem, and only to check artifact existence). All
policy lives here so it can be unit-tested without spinning up an agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from equipa.git_ops import git_run_async

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class GateVerdict:
    """Decision returned by ``evaluate_gate``.

    ``gating`` — True iff the merge MUST be blocked.
    ``escalate`` — True iff the reviewer pipeline itself failed in a way
        that the operator must inspect (gating verdict but no artifact).
        Distinct from ``gating`` because escalation should NOT be silent:
        a missing artifact for a BLOCK verdict is unauditable.
    ``reason`` — short human-readable rationale, surfaced in dispatch
        logs so operators can tell "skipped" from "blocked" at a glance.
    """

    gating: bool
    escalate: bool
    reason: str


def evaluate_gate(
    *,
    task_id: int,
    changed_files: list[str],
    reviewer_verdict: str,
    reviewer_findings_high: int,
    reviewer_findings_critical: int,
    artifact_dir: Path,
) -> GateVerdict:
    """Apply the gating policy for one finished security review.

    Policy precedence (first match wins):
      1. Doc-only diff -> never gating (defect 1 in task #2360).
      2. Reviewer says BLOCK and the SECURITY-REVIEW-{task_id}.md
         artifact does NOT exist on disk -> escalate as reviewer
         failure (defect 3 in task #2360 / hardening of task #2341).
      3. Reviewer says BLOCK and findings exist -> gate the merge.
      4. Otherwise -> non-gating.

    ``reviewer_verdict`` is a free-form string — only ``"BLOCK"`` is
    treated as a gating verdict, everything else (``"PASS"``, ``""``,
    ``"INFO"``) lets the merge proceed (subject to rule 1 which has
    already short-circuited the path that matters).
    """
    if is_doc_only_diff(changed_files):
        return GateVerdict(
            gating=False,
            escalate=False,
            reason="doc-only change — security gate skipped",
        )

    if reviewer_verdict.upper() != "BLOCK":
        return GateVerdict(
            gating=False,
            escalate=False,
            reason="reviewer did not return a gating verdict",
        )

    artifact = artifact_dir / f"SECURITY-REVIEW-{task_id}.md"
    if not artifact.exists():
        return GateVerdict(
            gating=False,
            escalate=True,
            reason=(
                f"reviewer returned BLOCK but artifact "
                f"{artifact.name} is missing — escalate as reviewer "
                "failure (block must not be silent / unauditable)"
            ),
        )

    if reviewer_findings_critical > 0 or reviewer_findings_high > 0:
        return GateVerdict(
            gating=True,
            escalate=False,
            reason=(
                f"{reviewer_findings_critical} CRITICAL, "
                f"{reviewer_findings_high} HIGH finding(s)"
            ),
        )

    return GateVerdict(
        gating=False,
        escalate=False,
        reason="no gating findings recorded",
    )


async def get_changed_files_for_branch(
    project_dir: str,
    base_ref: str = "master",
) -> list[str]:
    """Return file paths changed on the current branch vs ``base_ref``.

    Uses ``git diff --name-only base_ref...HEAD`` (three-dot syntax) so
    the comparison is against the merge base, not the literal tip of
    ``base_ref`` — this matches what the eventual ``git merge`` will
    actually examine.

    On any failure (timeout, missing git, base_ref unknown), returns an
    empty list. Callers MUST treat an empty list as "could not determine
    doc-only-ness" — :func:`is_doc_only_diff` already returns False for
    an empty list precisely so a failed lookup never silently disables
    the gate.
    """
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
