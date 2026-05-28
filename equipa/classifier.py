"""Outcome-classification helpers for the dev-test loop.

Background (task #2481):
The dev-test loop's tester-outcome dispatcher (``loops._dispatch_tester_outcome``)
used to map ``test_outcome == "no-tests"`` directly to the run outcome
``"no_tests"``, *without* consulting whether the developer actually wrote
test files in this run. In task #2476 the developer agent wrote a new
regression test (``tests/test_review_artifact_paths.py``) yet the
orchestrator still classified the outcome as ``no_tests``. That is a
contradiction — if the tester reports "no tests found" while the
developer's ``files_changed_set`` clearly contains test files, the tester
ran against a stale view of the worktree or failed to discover the new
files. Returning ``no_tests`` in that case silently downgrades the run.

This module centralizes the predicates used to detect that contradiction
so the dispatcher in ``equipa/loops.py`` and the regression tests in
``tests/test_outcome_classifier.py`` share a single source of truth.
The pre-existing vacuous-pass guard (``equipa/hooks/vacuous_pass.py``,
fixed 2026-04-17) is NOT weakened — its protection runs upstream of this
classifier on the ``pass`` branch.

Copyright 2026 Forgeborn
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

_TEST_FILENAME_RE = re.compile(
    r"""
    (?:^|/)                 # path boundary
    (?:
        test_[^/]+\.py$     # test_foo.py — Python/pytest convention
        | [^/]+_test\.py$   # foo_test.py — Go-style / alt pytest convention
        | conftest\.py$     # pytest fixtures
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Any path that lives inside a `tests/` (or `test/`) directory at any depth
# also counts as a test file even if its leaf doesn't match the naming
# conventions above. This matches the agent prompts that ask testers to
# place new tests under a `tests/` subtree.
_TESTS_DIR_RE = re.compile(r"(?:^|/)(?:tests?)(?:/|$)", re.IGNORECASE)


def is_test_file(path: str) -> bool:
    """Return True if ``path`` looks like a test file by name or directory.

    Recognized shapes (case-insensitive):
        - ``test_*.py`` at any depth (pytest convention)
        - ``*_test.py`` at any depth (Go-style / alt pytest convention)
        - ``conftest.py`` at any depth (pytest fixtures)
        - any path under a ``tests/`` or ``test/`` directory at any depth
    """
    if not isinstance(path, str) or not path:
        return False
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return False
    if _TEST_FILENAME_RE.search(normalized):
        return True
    if _TESTS_DIR_RE.search(normalized):
        return True
    return False


def wrote_test_files(files: Iterable[str] | None) -> bool:
    """Return True if any element of ``files`` matches ``is_test_file``."""
    if not files:
        return False
    for path in files:
        if is_test_file(path):
            return True
    return False


def _collect_files(turn: dict[str, Any]) -> list[str]:
    """Pull a list of file paths out of a turn-summary dict.

    Accepts both ``files_changed`` (legacy alias used in some loop callers
    and in our test fixtures) and ``files_changed_set`` (the authoritative
    key populated by ``agent_runner.run_agent``).
    """
    for key in ("files_changed_set", "files_changed"):
        value = turn.get(key)
        if isinstance(value, (list, tuple, set)):
            return [str(p) for p in value]
    return []


def classify_outcome(
    turns: Sequence[dict[str, Any]],
    *,
    repo_has_tests: bool = True,
) -> str:
    """Classify a sequence of agent turns into an outcome string.

    This is the test-facing surface for the dispatcher's logic — it folds
    together the developer and tester turn summaries and returns one of:

        - ``"tests_passed"`` — at least one test file was written or
          existed in the repo, AND at least one turn reports tests_passed
          > 0, AND no turn reports tests_failed > 0.
        - ``"tests_failed"`` — any turn reports tests_failed > 0.
        - ``"no_tests"`` — no test files written, no tests in repo,
          no pass/fail signal.

    The vacuous-pass guard (``equipa/hooks/vacuous_pass.py``) runs upstream
    of this in real dispatch and rejects pass signals with zero file
    changes before they reach the classifier; this function preserves
    that invariant by treating a tests_passed signal with zero file
    changes as suspicious.

    Args:
        turns: ordered list of turn-summary dicts, each with optional
            ``files_changed`` / ``files_changed_set`` and optional
            ``tests_passed`` / ``tests_failed`` integer counts.
        repo_has_tests: True if the repository already contains tests
            independent of this run. When False *and* the agent wrote no
            test files *and* no pass/fail signal was emitted, the outcome
            is ``"no_tests"``.
    """
    all_files: list[str] = []
    any_tests_passed = False
    any_tests_failed = False
    any_files_written = False

    for turn in turns:
        files = _collect_files(turn)
        if files:
            any_files_written = True
        all_files.extend(files)
        passed = turn.get("tests_passed")
        failed = turn.get("tests_failed")
        if isinstance(passed, int) and not isinstance(passed, bool) and passed > 0:
            any_tests_passed = True
        if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
            any_tests_failed = True

    # Hard failure wins — tests existed and at least one failed.
    if any_tests_failed:
        return "tests_failed"

    wrote_tests = wrote_test_files(all_files)

    # Vacuous-pass guard preservation: a "pass" signal with zero file
    # changes is suspicious and must NOT be promoted to tests_passed.
    if any_tests_passed and not any_files_written:
        return "tests_inconclusive"

    if any_tests_passed:
        return "tests_passed"

    # No explicit pass/fail signal. If the developer wrote test files,
    # but no tester confirmed passing, treat as inconclusive — this is
    # the exact scenario that produced the task #2476 misclassification
    # and the codepath this module exists to fix.
    if wrote_tests:
        return "tests_inconclusive"

    if not repo_has_tests:
        return "no_tests"

    return "no_tests"
