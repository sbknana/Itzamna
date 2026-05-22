"""Regression tests for ``equipa.hooks.vacuous_pass.check_vacuous_pass``.

Task #2079: the guard previously fired on the early-commit / idle-later
pattern. An agent could commit all its work in cycles 1-2 and report
``FILES_CHANGED: none`` in cycles 3-5 (legitimate idle, branch already
has the real work), and the guard would falsely classify the attempt
as vacuous because it only saw the empty per-cycle delta.

The fix widens the file-change view to include:
    * ``accumulated_files`` kwarg — union of files touched across all
      cycles in the current dev-test loop.
    * Git branch state — if ``has_branch_commits(project_dir)`` returns
      ``True``, the agent shipped real work even when both the per-cycle
      and accumulated views are empty (e.g. checkpoint-resumed loops).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from typing import Any

import pytest

from equipa.hooks import vacuous_pass as vp


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _result(
    outcome: str = "pass",
    tests_run: int = 0,
    tests_passed: int | None = None,
    tests_failed: int = 0,
    tests_skipped: int = 0,
    tests_skipped_present: bool = True,
) -> dict[str, Any]:
    """Build a minimal tester_result payload.

    Task #2242 Phase A: ``tests_skipped_present`` defaults to True so the
    common case (tester emitted the field) is the default. Tests that
    exercise the contract-violation path set ``tests_skipped_present=False``
    explicitly, or omit the key entirely.
    """
    if tests_passed is None:
        tests_passed = tests_run
    payload: dict[str, Any] = {
        "result": outcome,
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_skipped": tests_skipped,
    }
    if tests_skipped_present:
        payload["tests_skipped_present"] = True
    return payload


def _dev(files_changed: list[str] | str | None = None) -> dict[str, Any]:
    """Build a minimal dev_result payload."""
    if files_changed is None:
        files_changed = []
    return {"files_changed": files_changed}


# ---------------------------------------------------------------------------
# baseline behavior — the legacy heuristic must still fire on true vacuous
# ---------------------------------------------------------------------------


def test_truly_vacuous_pass_with_no_changes_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tester says pass, no per-cycle and no accumulated files → vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result=_dev([]),
        task={"description": "implement foo"},
        accumulated_files=[],
        project_dir="/nonexistent",
    )
    assert out["vacuous"] is True
    assert "vacuous_pass" in out["reason"]


def test_state_file_only_change_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Touching only the .forge-state.json sentinel is still vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("no-tests"),
        dev_result=_dev([".forge-state.json"]),
        task={"description": "add validation"},
        accumulated_files=[".forge-state.json"],
        project_dir=None,
    )
    assert out["vacuous"] is True


def test_docs_only_with_implement_verb_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code-change verb + docs-only diff → vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 3),
        dev_result=_dev(["README.md", "CHANGELOG.md"]),
        task={"description": "implement new endpoint"},
        accumulated_files=["README.md", "CHANGELOG.md"],
        project_dir=None,
    )
    assert out["vacuous"] is True
    assert "documentation" in out["reason"]


# ---------------------------------------------------------------------------
# task-2079 fix — early-commit / idle-later pattern must NOT flag
# ---------------------------------------------------------------------------


def test_idle_cycle_with_accumulated_files_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-cycle empty BUT accumulated_files has real code → not vacuous.

    This is the exact pattern observed on task #2074: cycles 1-2 wrote
    code, cycles 3-5 idled, and the guard fired anyway.
    """
    # Branch check intentionally False — we want the accumulated_files
    # path to be load-bearing here.
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result=_dev([]),  # cycle 5 wrote nothing
        task={"description": "implement foo"},
        accumulated_files=["equipa/loops.py", "tests/test_loops.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False


def test_idle_cycle_with_branch_commits_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-cycle empty AND accumulated empty BUT branch has commits → not vacuous.

    Covers the checkpoint-resumed loop case: a fresh DevTestState starts
    with empty ``accumulated_files`` even though the worktree already
    contains committed work from a previous orchestrator session.
    """
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result=_dev([]),
        task={"description": "implement foo"},
        accumulated_files=[],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False
    assert "branch has commits" in out["reason"]


def test_docs_only_this_cycle_but_code_in_accumulated_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cycle touches only README, but prior cycles touched code → not vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 5),
        dev_result=_dev(["README.md"]),
        task={"description": "implement endpoint"},
        accumulated_files=["app/router.py", "tests/test_router.py", "README.md"],
        project_dir=None,
    )
    assert out["vacuous"] is False


def test_docs_only_with_branch_commits_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docs-only diff + branch already has code commits → not vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 1),
        dev_result=_dev(["docs/changelog.md"]),
        task={"description": "add caching"},
        accumulated_files=["docs/changelog.md"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False


# ---------------------------------------------------------------------------
# edge cases — input normalization and missing kwargs
# ---------------------------------------------------------------------------


def test_files_changed_as_multiline_string_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler accepts FILES_CHANGED as a newline-delimited string."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result={"files_changed": "- app/main.py\n- tests/test_main.py"},
        task={"description": "fix bug"},
        accumulated_files=[],
        project_dir=None,
    )
    assert out["vacuous"] is False


def test_no_project_dir_falls_through_to_per_cycle_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing project_dir → branch check returns False, no crash."""
    # Force the real lookup; should silently return False.
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result=_dev([]),
        task={"description": "implement foo"},
        accumulated_files=[],
        project_dir=None,
    )
    assert out["vacuous"] is True


def test_branch_check_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """has_branch_commits crashing must not propagate — guard fails closed."""

    def _boom(_pd: Any) -> bool:
        raise RuntimeError("git not installed")

    # Patch the imported helper via the lazy import path.
    import equipa.monitoring as monitoring

    monkeypatch.setattr(monitoring, "has_branch_commits", _boom)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 0),
        dev_result=_dev([]),
        task={"description": "implement foo"},
        accumulated_files=[],
        project_dir="/some/repo",
    )
    # Falls back to vacuous=True because the branch check returned False
    # (fails closed — see _branch_has_real_work docstring).
    assert out["vacuous"] is True


def test_non_code_task_description_skips_docs_only_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task description without code-change verbs → docs-only is fine."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result=_result("pass", 1),
        dev_result=_dev(["README.md"]),
        task={"description": "review the changelog"},
        accumulated_files=["README.md"],
        project_dir=None,
    )
    assert out["vacuous"] is False


# ---------------------------------------------------------------------------
# task-2242 fix — all-tests-skipped must be flagged as vacuous regardless
# of how much code the developer wrote (GutenForge Stage D loophole)
# ---------------------------------------------------------------------------


def test_all_tests_skipped_is_flagged_even_with_real_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tester reports pass but every test was skipped → vacuous.

    Exact repro of task #2236 (Stage D): 982 LOC delivered, but the spec
    is gated by ``test.skip()`` on missing env vars. Tester reports
    "pass (0/1 passed)" with tests_skipped=1. Without this guard, the
    orchestrator marks the task done despite zero validation occurring.
    """
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=1, tests_passed=0, tests_skipped=1
        ),
        dev_result=_dev(["app/feature.py", "tests/test_feature.py"]),
        task={"description": "implement async AI generation"},
        accumulated_files=["app/feature.py", "tests/test_feature.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert out["all_skipped"] is True
    assert "all_tests_skipped" in out["reason"]


def test_all_skipped_with_many_tests_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tests_run=42 / tests_skipped=42 / tests_passed=0 → still vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=42, tests_passed=0, tests_skipped=42
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "add caching"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert out["all_skipped"] is True


def test_partial_skip_with_some_passes_is_not_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tests_passed > 0 → at least one test ran for real, not vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=10, tests_passed=7, tests_skipped=3
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False
    assert out.get("all_skipped") is False


def test_all_skipped_overrides_branch_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """all-skipped detector fires even when branch has real commits.

    The branch-has-commits escape hatch was added for the early-commit /
    idle-later pattern (task #2079). It must NOT mask the all-skipped
    pattern — the work being committed is exactly what task #2236 had,
    yet no validation occurred.
    """
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=1, tests_passed=0, tests_skipped=1
        ),
        dev_result=_dev([]),
        task={"description": "implement async generation"},
        accumulated_files=["already.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert out["all_skipped"] is True


def test_zero_tests_run_with_skip_zero_is_not_all_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tests_run=0 means no tests existed — that's no-tests, not all-skipped."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "no-tests", tests_run=0, tests_passed=0, tests_skipped=0
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False
    assert out.get("all_skipped") is False


def test_missing_tests_skipped_field_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #2242 Phase A: omitting TESTS_SKIPPED is a contract violation.

    A tester that reports pass with tests_run > 0 but does NOT emit the
    TESTS_SKIPPED line cannot be trusted — the field is REQUIRED. Without
    this fail-closed behavior the loophole reopens silently on tester
    drift / truncation / regression. We must not default the field to 0.
    """
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        # NOTE: no tests_skipped_present key — simulates legacy / drifted tester
        tester_result={"result": "pass", "tests_run": 5, "tests_passed": 5},
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert "missing_tests_skipped_field" in out["reason"]


def test_tests_skipped_present_false_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit tests_skipped_present=False is treated as contract violation."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=5, tests_passed=5, tests_skipped_present=False
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert "missing_tests_skipped_field" in out["reason"]


def test_counts_inconsistent_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #2242 Phase C: passed+failed+skipped != tests_run -> vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=10, tests_passed=0, tests_failed=0, tests_skipped=3,
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert "counts_inconsistent" in out["reason"]


def test_counts_consistent_partial_skip_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #2242 Phase C: consistent partial skip is not vacuous."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=10, tests_passed=7, tests_failed=0, tests_skipped=3,
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is False


def test_phase_d_equality_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #2242 Phase D: predicate is tests_skipped == tests_run (not >=).

    A run with skipped > run is internally inconsistent and is caught by
    Phase C upstream, but the all_skipped path itself uses strict equality
    to match the docstring, prompt, and external contract.
    """
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: True)
    out = vp.check_vacuous_pass(
        tester_result=_result(
            "pass", tests_run=5, tests_passed=0, tests_skipped=5,
        ),
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    assert out["vacuous"] is True
    assert out["all_skipped"] is True
    assert "all_tests_skipped" in out["reason"]


def test_fail_outcome_with_skips_is_not_classified_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing run with skips is still a failure, not the all-skipped path."""
    monkeypatch.setattr(vp, "_branch_has_real_work", lambda _pd: False)
    out = vp.check_vacuous_pass(
        tester_result={
            "result": "fail",
            "tests_run": 5,
            "tests_passed": 0,
            "tests_skipped": 5,
        },
        dev_result=_dev(["src/main.py"]),
        task={"description": "implement feature"},
        accumulated_files=["src/main.py"],
        project_dir="/some/repo",
    )
    # fail outcome short-circuits before the all-skipped check; the
    # all-skipped detector only fires on pass/no-tests.
    assert out.get("all_skipped", False) is False
