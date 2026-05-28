"""Regression test for task #2488.

Verifies orchestrator git worktree isolation:
- Each dispatched task gets its own fresh `forge-task-<id>` branch
  (never reuses a prior task's branch when the task description references it).
- Worktree creation refuses to reuse an existing branch name.
- Merge step raises a clear error if `forge-task-<id>` is missing, rather
  than silently logging "no commits ahead".

Bug background:
    Task #2486 left branch `forge-task-2486` checked out in the main checkout.
    Task #2487's description mentioned `git merge forge-task-2486` (ancestry).
    The orchestrator (a) had no separate worktree, (b) kept the prior branch
    checked out, and (c) committed #2487 work onto forge-task-2486. The merge
    step then said "branch forge-task-2487 has NO commits ahead of HEAD" and
    silently no-op'd the merge.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=check,
        text=True,
        capture_output=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "master"], path)
    _run(["git", "config", "user.email", "test@forgeborn.local"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


def _git_rev_parse(branch: str, cwd: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Simulate task #N having already run: create branch + a commit.
    _run(["git", "checkout", "-q", "-b", "forge-task-1000"], repo)
    (repo / "phase1.txt").write_text("phase 1 v2\n", encoding="utf-8")
    _run(["git", "add", "phase1.txt"], repo)
    _run(["git", "commit", "-q", "-m", "feat(1000): phase 1 v2"], repo)
    # Operator merged 1000 to master in the real bug scenario.
    _run(["git", "checkout", "-q", "master"], repo)
    _run(["git", "merge", "-q", "--no-ff", "-m", "merge 1000", "forge-task-1000"], repo)
    return repo


def test_create_worktree_makes_fresh_branch(tmp_git_repo: Path) -> None:
    """When orchestrator creates a worktree for task N+1, it must produce
    a new branch `forge-task-<N+1>` and NOT reuse `forge-task-<N>`."""
    from equipa.git_ops import create_task_worktree, remove_task_worktree

    wt = create_task_worktree(tmp_git_repo, task_id=1001, base_ref="master")
    try:
        # Branch must exist and be distinct from the prior task's branch.
        assert _git_rev_parse("forge-task-1001", tmp_git_repo) is not None
        assert wt.branch == "forge-task-1001"
        # Worktree path is separate from main checkout.
        assert wt.path.resolve() != tmp_git_repo.resolve()
        # `git worktree list` reports more than just the main checkout.
        listing = _run(["git", "worktree", "list", "--porcelain"], tmp_git_repo).stdout
        assert str(wt.path.resolve()) in listing or wt.path.name in listing
    finally:
        remove_task_worktree(tmp_git_repo, wt)


def test_create_worktree_refuses_existing_branch(tmp_git_repo: Path) -> None:
    """Defensive invariant: if `forge-task-<id>` already exists, refuse."""
    from equipa.git_ops import WorktreeBranchConflictError, create_task_worktree

    with pytest.raises(WorktreeBranchConflictError):
        # 1000 already exists from the fixture.
        create_task_worktree(tmp_git_repo, task_id=1000, base_ref="master")


def test_agent_commits_land_on_fresh_branch(tmp_git_repo: Path) -> None:
    """Simulate agent making a commit inside the worktree, then verify
    the commit lands on forge-task-1001 (not forge-task-1000)."""
    from equipa.git_ops import create_task_worktree, remove_task_worktree

    wt = create_task_worktree(tmp_git_repo, task_id=1001, base_ref="master")
    try:
        (wt.path / "phase1_v3.txt").write_text("phase 1 v3\n", encoding="utf-8")
        _run(["git", "add", "phase1_v3.txt"], wt.path)
        _run(["git", "commit", "-q", "-m", "feat(1001): phase 1 v3"], wt.path)

        head_1001 = _git_rev_parse("forge-task-1001", tmp_git_repo)
        head_1000 = _git_rev_parse("forge-task-1000", tmp_git_repo)
        assert head_1001 is not None
        assert head_1000 is not None
        assert head_1001 != head_1000, "commit must land on 1001, not 1000"

        # And the new commit IS ahead of master.
        ahead = _run(
            ["git", "rev-list", "--count", "master..forge-task-1001"],
            tmp_git_repo,
        ).stdout.strip()
        assert int(ahead) >= 1
    finally:
        remove_task_worktree(tmp_git_repo, wt)


def test_merge_step_errors_when_branch_missing(tmp_git_repo: Path) -> None:
    """Defensive check in merge step: if branch missing, raise explicit error
    rather than silently logging 'no commits ahead of HEAD'."""
    from equipa.git_ops import MissingTaskBranchError, merge_task_branch

    with pytest.raises(MissingTaskBranchError):
        merge_task_branch(tmp_git_repo, task_id=9999, target_ref="master")


def test_worktree_teardown_returns_main_to_master(tmp_git_repo: Path) -> None:
    """After task completion, tear-down should leave main checkout clean
    on master (not on the prior task's branch)."""
    from equipa.git_ops import create_task_worktree, remove_task_worktree

    wt = create_task_worktree(tmp_git_repo, task_id=1001, base_ref="master")
    remove_task_worktree(tmp_git_repo, wt)

    head_ref = _run(["git", "symbolic-ref", "HEAD"], tmp_git_repo).stdout.strip()
    assert head_ref.endswith("/master"), f"main checkout on {head_ref!r}, expected master"
    # Worktree directory gone.
    assert not wt.path.exists()
