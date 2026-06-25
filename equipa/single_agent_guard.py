"""Single-agent vacuous-pass / no-output guard (task #2371).

The existing vacuous-pass guard in :mod:`equipa.hooks.vacuous_pass` only runs
on the Dev+Test loop's ``dev_test:fail`` / ``attempt:after`` events. When the
CLI is invoked as ``equipa --task X --role Y`` (single-agent mode, no
``--dev-test``), the loop is skipped entirely and so is the guard. As a
result a no-output planner / code-reviewer run was happily marked SUCCESS
and the task row set to DONE — exactly the failure that bit task #2361 on
2026-05-14, when a planner wrote zero files but the orchestrator recorded
``tests_passed`` and even trusted a hallucinated
``TASKS_CREATED: 78,79,80,81,82`` line that referenced unrelated
pre-existing ForgeBridge tickets from January.

This module provides the role-aware "did the agent actually produce
output?" checks that must run on EVERY single-agent role completion:

* :func:`evaluate_single_agent_outcome` — given a role, a task id, the
  agent's run-result dict, and the repo path, returns a
  :class:`SingleAgentOutcome` whose ``is_blocked`` flag is True when the
  run produced no on-disk evidence. The exact "no output" definition is
  role-dependent (see ``_FILE_PRODUCING_ROLES`` and ``_REVIEW_ROLES``).
* :func:`validate_tasks_created_claim` — rejects agent stdout containing a
  ``TASKS_CREATED:`` line whose IDs (a) do not exist, (b) were created
  before the run started, or (c) belong to a different project than the
  one the agent was dispatched against.

The orchestrator's CLI single-agent branch (see ``equipa/cli.py`` around
the ``# Single-agent mode (Phase 1 — with model tiering)`` block) consults
these helpers and downgrades the recorded outcome to ``"no_output"``
(treated as failure by ``_post_task_telemetry``) when the guard trips.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


# Roles whose deliverable is a code/document change committed to git.
# A run by one of these roles MUST produce at least one real file change
# (or an explicit deliverable named in the task description).
_FILE_PRODUCING_ROLES: frozenset[str] = frozenset({
    "planner",
    "developer",
    "frontend-designer",
    "frontend_designer",
    "world-builder",
    "world_builder",
})

# Roles whose deliverable is an on-disk review artifact named
# ``{REVIEW-TYPE}-{TASK_ID}.md``. These runs need not commit code; they
# must, however, leave the review file on disk.
_REVIEW_ROLES: frozenset[str] = frozenset({
    "code-reviewer",
    "code_reviewer",
    "security-reviewer",
    "security_reviewer",
    "evaluator",
})

# Files that don't count as real work (the same sentinel files the
# Dev+Test vacuous-pass guard ignores in ``equipa/hooks/vacuous_pass.py``).
_TRIVIAL_FILES: frozenset[str] = frozenset({
    ".forge-state.json",
    ".forge_state.json",
})

# Map review-role -> filename prefix for the expected artifact. The
# task description's spec uses ``{REVIEW-TYPE}-{TASK_ID}.md``; this map
# defines the canonical REVIEW-TYPE per role.
_REVIEW_ARTIFACT_PREFIX: dict[str, tuple[str, ...]] = {
    "code-reviewer": ("CODE-REVIEW",),
    "code_reviewer": ("CODE-REVIEW",),
    "security-reviewer": ("SECURITY-REVIEW",),
    "security_reviewer": ("SECURITY-REVIEW",),
    "evaluator": ("EVALUATION", "EVAL"),
}


@dataclass(frozen=True)
class SingleAgentOutcome:
    """Result of evaluating a single-agent run for the vacuous-pass guard.

    ``status`` is either ``"success"`` (the run produced output) or
    ``"no_output"`` (no on-disk evidence — orchestrator should mark the
    task BLOCKED, never DONE).
    """

    status: str
    reason: str
    files_observed: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.status != "success"


@dataclass(frozen=True)
class TasksCreatedValidation:
    """Verdict on a ``TASKS_CREATED: ...`` line emitted by an agent."""

    is_valid: bool
    reason: str
    claimed_ids: tuple[int, ...] = ()
    invalid_ids: tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# File-set helpers
# ---------------------------------------------------------------------------

def _normalize_files(raw: Any) -> list[str]:
    """Mirror equipa.hooks.vacuous_pass._normalize_files."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [line.strip(" -*\t") for line in raw.splitlines() if line.strip()]
    return [str(f).strip() for f in raw if str(f).strip()]


def _real_files(files: Iterable[str]) -> list[str]:
    """Filter out trivial sentinel files (e.g. .forge-state.json)."""
    return [f for f in files if f and f not in _TRIVIAL_FILES]


def _git_diff_files(repo_path: Path) -> list[str]:
    """Files changed on the working branch vs its merge-base.

    Falls back to ``git status --porcelain`` if rev-list is unavailable
    (e.g. when no upstream is configured). Returns an empty list on any
    error so the guard fails CLOSED — better to ask for explicit output
    proof than to silently accept a no-op run.
    """
    try:
        # Prefer the diff against the merge-base of master/main if one exists.
        for base in ("master", "main", "HEAD~1"):
            try:
                out = subprocess.run(
                    ["git", "diff", "--name-only", f"{base}...HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if out.returncode == 0 and out.stdout.strip():
                    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            except (subprocess.SubprocessError, OSError):
                continue
        # Fallback: untracked + modified working-tree files.
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return []
        files: list[str] = []
        for line in out.stdout.splitlines():
            # Porcelain format: "XY path" — strip the two status chars + space.
            stripped = line[3:].strip() if len(line) > 3 else line.strip()
            if stripped:
                files.append(stripped)
        return files
    except Exception:  # pragma: no cover — defensive only
        logger.exception("git diff failed in %s", repo_path)
        return []


# Directories that never hold a real deliverable; pruned by the filesystem
# scan so a huge dependency/VCS tree can't slow it down (or false-positive on
# a vendored file whose mtime happens to fall after run start).
_FS_SCAN_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".forge-worktrees",
})

# Filesystem-mtime granularity (and small clock skew between the orchestrator
# recording the run start and the agent process writing files) can leave a
# genuinely-during-the-run file looking a hair "older" than run start. Allow a
# small slack so we don't drop a real deliverable on rounding.
_FS_MTIME_SLACK_SECONDS = 2.0


def _as_timestamp(value: Any) -> float | None:
    """Coerce a run-start marker (epoch float, datetime, or ISO string) to epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    dt = _parse_iso_timestamp(value)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _fs_files_since(repo_path: Path, run_started_at: Any) -> list[str]:
    """Non-trivial files under ``repo_path`` created/modified at/after run start.

    This is the git-independent evidence path: a deliverable written into a
    **non-git** project dir (a CAD/docs/product folder) is invisible to
    :func:`_git_diff_files`, but it still lands on disk with a fresh mtime.
    Keying on ``run_started_at`` (rather than trusting the agent's self-report)
    keeps the guard honest — a pre-existing file is not counted as new work.

    Returns repo-relative paths. Empty when no marker is available or nothing
    changed (so the guard still fails CLOSED on a genuinely empty run).
    """
    started = _as_timestamp(run_started_at)
    if started is None:
        return []
    threshold = started - _FS_MTIME_SLACK_SECONDS

    found: list[str] = []
    try:
        for root, dirs, files in os.walk(repo_path):
            # Prune hidden + heavy dirs in place so os.walk skips them entirely.
            dirs[:] = [
                d for d in dirs
                if d not in _FS_SCAN_SKIP_DIRS and not d.startswith(".")
            ]
            for name in files:
                if name in _TRIVIAL_FILES or name.startswith("."):
                    continue
                p = Path(root) / name
                try:
                    if p.stat().st_mtime >= threshold:
                        found.append(str(p.relative_to(repo_path)))
                except (OSError, ValueError):
                    continue
    except OSError:
        return found
    return found


def _is_report_role(role: str, project_dir: str | None) -> bool:
    """True for a report-writing role (base OR project overlay).

    A role whose frontmatter sets ``early_term_exempt: true`` delivers an
    on-disk artifact rather than a git commit, so its output must be credited
    via the filesystem scan, not git. Resolver-aware so project-overlay roles
    (``<project_dir>/.equipa/roles/<role>.md``) are classified correctly
    instead of falling into the strict "unrecognized role" branch.
    """
    try:
        from equipa.role_resolver import is_role_early_term_exempt
    except Exception:  # pragma: no cover — defensive, keeps guard importable
        return False
    try:
        return is_role_early_term_exempt(role, project_dir)
    except Exception:  # pragma: no cover — never let classification crash the guard
        logger.exception("role classification failed for %r", role)
        return False


# ---------------------------------------------------------------------------
# Public API — single-agent outcome evaluation
# ---------------------------------------------------------------------------

def _review_artifact_present(role: str, task_id: int, repo_path: Path) -> list[str]:
    """Return the on-disk review artifacts matching ``{PREFIX}-{TASK_ID}.md``.

    Looks for files at the repo root AND one level deep so reviews stored
    in e.g. ``docs/`` or ``reviews/`` still count.
    """
    prefixes = _REVIEW_ARTIFACT_PREFIX.get(role, ())
    if not prefixes:
        return []

    found: list[str] = []
    candidates: list[Path] = []
    for prefix in prefixes:
        name = f"{prefix}-{task_id}.md"
        candidates.append(repo_path / name)
        try:
            candidates.extend(repo_path.glob(f"*/{name}"))
        except OSError:
            pass
    for p in candidates:
        try:
            if p.is_file():
                found.append(str(p.relative_to(repo_path)))
        except (ValueError, OSError):
            found.append(str(p))
    return found


def evaluate_single_agent_outcome(
    *,
    role: str,
    task_id: int,
    run_result: dict[str, Any],
    repo_path: Path,
    run_started_at: str | datetime | float | None = None,
    project_dir: str | None = None,
) -> SingleAgentOutcome:
    """Decide whether a single-agent run produced real output.

    Args:
        role: The role the agent was dispatched as (e.g. ``"planner"``).
        task_id: The TheForge task id (used for review-artifact matching).
        run_result: The agent_runner result dict. Recognized keys:
            ``files_changed`` (preferred), ``raw_files_changed`` (fallback),
            ``stdout`` (used only as a last-resort signal for review roles).
        repo_path: The on-disk working directory the agent ran in.
        run_started_at: When the orchestrator dispatched the agent (epoch
            float, datetime, or ISO string). Enables the git-independent
            filesystem fallback: a deliverable written into a **non-git**
            project dir is detected by its post-run mtime. When omitted the
            fallback is disabled and detection is git + self-report only
            (legacy behaviour).
        project_dir: The dispatching project's directory, used to resolve a
            project-overlay role's ``early_term_exempt`` frontmatter so a
            report-writing overlay role is classified correctly rather than
            treated as an "unrecognized" strict role.

    Returns:
        :class:`SingleAgentOutcome`. ``is_blocked`` is True iff the run
        failed the role-specific output requirement.
    """
    repo_path = Path(repo_path)
    files_changed = _normalize_files(
        run_result.get("files_changed") or run_result.get("raw_files_changed") or []
    )

    # Always cross-check against git for authoritative "real work" signal.
    git_files = _git_diff_files(repo_path)
    union = list(dict.fromkeys(_real_files(files_changed) + _real_files(git_files)))

    # Git-independent fallback. Only engaged when git found nothing — i.e. a
    # non-git project dir, or a git repo with no committed/working-tree change
    # (e.g. an untracked report). Keyed on run start so pre-existing files do
    # not count. This is what stops "no git repo" being read as "no output".
    fs_files: list[str] = []
    if not git_files:
        fs_files = _fs_files_since(repo_path, run_started_at)

    if role in _FILE_PRODUCING_ROLES:
        evidence = list(dict.fromkeys(union + fs_files))
        if evidence:
            return SingleAgentOutcome(
                status="success",
                reason=f"file-producing role {role!r} changed {len(evidence)} file(s)",
                files_observed=tuple(evidence),
            )
        return SingleAgentOutcome(
            status="no_output",
            reason=(
                f"role={role!r} requires at least one file change but the run "
                f"committed zero real files (files_changed={files_changed!r}, "
                f"git_diff={git_files!r})"
            ),
            files_observed=(),
        )

    if role in _REVIEW_ROLES:
        artifacts = _review_artifact_present(role, task_id, repo_path)
        # Generalized fallback: a review run that left its artifact under a
        # non-conventional name (or in a non-git dir) is still credited on any
        # non-trivial file touched since run start.
        evidence = list(dict.fromkeys(artifacts + fs_files))
        if evidence:
            return SingleAgentOutcome(
                status="success",
                reason=f"review role {role!r} produced artifact(s): {evidence!r}",
                files_observed=tuple(evidence),
            )
        return SingleAgentOutcome(
            status="no_output",
            reason=(
                f"role={role!r} requires a review artifact named "
                f"{{REVIEW-TYPE}}-{task_id}.md on disk but none was found "
                f"under {repo_path}"
            ),
            files_observed=(),
        )

    # Report-writing role (base or project overlay) or unknown/generic role.
    # Both are credited on git/self-report evidence OR a filesystem deliverable
    # produced since run start — the report-overlay case is the common one for
    # non-git product folders.
    report_role = _is_report_role(role, project_dir)
    evidence = list(dict.fromkeys(union + fs_files))
    if evidence:
        kind = "report role" if report_role else "unrecognized role"
        return SingleAgentOutcome(
            status="success",
            reason=f"{kind} {role!r}; accepted on {len(evidence)} file change(s)",
            files_observed=tuple(evidence),
        )
    kind = "report role" if report_role else "unrecognized role"
    return SingleAgentOutcome(
        status="no_output",
        reason=(
            f"{kind} {role!r} produced no on-disk evidence since run start; "
            "refusing to mark success without a deliverable on disk"
        ),
        files_observed=(),
    )


# ---------------------------------------------------------------------------
# TASKS_CREATED hallucination check
# ---------------------------------------------------------------------------

_TASKS_CREATED_RE = re.compile(
    r"^\s*TASKS_CREATED\s*:\s*([0-9 ,]+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_tasks_created_ids(stdout: str) -> list[int]:
    """Extract integer task ids from any TASKS_CREATED line in ``stdout``."""
    ids: list[int] = []
    for match in _TASKS_CREATED_RE.finditer(stdout or ""):
        for chunk in match.group(1).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                continue
    # De-dupe while preserving order
    seen: set[int] = set()
    deduped: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            deduped.append(i)
    return deduped


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parser; returns None on garbage."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    # Try a few common shapes.
    for fmt in (None,):
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def validate_tasks_created_claim(
    *,
    stdout: str,
    run_started_at: str | datetime | None,
    expected_project_id: int,
    db: Any,
) -> TasksCreatedValidation:
    """Verify a ``TASKS_CREATED: ...`` claim against TheForge.

    The agent's claim is accepted only if every claimed id:
        (a) exists in ``tasks``,
        (b) was created at or after ``run_started_at``,
        (c) belongs to ``expected_project_id``.

    If no TASKS_CREATED line is present in ``stdout`` the claim is
    trivially valid (there is nothing to hallucinate).

    Args:
        stdout: Raw agent output to scan.
        run_started_at: When the orchestrator dispatched the agent.
            Used to detect "stale" ids that predate this run.
        expected_project_id: The project the agent should be writing to.
        db: An object exposing ``fetch_tasks_by_ids(ids: Sequence[int]) ->
            list[dict]`` where each dict has at minimum ``id``,
            ``project_id`` and ``created_at`` keys.
    """
    claimed = _parse_tasks_created_ids(stdout)
    if not claimed:
        return TasksCreatedValidation(
            is_valid=True,
            reason="no TASKS_CREATED line present",
        )

    rows: Sequence[dict[str, Any]] = []
    try:
        rows = db.fetch_tasks_by_ids(claimed) or []
    except Exception as exc:  # pragma: no cover — db-shape errors
        logger.exception("fetch_tasks_by_ids failed")
        return TasksCreatedValidation(
            is_valid=False,
            reason=f"db lookup failed: {exc!r}",
            claimed_ids=tuple(claimed),
            invalid_ids=tuple(claimed),
        )

    rows_by_id = {int(r["id"]): r for r in rows if r.get("id") is not None}
    run_started = _parse_iso_timestamp(run_started_at)

    invalid: list[int] = []
    reasons: list[str] = []
    for tid in claimed:
        row = rows_by_id.get(tid)
        if row is None:
            invalid.append(tid)
            reasons.append(f"#{tid}: not present in tasks (hallucinated id)")
            continue
        # Project mismatch
        row_project = row.get("project_id")
        if expected_project_id is not None and row_project != expected_project_id:
            invalid.append(tid)
            reasons.append(
                f"#{tid}: project_id={row_project} != expected "
                f"{expected_project_id} (likely stale/pre-existing id)"
            )
            continue
        # Pre-existing id (created before the agent run started)
        if run_started is not None:
            created = _parse_iso_timestamp(row.get("created_at"))
            if created is not None and created < run_started:
                invalid.append(tid)
                reasons.append(
                    f"#{tid}: created_at={row.get('created_at')!r} predates "
                    f"run_started_at={run_started_at!r} (pre-existing id)"
                )

    if invalid:
        return TasksCreatedValidation(
            is_valid=False,
            reason="; ".join(reasons),
            claimed_ids=tuple(claimed),
            invalid_ids=tuple(invalid),
        )
    return TasksCreatedValidation(
        is_valid=True,
        reason=f"all {len(claimed)} claimed task id(s) verified",
        claimed_ids=tuple(claimed),
    )


__all__ = [
    "SingleAgentOutcome",
    "TasksCreatedValidation",
    "evaluate_single_agent_outcome",
    "validate_tasks_created_claim",
]
