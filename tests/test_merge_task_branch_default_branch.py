"""Tests for task #2493: ``_merge_task_branch`` must always merge into the
DEFAULT branch and leave the main checkout on it afterwards.

BUG (reproduced on #2489/#2492): in single-task ``--task <id>`` mode the
orchestrator runs in the MAIN checkout with HEAD already ON the per-task
branch ``forge-task-<id>``. The old code counted ``HEAD..<branch>`` (an empty
range, since HEAD == the branch) and skipped the merge as a silent no-op, so
the branch never landed on the default branch.

These tests prove BOTH dispatch modes still merge correctly:

* single-task  — HEAD on ``forge-task-<id>`` (same repo, no worktree); the
  helper checks out the default branch, merges, and ends on the default
  branch.
* parallel     — HEAD on the default branch, task branch ahead (worktree);
  the helper still merges with no regression.

plus the invariants the fix must NOT weaken:

* the #2451 security gate still raises ``SecurityGateBypassError`` on a
  HIGH/CRITICAL review;
* ``expect_artifact=False`` doc-only diffs merge without demanding an
  artifact;
* a missing task branch still returns ``False`` with the #2488 ERROR.

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

from equipa.dispatch import _merge_task_branch as _merge_task_branch_async
from equipa.git_ops import _clear_default_branch_cache
from equipa.security_gate import SecurityGateBypassError


def _merge(project_dir, task_id, branch_name, *, expect_artifact=True):
    return asyncio.run(
        _merge_task_branch_async(
            str(project_dir), task_id, branch_name,
            expect_artifact=expect_artifact,
        ),
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _init_repo(path: Path, *, default: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", default)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("init\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    # Detection caches per resolved path for 5 min; clear so a fresh repo at
    # the same tmp_path is not shadowed by a previous run's result.
    _clear_default_branch_cache()


def _write_clean_review(repo: Path, task_id: int) -> None:
    (repo / f"SECURITY-REVIEW-{task_id}.md").write_text(
        "# Security Review\n\nNo blocking findings.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )


def _write_blocking_review(repo: Path, task_id: int) -> None:
    (repo / f"SECURITY-REVIEW-{task_id}.md").write_text(
        "# Security Review\n\nBlocking findings present.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )


def _commit_on_new_branch(repo: Path, branch: str, fname: str, n: int = 1) -> None:
    """Create ``branch`` off the current HEAD and add ``n`` commits to it."""
    _git(repo, "checkout", "-b", branch)
    for i in range(n):
        (repo / f"{fname}{i}.txt").write_text(f"content {i}\n")
        _git(repo, "add", f"{fname}{i}.txt")
        _git(repo, "commit", "-m", f"feat: {fname} commit {i}")


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not available")


# --- single-task mode (the #2493 bug) ---


def test_single_task_head_on_task_branch_merges_to_default(tmp_path, capsys):
    """HEAD on forge-task-<id> with N commits ahead of the default branch:
    the helper checks out the default branch, merges, returns True, and
    leaves HEAD on the default branch with the N commits applied.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="main")
    default_head_before = _git(repo, "rev-parse", "main").stdout.strip()

    # Single-task mode: a real local branch in the SAME repo, HEAD left on it.
    _commit_on_new_branch(repo, "forge-task-100", "feature", n=3)
    _write_clean_review(repo, 100)
    assert _current_branch(repo) == "forge-task-100"
    capsys.readouterr()

    ok = _merge(repo, 100, "forge-task-100")

    assert ok is True
    # Merge landed on the default branch...
    assert _git(repo, "rev-parse", "main").stdout.strip() != default_head_before
    # ...all 3 commits are now reachable from main...
    log = _git(repo, "log", "--oneline", "main").stdout
    assert log.count("feat: feature commit") == 3
    # ...and the checkout was left on the default branch for the next dispatch.
    assert _current_branch(repo) == "main"
    out = capsys.readouterr().out
    assert "3 commit(s) to merge" in out


def test_single_task_works_when_default_is_master(tmp_path):
    """Default-branch detection must not hardcode main: a master-default
    repo in single-task mode merges onto master and ends there.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="master")
    _commit_on_new_branch(repo, "forge-task-101", "feature", n=1)
    _write_clean_review(repo, 101)
    assert _current_branch(repo) == "forge-task-101"

    ok = _merge(repo, 101, "forge-task-101")

    assert ok is True
    assert _current_branch(repo) == "master"
    assert "feat: feature commit 0" in _git(repo, "log", "--oneline", "master").stdout


# --- parallel mode (no regression) ---


def test_parallel_head_on_default_still_merges(tmp_path, capsys):
    """HEAD on the default branch, task branch ahead — the classic parallel
    --tasks/worktree case must still merge with no regression.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="main")
    default_head_before = _git(repo, "rev-parse", "main").stdout.strip()

    # Build the task branch, then return HEAD to the default branch (the
    # state the main checkout sits in during parallel dispatch).
    _commit_on_new_branch(repo, "forge-task-200", "parallel", n=2)
    _git(repo, "checkout", "main")
    _write_clean_review(repo, 200)
    assert _current_branch(repo) == "main"
    capsys.readouterr()

    ok = _merge(repo, 200, "forge-task-200")

    assert ok is True
    assert _git(repo, "rev-parse", "main").stdout.strip() != default_head_before
    assert _current_branch(repo) == "main"
    out = capsys.readouterr().out
    assert "2 commit(s) to merge" in out


# --- preserved invariants ---


def test_gate_blocks_merge_on_high_finding(tmp_path):
    """A HIGH finding in the review artifact still raises
    SecurityGateBypassError and never merges (#2451 invariant).
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="main")
    default_head_before = _git(repo, "rev-parse", "main").stdout.strip()
    _commit_on_new_branch(repo, "forge-task-300", "risky", n=1)
    _write_blocking_review(repo, 300)

    with pytest.raises(SecurityGateBypassError):
        _merge(repo, 300, "forge-task-300")

    # No merge happened: the default branch is untouched.
    assert _git(repo, "rev-parse", "main").stdout.strip() == default_head_before


def test_doc_only_merges_without_artifact(tmp_path):
    """expect_artifact=False (doc-only diff) merges without demanding a
    SECURITY-REVIEW artifact (#2451 Phase H F-01 handling preserved).
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="main")
    default_head_before = _git(repo, "rev-parse", "main").stdout.strip()
    _commit_on_new_branch(repo, "forge-task-400", "docs", n=1)
    # Deliberately NO review artifact written.
    assert not (repo / "SECURITY-REVIEW-400.md").exists()

    ok = _merge(repo, 400, "forge-task-400", expect_artifact=False)

    assert ok is True
    assert _git(repo, "rev-parse", "main").stdout.strip() != default_head_before
    assert _current_branch(repo) == "main"


def test_missing_branch_returns_false_with_2488_error(tmp_path, capsys):
    """A nonexistent task branch still returns False with the #2488 ERROR,
    never a silent no-op.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, default="main")
    _write_clean_review(repo, 500)
    capsys.readouterr()

    ok = _merge(repo, 500, "forge-task-does-not-exist")

    assert ok is False
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "does NOT exist" in out
    assert "forge-task-does-not-exist" in out
