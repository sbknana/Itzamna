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


def _result(outcome: str = "pass", tests_run: int = 0) -> dict[str, Any]:
    """Build a minimal tester_result payload."""
    return {"result": outcome, "tests_run": tests_run}


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
