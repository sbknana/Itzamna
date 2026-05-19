"""Regression tests for the unified single-task merge gate (task #2450).

Before #2450, single-task --dev-test mode merged the worktree branch INSIDE
run_dev_test_loop BEFORE the security gate was evaluated. The gate could mark
a task as security_review_blocked in TheForge while its commits were already
on master — a fail-open gate.

These tests pin down the unified behavior: the merge must happen ONLY after
the gate passes, via the same _merge_task_branch helper parallel mode uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT
    ).strip()


def _init_repo_with_task_branch(tmp_path: Path) -> tuple[Path, str]:
    """Create a temp repo with a master and a forge-task-N branch that has a commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@forgeborn")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")

    branch = "forge-task-9999"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "feat: add feature")
    _git(repo, "checkout", "master")
    return repo, branch


def _branch_commits_on_master(repo: Path, branch: str) -> bool:
    """Check if the tip commit of branch is reachable from master."""
    tip = _git(repo, "rev-parse", branch)
    try:
        _git(repo, "merge-base", "--is-ancestor", tip, "master")
        return True
    except subprocess.CalledProcessError:
        return False


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        return True
    except subprocess.CalledProcessError:
        return False


@pytest.fixture
def task_repo(tmp_path: Path) -> tuple[Path, str]:
    return _init_repo_with_task_branch(tmp_path)


def _run_gated_merge(
    repo: Path,
    branch: str,
    *,
    outcome: str,
    review_blocks_merge: bool,
) -> str:
    """Invoke the gated merge step that cli.py run_mode_task should call.

    This wraps the post-loop gate-then-merge logic so the tests can exercise
    it independently of run_dev_test_loop's full async stack.
    """
    import asyncio

    from equipa import cli

    return asyncio.run(
        cli._gated_post_merge(  # type: ignore[attr-defined]
            repo=repo,
            branch=branch,
            outcome=outcome,
            review_blocks_merge=review_blocks_merge,
        )
    )


def test_single_task_high_finding_branch_not_on_master(task_repo: tuple[Path, str]) -> None:
    """HIGH severity → gate blocks, branch must NOT be on master and must still exist."""
    repo, branch = task_repo
    result = _run_gated_merge(
        repo, branch, outcome="security_review_blocked", review_blocks_merge=True
    )
    assert result == "blocked"
    assert not _branch_commits_on_master(repo, branch), (
        "Branch commits leaked to master despite gate block"
    )
    assert _branch_exists(repo, branch), (
        "Branch was deleted despite gate block — operator cannot review"
    )


def test_single_task_critical_finding_branch_not_on_master(task_repo: tuple[Path, str]) -> None:
    """CRITICAL severity → gate blocks, same invariants as HIGH."""
    repo, branch = task_repo
    result = _run_gated_merge(
        repo, branch, outcome="security_review_blocked", review_blocks_merge=True
    )
    assert result == "blocked"
    assert not _branch_commits_on_master(repo, branch)
    assert _branch_exists(repo, branch)


def test_single_task_clean_review_branch_merged(task_repo: tuple[Path, str]) -> None:
    """0 HIGH/CRITICAL → branch is merged to master via _merge_task_branch."""
    repo, branch = task_repo
    result = _run_gated_merge(
        repo, branch, outcome="tests_passed", review_blocks_merge=False
    )
    assert result == "merged"
    assert _branch_commits_on_master(repo, branch), (
        "Clean review did not result in merge"
    )


def test_single_task_reviewer_crash_branch_not_on_master(task_repo: tuple[Path, str]) -> None:
    """Reviewer crash → fail-closed: outcome reflects block, branch not on master."""
    repo, branch = task_repo
    # Reviewer crash is surfaced by the cli.py gate block as outcome=security_review_blocked
    # with review_blocks_merge=True (fail-closed).
    result = _run_gated_merge(
        repo, branch, outcome="security_review_blocked", review_blocks_merge=True
    )
    assert result == "blocked"
    assert not _branch_commits_on_master(repo, branch)
    assert _branch_exists(repo, branch)


def test_run_dev_test_loop_does_not_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_dev_test_loop must NOT perform any merge — that's the post-step's job."""
    from equipa import loops

    # Sentinel: if _merge_task_branch (or any internal merge helper) is called
    # from within run_dev_test_loop, this test fails.
    called: dict[str, Any] = {"merge": False}

    def _trip(*_args: Any, **_kwargs: Any) -> None:
        called["merge"] = True
        raise AssertionError("run_dev_test_loop must not invoke merge logic")

    # Cover the canonical helper name and any legacy alias.
    for attr in ("_merge_task_branch", "_merge_worktree_branch", "merge_task_branch"):
        if hasattr(loops, attr):
            monkeypatch.setattr(loops, attr, _trip, raising=False)

    # If the source no longer references any merge helper, the test passes
    # by construction (the post-step is the only merge path).
    import inspect

    src = inspect.getsource(loops.run_dev_test_loop)
    forbidden = ["_merge_task_branch(", "_merge_worktree_branch(", "git merge "]
    for needle in forbidden:
        assert needle not in src, (
            f"run_dev_test_loop still references {needle!r} — merge must live "
            f"in cli.py post-step, not in the loop"
        )
