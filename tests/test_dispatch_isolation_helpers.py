"""Unit tests for the worktree-isolation helpers extracted from
run_parallel_tasks: _create_isolation_worktrees, _merge_task_branch,
and _cleanup_worktrees.

These helpers exist because run_parallel_tasks was a 295-line function
in which silent ``except Exception: pass`` made worktree-merge data-loss
(open question #332) impossible to diagnose. The tests here exercise:

1. happy path — helper does its job, prints expected breadcrumbs
2. error path — helper logs the error explicitly and does NOT swallow it

Tests use real ``git`` subprocess calls inside ``tmp_path`` repos so the
helper logic is exercised end-to-end without mocking subprocess.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from equipa.dispatch import (
    _cleanup_worktrees as _cleanup_worktrees_async,
    _create_isolation_worktrees as _create_isolation_worktrees_async,
    _merge_task_branch as _merge_task_branch_async,
)


def _create_isolation_worktrees(tasks, project_dir, base):
    return asyncio.run(
        _create_isolation_worktrees_async(tasks, project_dir, base),
    )


def _merge_task_branch(project_dir, task_id, branch_name):
    return asyncio.run(
        _merge_task_branch_async(project_dir, task_id, branch_name),
    )


def _cleanup_worktrees(project_dir, worktree_dirs, merged_tasks, base):
    return asyncio.run(
        _cleanup_worktrees_async(project_dir, worktree_dirs, merged_tasks, base),
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("init\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not available")


# --- run_parallel_tasks worktree gate (regression) ---


def test_run_parallel_tasks_does_not_gate_worktrees_on_task_count():
    """Regression: `--tasks N` with a single task ID must still isolate.

    The historical condition `use_worktrees = len(tasks) > 1 and ...`
    silently dropped isolation for single-task `--tasks` dispatches, so
    agents wrote directly to the project's working tree on master.
    Caught 2026-05-21 on cryptotrader-v2 task #2386.
    """
    import inspect
    import re
    from equipa import dispatch
    source = inspect.getsource(dispatch.run_parallel_tasks)
    # Match any RHS of `use_worktrees = ...` and forbid `len(tasks)` in it.
    # `len(tasks) > 1` is fine elsewhere (e.g. `use_flow`); only the
    # isolation gate must be task-count-independent.
    m = re.search(r"use_worktrees\s*=\s*([^\n]+)", source)
    assert m is not None, "use_worktrees assignment missing from run_parallel_tasks"
    assert "len(tasks)" not in m.group(1), (
        "run_parallel_tasks must not gate worktree creation on task count "
        "— single-task --tasks dispatches need isolation too. "
        f"Got: use_worktrees = {m.group(1).strip()}"
    )


# --- _create_isolation_worktrees ---


def test_create_isolation_worktrees_happy_path(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    tasks = [{"id": 1}, {"id": 2}]
    base = repo / ".forge-worktrees"

    result = _create_isolation_worktrees(tasks, str(repo), base)

    assert set(result.keys()) == {1, 2}
    assert (base / "task-1").is_dir()
    assert (base / "task-2").is_dir()
    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-1" in branches
    assert "forge-task-2" in branches
    out = capsys.readouterr().out
    assert "[Isolation] Task #1 -> task-1" in out
    assert "[Isolation] Task #2 -> task-2" in out


def test_create_isolation_worktrees_error_path_logs_warning(tmp_path, capsys):
    """If git can't create worktrees (bare/invalid repo), the helper
    must log a WARNING and return without the failed task — never crash
    or silently swallow the error.
    """
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    base = not_a_repo / ".forge-worktrees"

    result = _create_isolation_worktrees([{"id": 99}], str(not_a_repo), base)

    assert 99 not in result
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "task #99" in out


# --- _merge_task_branch ---


def _write_clean_review(repo, task_id):
    """Write a no-findings SECURITY-REVIEW-{task_id}.md.

    Required by the task #2451 defensive invariant: ``_merge_task_branch``
    fails closed when the artifact is missing or unparseable. Legacy helper
    tests that exercise the merge path must satisfy the invariant first.
    """
    (repo / f"SECURITY-REVIEW-{task_id}.md").write_text(
        "# Security Review\n\nNo blocking findings.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )


def test_merge_task_branch_happy_path(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    _create_isolation_worktrees([{"id": 7}], str(repo), base)
    capsys.readouterr()  # clear setup output

    wt = base / "task-7"
    (wt / "feature.txt").write_text("hello\n")
    _git(wt, "add", "feature.txt")
    _git(wt, "commit", "-m", "feat: add feature")
    _write_clean_review(repo, 7)

    pre_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ok = _merge_task_branch(str(repo), 7, "forge-task-7")
    post_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert ok is True
    assert pre_head != post_head
    out = capsys.readouterr().out
    assert "Merged task #7 into main" in out


def test_merge_task_branch_no_commits_returns_false(tmp_path, capsys):
    """A worktree with zero new commits must be skipped (False) and
    explicitly logged as 'NO commits ahead' — never silently treated as
    success.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    _create_isolation_worktrees([{"id": 5}], str(repo), base)
    _write_clean_review(repo, 5)
    capsys.readouterr()

    ok = _merge_task_branch(str(repo), 5, "forge-task-5")

    assert ok is False
    out = capsys.readouterr().out
    assert "NO commits ahead of HEAD" in out


def test_merge_task_branch_missing_branch_logs_explicitly(tmp_path, capsys):
    """A nonexistent branch is still a failure that must be logged —
    not silently swallowed (the bug class behind open question #332).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_clean_review(repo, 42)

    ok = _merge_task_branch(str(repo), 42, "forge-task-does-not-exist")

    assert ok is False
    out = capsys.readouterr().out
    # Either "NO commits" (git log returns empty) or an explicit error log
    # — what matters is the helper does NOT silently return True.
    assert "task #42" in out or "forge-task-does-not-exist" in out


# --- _cleanup_worktrees ---


def test_cleanup_worktrees_deletes_merged_preserves_unmerged(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    worktree_dirs = _create_isolation_worktrees(
        [{"id": 10}, {"id": 11}], str(repo), base,
    )
    capsys.readouterr()

    # Pretend task 10 merged, task 11 did not.
    _cleanup_worktrees(str(repo), worktree_dirs, {10}, base)

    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-10" not in branches  # merged → deleted
    assert "forge-task-11" in branches      # unmerged → preserved
    out = capsys.readouterr().out
    assert "Keeping branch 'forge-task-11'" in out
    assert not (base / "task-10").exists()
    assert not (base / "task-11").exists()


def test_cleanup_worktrees_stashes_uncommitted_on_unmerged_branch(
    tmp_path, capsys,
):
    """Regression for task #2158 — when an early_terminated agent leaves
    uncommitted file changes in its worktree, cleanup must stash them on
    the branch BEFORE ``git worktree remove --force`` discards them.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    worktree_dirs = _create_isolation_worktrees(
        [{"id": 30}], str(repo), base,
    )
    wt_path = Path(worktree_dirs[30])

    # Simulate 32 turns of edits the agent never got to commit.
    (wt_path / "modified.py").write_text("print('important work')\n")
    _git(wt_path, "add", "modified.py")
    (wt_path / "untracked.txt").write_text("untracked but valuable\n")

    capsys.readouterr()
    # Empty merged_tasks set → task 30 is unmerged → must stash.
    _cleanup_worktrees(str(repo), worktree_dirs, set(), base)

    out = capsys.readouterr().out
    assert "stashed uncommitted work" in out
    assert "task-30" in out

    # Verify the stash actually exists on the preserved branch.
    stash_list = _git(repo, "stash", "list").stdout
    assert "equipa-early-term task-30" in stash_list

    # Branch is preserved even though work is stashed.
    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-30" in branches


def test_cleanup_worktrees_no_stash_when_clean(tmp_path, capsys):
    """If the worktree has no uncommitted changes, do NOT create an
    empty stash — keeps the stash list noise-free.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    worktree_dirs = _create_isolation_worktrees(
        [{"id": 31}], str(repo), base,
    )
    capsys.readouterr()
    _cleanup_worktrees(str(repo), worktree_dirs, set(), base)

    stash_list = _git(repo, "stash", "list").stdout
    assert "task-31" not in stash_list


def test_cleanup_worktrees_logs_errors_explicitly(tmp_path, capsys):
    """If git operations fail (e.g. invalid project_dir), the cleanup
    helper must LOG the error — the original code's
    ``except Exception: pass`` is exactly the bug we are fixing.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = repo / ".forge-worktrees"
    worktree_dirs = _create_isolation_worktrees(
        [{"id": 20}], str(repo), base,
    )
    capsys.readouterr()

    # Point cleanup at a nonexistent project_dir → subprocess.run with
    # cwd=bad_dir raises FileNotFoundError. The helper must catch it and
    # LOG explicitly (not silently `pass` like the pre-refactor code).
    bad_dir = str(tmp_path / "definitely-not-a-repo")
    _cleanup_worktrees(bad_dir, worktree_dirs, set(), base)

    out = capsys.readouterr().out
    # Helper must produce an explicit "Cleanup error" log line — this is
    # the exact behavior change vs. the old `except Exception: pass`.
    assert "Cleanup error for task #20" in out
    assert "forge-task-20" in out
