"""Vacuous-pass guard handler.

Migrated from inline guard logic in equipa/loops.py. Detects the
"vacuous pass" failure mode where a tester reports success but the
developer made no real file changes (or only touched no-op files such
as the local .forge-state.json checkpoint).

Fires on ``dev_test:fail`` and ``attempt:after``. Returns a dict with
``vacuous=True`` when the heuristic trips so callers can mark the
attempt as not-truly-passed and trigger a retry with stricter prompts.

Heuristic (mirrors the prior inline behavior):
    * If the tester result reports zero tests run AND there are no real
      file changes (FILES_CHANGED is empty or only contains the state
      file), classify as vacuous.
    * If FILES_CHANGED contains only documentation files (*.md) but the
      task description verb is "implement"/"add"/"fix", also vacuous.

Task #2079: the per-cycle ``dev_result.files_changed`` view is too
narrow. Agents legitimately commit all their work in cycles 1-2 and
then idle in cycles 3-5; the guard would fire on the late idle cycles
even though the branch already contained real commits. The handler
now widens the file-change view to include:

    * ``accumulated_files`` kwarg — set of files touched across all
      cycles so far (the dev-test loop already tracks this for the
      no-progress guard, see ``equipa/loops.py:_check_dev_progress``).
    * ``has_branch_commits(project_dir)`` — git-level fallback. If the
      working branch has commits past its merge-base, the agent has
      shipped real work, so a per-cycle empty ``files_changed`` is not
      vacuous.

The handler is pure (no side effects) — it only inspects the event
payload and returns a classification dict. It MUST NOT mutate
orchestrator state. Callers in loops.py read the returned dict from
``fire_async`` results and react accordingly.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Files that don't count as real work.
_TRIVIAL_FILES: frozenset[str] = frozenset({
    ".forge-state.json",
    ".forge_state.json",
})

# Verbs in a task description that REQUIRE code changes (not just docs).
_CODE_CHANGE_VERBS: tuple[str, ...] = (
    "implement", "add", "fix", "create", "build",
    "refactor", "migrate", "update", "rename", "remove",
)


def _is_trivial_change(files_changed: list[str]) -> bool:
    """True if the change list is empty or only touches sentinel files."""
    if not files_changed:
        return True
    real = [f for f in files_changed if f.strip() and f.strip() not in _TRIVIAL_FILES]
    return len(real) == 0


def _is_docs_only(files_changed: list[str]) -> bool:
    """True if every changed file is a markdown / docs file."""
    real = [f.strip() for f in files_changed if f.strip()]
    if not real:
        return False
    return all(f.lower().endswith((".md", ".rst", ".txt")) for f in real)


def _normalize_files(raw: Any) -> list[str]:
    """Coerce a string-or-iterable file list into a clean list[str]."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [
            line.strip(" -*\t")
            for line in raw.splitlines()
            if line.strip()
        ]
    return [str(f) for f in raw]


def _branch_has_real_work(project_dir: Any) -> bool:
    """True when the working branch already contains committed work.

    Imports lazily to avoid a circular ``equipa.hooks`` ↔ ``equipa.monitoring``
    import cycle (monitoring's git helpers also depend on hook event names).
    Any failure mode (missing dir, git not installed, timeout) falls back to
    ``False`` so the guard fails closed — i.e. we do not silently swallow a
    truly vacuous attempt just because git was unreachable.
    """
    if not project_dir:
        return False
    try:
        from equipa.monitoring import has_branch_commits
    except ImportError:  # pragma: no cover — defensive only
        logger.debug("has_branch_commits unavailable; skipping branch check")
        return False
    try:
        return bool(has_branch_commits(str(project_dir)))
    except Exception:  # pragma: no cover — defensive only
        logger.exception("has_branch_commits raised while checking %r", project_dir)
        return False


def check_vacuous_pass(**kwargs: Any) -> dict[str, Any]:
    """Return ``{"vacuous": bool, "reason": str}`` for the current attempt.

    Inspects (in priority order):
        1. ``dev_result.files_changed`` — the current cycle's delta.
        2. ``files_changed`` kwarg — explicit override (callers may pass
           a pre-parsed list).
        3. ``accumulated_files`` kwarg — set of files across ALL cycles
           in the current dev-test loop. Wins over per-cycle empty deltas.
        4. ``project_dir`` kwarg — if the branch has real commits past
           its merge-base, treat the attempt as non-vacuous regardless
           of per-cycle ``files_changed``.
    """
    tester_result = kwargs.get("tester_result") or {}
    dev_result = kwargs.get("dev_result") or {}
    task = kwargs.get("task") or {}

    per_cycle_raw = dev_result.get("files_changed") or kwargs.get("files_changed") or []
    accumulated_raw = kwargs.get("accumulated_files") or []
    project_dir = kwargs.get("project_dir")

    files_changed = _normalize_files(per_cycle_raw)
    accumulated_files = _normalize_files(accumulated_raw)

    tests_run = int(tester_result.get("tests_run") or 0)
    tester_outcome = (tester_result.get("result") or "").lower()

    # Task #2079: union per-cycle delta with the accumulated cross-cycle
    # set BEFORE applying the trivial/docs-only checks. Without this, the
    # guard reports a false-positive on the "agent commits early then
    # idles" pattern (task #2074 was the first repro).
    combined_for_trivial = list(dict.fromkeys(files_changed + accumulated_files))

    # Case 1: tester reports a "pass" but nothing meaningful was changed.
    if tester_outcome in {"pass", "no-tests"} and _is_trivial_change(combined_for_trivial):
        # Last-chance escape hatch: branch-level commits past merge-base
        # mean the agent shipped real work earlier, even if both the
        # per-cycle and accumulated views are empty (e.g. when the loop
        # was resumed from a checkpoint and accumulated_files is stale).
        if _branch_has_real_work(project_dir):
            return {
                "vacuous": False,
                "reason": (
                    "non_vacuous: branch has commits past merge-base "
                    f"(per-cycle files_changed={files_changed!r}, "
                    f"accumulated={accumulated_files!r})"
                ),
            }
        return {
            "vacuous": True,
            "reason": (
                f"vacuous_pass: tester={tester_outcome} tests_run={tests_run} "
                f"files_changed={files_changed!r} "
                f"accumulated={accumulated_files!r}"
            ),
        }

    # Case 2: code-change verb in task description, but only docs touched.
    # Apply the same widening: if accumulated_files contains real code
    # OR the branch already has commits, the docs-only-this-cycle pattern
    # is not vacuous.
    description = (task.get("description") or task.get("title") or "").lower()
    if any(verb in description for verb in _CODE_CHANGE_VERBS):
        if _is_docs_only(combined_for_trivial) and not _branch_has_real_work(project_dir):
            return {
                "vacuous": True,
                "reason": (
                    "vacuous_pass: task description requires code changes "
                    f"but only documentation was touched ({combined_for_trivial!r})"
                ),
            }

    return {"vacuous": False, "reason": ""}


def register(dispatcher: Any) -> None:
    """Register the vacuous-pass callbacks on the colon-namespaced events."""
    dispatcher.on("dev_test:fail", check_vacuous_pass)
    dispatcher.on("attempt:after", check_vacuous_pass)
