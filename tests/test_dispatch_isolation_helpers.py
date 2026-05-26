"""Unit tests for equipa.worktree_manager — the canonical worktree-isolation
implementation that replaced the inline helpers in dispatch.py (Phase 1
convergence, 2026-05-23).

Tests exercise:
  1. create  — worktree + branch + DB row created
  2. merge_back — commits land on main, no-commits case handled
  3. destroy — stash-before-remove, branch cleanup
  4. mark_failed / mark_conflict — DB status transitions
  5. TaskWorkspace context manager — success and failure paths

Tests use real ``git`` subprocess calls inside ``tmp_path`` repos so the
worktree logic is exercised end-to-end. A temporary SQLite DB (from
schema.sql) replaces the production TheForge DB via monkeypatch.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import equipa.constants as constants
import equipa.db as db_mod
from equipa.worktree_manager import (
    STATUS_ACTIVE,
    STATUS_ABANDONED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    STATUS_MERGED,
    MERGE_LOCK_TIMEOUT_SECONDS,
    TaskWorkspace,
    WorktreeError,
    _merge_lock_path,
    cleanup_all_stale,
    cleanup_stale_worktree,
    create,
    destroy,
    list_active,
    list_stale,
    mark_conflict,
    mark_failed,
    merge_back,
    MergeResult,
)

REPO_ROOT_REAL = Path(__file__).resolve().parent.parent


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


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Create a temp git repo + temp DB with a project row pointing at it.

    Returns ``(repo_path, db_path, project_id)``.
    """
    db_path = tmp_path / "test_theforge.db"
    repo = tmp_path / "repo"
    _init_repo(repo)

    schema_sql = (REPO_ROOT_REAL / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, codename, local_path) VALUES (?, ?, ?)",
        ("IsoTest", "isotest", str(repo.resolve())),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()

    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", db_path)

    return repo, db_path, int(project_id)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# --- create ---


def test_create_happy_path(isolated_env):
    repo, db_path, project_id = isolated_env

    record = _run(create(1, str(repo)))

    assert record.task_id == 1
    assert record.project_id == project_id
    assert record.status == STATUS_ACTIVE
    assert record.branch == "forge-task-1"
    assert Path(record.path).is_dir()

    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-1" in branches

    gitignore = (repo / ".gitignore").read_text()
    assert "forge_worktrees/" in gitignore


def test_create_duplicate_raises(isolated_env):
    repo, _, _ = isolated_env

    _run(create(2, str(repo)))
    with pytest.raises(WorktreeError, match="already exists"):
        _run(create(2, str(repo)))


def test_create_orphan_branch_recovery(isolated_env):
    """If a branch exists from a prior failed run but the worktree path is
    gone, create() should delete the orphan branch and succeed."""
    repo, _, _ = isolated_env

    _git(repo, "branch", "forge-task-3")

    record = _run(create(3, str(repo)))
    assert record.status == STATUS_ACTIVE
    assert Path(record.path).is_dir()


# --- merge_back ---


def test_merge_back_happy_path(isolated_env):
    repo, _, _ = isolated_env
    record = _run(create(10, str(repo)))

    wt = Path(record.path)
    (wt / "feature.txt").write_text("hello\n")
    _git(wt, "add", "feature.txt")
    _git(wt, "commit", "-m", "feat: add feature")

    pre_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _run(merge_back(10))
    post_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert result.ok is True
    assert pre_head != post_head
    assert (repo / "feature.txt").exists()

    conn = sqlite3.connect(isolated_env[1])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (10,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_MERGED


def test_merge_back_no_commits(isolated_env):
    """A worktree with zero new commits should report ok=True with a
    'nothing to merge' message — not a failure."""
    repo, _, _ = isolated_env
    _run(create(11, str(repo)))

    result = _run(merge_back(11))

    assert result.ok is True
    assert "no commits" in result.message.lower()


def test_merge_back_no_active_worktree(isolated_env):
    result = _run(merge_back(999))
    assert result.ok is False
    assert "No active worktree" in result.message


# --- destroy ---


def test_destroy_removes_worktree_and_branch(isolated_env):
    repo, db_path, _ = isolated_env
    record = _run(create(20, str(repo)))

    _run(destroy(20))

    assert not Path(record.path).exists()
    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-20" not in branches

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (20,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_ABANDONED


def test_destroy_keep_branch(isolated_env):
    repo, _, _ = isolated_env
    _run(create(21, str(repo)))

    _run(destroy(21, keep_branch=True))

    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-21" in branches


def test_destroy_stashes_uncommitted_work(isolated_env):
    """Regression for task #2158 — when an early-terminated agent leaves
    uncommitted file changes in its worktree, destroy() must stash them on
    the branch BEFORE ``git worktree remove --force`` discards them.
    """
    repo, _, _ = isolated_env
    record = _run(create(30, str(repo)))
    wt = Path(record.path)

    (wt / "modified.py").write_text("print('important work')\n")
    _git(wt, "add", "modified.py")
    (wt / "untracked.txt").write_text("untracked but valuable\n")

    _run(destroy(30, keep_branch=True))

    assert not wt.exists()

    stash_list = _git(repo, "stash", "list").stdout
    assert "equipa-early-term task-30" in stash_list

    branches = _git(repo, "branch", "--list").stdout
    assert "forge-task-30" in branches


def test_destroy_no_stash_when_clean(isolated_env):
    """If the worktree has no uncommitted changes, do NOT create an
    empty stash."""
    repo, _, _ = isolated_env
    _run(create(31, str(repo)))

    _run(destroy(31, keep_branch=True))

    stash_list = _git(repo, "stash", "list").stdout
    assert "task-31" not in stash_list


# --- mark_failed / mark_conflict ---


def test_mark_failed(isolated_env):
    repo, db_path, _ = isolated_env
    record = _run(create(40, str(repo)))

    _run(mark_failed(40))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, ended_at FROM worktrees WHERE task_id = ?", (40,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_FAILED
    assert row["ended_at"] is not None
    assert Path(record.path).is_dir()


def test_mark_conflict(isolated_env):
    repo, db_path, _ = isolated_env
    _run(create(41, str(repo)))

    _run(mark_conflict(41, MergeResult(ok=False, message="test conflict")))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (41,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_CONFLICT


# --- list_active ---


def test_list_active(isolated_env):
    repo, _, project_id = isolated_env
    _run(create(50, str(repo)))
    _run(create(51, str(repo)))
    _run(mark_failed(51))

    active = _run(list_active(project_id))
    assert len(active) == 1
    assert active[0].task_id == 50


# --- TaskWorkspace ---


def test_task_workspace_success_merges_and_destroys(isolated_env):
    repo, db_path, _ = isolated_env

    async def _run_ws():
        async with TaskWorkspace(60, str(repo), isolate=True) as ws:
            wt = Path(ws.path)
            assert wt != repo
            (wt / "ws_feature.txt").write_text("from workspace\n")
            _git(wt, "add", "ws_feature.txt")
            _git(wt, "commit", "-m", "workspace commit")
            ws.set_success(True)

    _run(_run_ws())

    assert (repo / "ws_feature.txt").exists()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (60,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_MERGED


def test_task_workspace_failure_retains_worktree(isolated_env):
    repo, db_path, _ = isolated_env

    async def _run_ws():
        async with TaskWorkspace(61, str(repo), isolate=True) as ws:
            (Path(ws.path) / "wip.txt").write_text("work in progress\n")

    _run(_run_ws())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, path FROM worktrees WHERE task_id = ?", (61,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_FAILED
    assert Path(row["path"]).is_dir()


def test_task_workspace_exception_marks_failed(isolated_env):
    repo, db_path, _ = isolated_env

    async def _run_ws():
        async with TaskWorkspace(62, str(repo), isolate=True) as ws:
            raise RuntimeError("agent crashed")

    with pytest.raises(RuntimeError, match="agent crashed"):
        _run(_run_ws())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (62,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_FAILED


def test_task_workspace_no_isolation(isolated_env):
    repo, _, _ = isolated_env

    async def _run_ws():
        async with TaskWorkspace(63, str(repo), isolate=False) as ws:
            assert ws.path == str(repo)

    _run(_run_ws())


def test_task_workspace_non_git_dir_falls_back(tmp_path, isolated_env):
    """A non-git directory should fall back to non-isolated mode."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    async def _run_ws():
        async with TaskWorkspace(64, str(not_a_repo), isolate=True) as ws:
            assert ws.path == str(not_a_repo)
            assert ws.isolate is False

    _run(_run_ws())


# --- Phase 2: merge lock ---


def test_merge_back_creates_lock_file(isolated_env):
    """merge_back should create a .merge.lock in forge_worktrees/."""
    repo, _, _ = isolated_env
    record = _run(create(70, str(repo)))
    wt = Path(record.path)
    (wt / "locktest.txt").write_text("lock test\n")
    _git(wt, "add", "locktest.txt")
    _git(wt, "commit", "-m", "lock test commit")

    _run(merge_back(70))

    lock_path = _merge_lock_path(str(repo))
    assert lock_path.exists()


def test_merge_back_serializes_concurrent_merges(isolated_env):
    """Two concurrent merge_back calls on the same project should both
    succeed — the lock serializes them so they don't interleave."""
    repo, _, _ = isolated_env
    _run(create(100, str(repo)))
    _run(create(101, str(repo)))

    wt100 = Path(_run(list_active())[1].path)  # older = index 1
    wt101 = Path(_run(list_active())[0].path)  # newer = index 0

    (wt100 / "file_a.txt").write_text("from task 100\n")
    _git(wt100, "add", "file_a.txt")
    _git(wt100, "commit", "-m", "task 100 commit")

    (wt101 / "file_b.txt").write_text("from task 101\n")
    _git(wt101, "add", "file_b.txt")
    _git(wt101, "commit", "-m", "task 101 commit")

    async def _merge_both():
        r1, r2 = await asyncio.gather(merge_back(100), merge_back(101))
        return r1, r2

    r1, r2 = _run(_merge_both())

    assert r1.ok, f"merge task 100 failed: {r1.message}"
    assert r2.ok, f"merge task 101 failed: {r2.message}"
    assert (repo / "file_a.txt").exists()
    assert (repo / "file_b.txt").exists()


def test_merge_lock_timeout(isolated_env, monkeypatch):
    """If the lock can't be acquired within the timeout, merge_back should
    return ok=False with a meaningful message."""
    import equipa.worktree_manager as wm

    repo, _, _ = isolated_env
    record = _run(create(80, str(repo)))
    wt = Path(record.path)
    (wt / "timeout.txt").write_text("timeout test\n")
    _git(wt, "add", "timeout.txt")
    _git(wt, "commit", "-m", "timeout commit")

    monkeypatch.setattr(wm, "MERGE_LOCK_TIMEOUT_SECONDS", 0.2)

    lock_path = _merge_lock_path(str(repo))
    from filelock import FileLock
    blocker = FileLock(lock_path)
    blocker.acquire()
    try:
        result = _run(merge_back(80))
        assert result.ok is False
        assert "lock" in result.message.lower()
    finally:
        blocker.release()


def test_merge_lock_path_creates_directory(tmp_path):
    """_merge_lock_path should create forge_worktrees/ if missing."""
    fake_project = tmp_path / "project"
    fake_project.mkdir()

    lock_path = _merge_lock_path(str(fake_project))

    assert lock_path.parent.exists()
    assert lock_path.name == ".merge.lock"


# --- Phase 3: cleanup ---


def test_list_stale_returns_failed_and_conflict(isolated_env):
    repo, _, project_id = isolated_env
    _run(create(200, str(repo)))
    _run(create(201, str(repo)))
    _run(create(202, str(repo)))

    _run(mark_failed(200))
    _run(mark_conflict(201, MergeResult(ok=False, message="conflict")))
    # 202 stays active

    stale = list_stale(project_id)
    stale_ids = {r.task_id for r in stale}
    assert 200 in stale_ids
    assert 201 in stale_ids
    assert 202 not in stale_ids


def test_list_stale_filters_by_project(isolated_env):
    repo, db_path, project_id = isolated_env

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO projects (name, codename, local_path) VALUES (?, ?, ?)",
        ("Other", "other", str(repo.parent / "other")),
    )
    other_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO worktrees (task_id, project_id, path, branch, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (999, other_id, str(repo.parent / "other" / "forge_worktrees" / "other-task-999"),
         "forge-task-999", STATUS_FAILED),
    )
    conn.commit()
    conn.close()

    _run(create(210, str(repo)))
    _run(mark_failed(210))

    all_stale = list_stale()
    assert any(r.task_id == 999 for r in all_stale)
    assert any(r.task_id == 210 for r in all_stale)

    filtered = list_stale(project_id)
    assert not any(r.task_id == 999 for r in filtered)
    assert any(r.task_id == 210 for r in filtered)


def test_cleanup_stale_worktree_removes_and_updates_db(isolated_env):
    repo, db_path, _ = isolated_env
    record = _run(create(220, str(repo)))
    _run(mark_failed(220))

    stale = list_stale()
    target = [r for r in stale if r.task_id == 220][0]

    success, msg = _run(cleanup_stale_worktree(target))

    assert success
    assert not Path(record.path).exists()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (220,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_ABANDONED


def test_cleanup_stale_worktree_stashes_uncommitted(isolated_env):
    repo, _, _ = isolated_env
    record = _run(create(230, str(repo)))
    wt = Path(record.path)

    (wt / "wip.py").write_text("print('work in progress')\n")
    _git(wt, "add", "wip.py")

    _run(mark_failed(230))

    stale = list_stale()
    target = [r for r in stale if r.task_id == 230][0]
    _run(cleanup_stale_worktree(target))

    stash_list = _git(repo, "stash", "list").stdout
    assert "equipa-early-term task-230" in stash_list


def test_cleanup_all_stale_dry_run(isolated_env):
    repo, db_path, _ = isolated_env
    record = _run(create(240, str(repo)))
    _run(mark_failed(240))

    results = _run(cleanup_all_stale(dry_run=True))

    assert len(results) >= 1
    assert all("DRY RUN" in msg for _, _, msg in results)
    assert Path(record.path).exists()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (240,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_FAILED


def test_cleanup_all_stale_handles_missing_directory(isolated_env):
    repo, db_path, _ = isolated_env
    record = _run(create(250, str(repo)))
    _run(mark_failed(250))

    shutil.rmtree(record.path)

    results = _run(cleanup_all_stale())
    target_results = [(r, ok, msg) for r, ok, msg in results if r.task_id == 250]
    assert len(target_results) == 1
    assert target_results[0][1] is True
    assert "disk" in target_results[0][2].lower() or "Already" in target_results[0][2]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM worktrees WHERE task_id = ?", (250,),
    ).fetchone()
    conn.close()
    assert row["status"] == STATUS_ABANDONED
