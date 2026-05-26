"""EQUIPA worktree manager: git-worktree-isolated agent dispatch.

Each isolated task gets:
  - Its own git worktree at <project_dir>/forge_worktrees/<codename>-task-<id>/
  - A dedicated branch forge-task-<id>
  - On success-flagged exit: fast-forward merge back to the project's HEAD
    branch, worktree destroyed, branch deleted.
  - On failure or unexpected exception: worktree retained for inspection.

Phase 1: primitives + single-task opt-in mode (--worktree).
Phase 2: per-project merge lock for cross-process serialization.
Phase 3: --cleanup-worktrees batch cleanup of failed/abandoned worktrees.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from filelock import FileLock, Timeout as FileLockTimeout

from equipa.db import db_conn
from equipa.git_ops import git_run_async

logger = logging.getLogger(__name__)


# --- Status constants ---
STATUS_ACTIVE = "active"
STATUS_MERGED = "merged"
STATUS_FAILED = "failed"
STATUS_CONFLICT = "conflict"
STATUS_ABANDONED = "abandoned"

MERGE_LOCK_TIMEOUT_SECONDS = 300


@dataclass
class WorktreeRecord:
    id: int
    task_id: int
    project_id: int
    path: str
    branch: str
    status: str
    created_at: str
    ended_at: Optional[str] = None


@dataclass
class MergeResult:
    ok: bool
    message: str = ""
    branch: str = ""


class WorktreeError(RuntimeError):
    """Raised when worktree operations fail in ways the caller must surface."""


# --- Helpers ---

def _merge_lock_path(project_dir: str) -> Path:
    """Per-project merge lock file. Lives in forge_worktrees/ (already gitignored)."""
    lock_dir = Path(project_dir) / "forge_worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / ".merge.lock"

async def _stash_uncommitted(
    worktree_path: str,
    task_id: int,
    branch: str,
) -> bool:
    """Stash uncommitted changes in a worktree before it is destroyed.

    Returns True if a stash was created, False otherwise.
    Stash message is tagged so it can be located later:
    ``equipa-early-term task-<id> branch-<branch>``.

    Silent on failure — cleanup must continue even if the stash cannot be saved.
    """
    if not Path(worktree_path).exists():
        return False
    try:
        status = await git_run_async(
            ["status", "--porcelain"], cwd=worktree_path,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return False
        stash_msg = f"equipa-early-term task-{task_id} branch-{branch}"
        result = await git_run_async(
            ["stash", "push", "-u", "-m", stash_msg], cwd=worktree_path,
        )
        if result.returncode == 0 and "No local changes" not in result.stdout:
            logger.info(
                "[worktree] Task #%s: stashed uncommitted work on '%s'",
                task_id, branch,
            )
            return True
    except Exception as e:
        logger.warning(
            "[worktree] Could not stash uncommitted work for task #%s: %s",
            task_id, e,
        )
    return False

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_project(project_dir: str) -> tuple[int, str]:
    """Look up project_id and codename from project_dir.

    Returns (project_id, codename). Raises WorktreeError if no match.
    Does a normalized-path comparison so Windows path variants resolve.
    """
    target = Path(project_dir).resolve()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, codename, local_path FROM projects "
            "WHERE local_path IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                if Path(r["local_path"]).resolve() == target:
                    return r["id"], (r["codename"] or "project")
            except (OSError, ValueError):
                continue
    raise WorktreeError(
        f"No project in TheForge has local_path matching {target}. "
        "Cannot create worktree without a project mapping."
    )


def _ensure_gitignored(project_dir: str) -> None:
    """Ensure forge_worktrees/ is listed in the project's .gitignore.

    Append-only and idempotent. Does not commit the change — operator can
    commit it whenever convenient. The agent will see this as an uncommitted
    diff, which is intentional and harmless.
    """
    gitignore = Path(project_dir) / ".gitignore"
    line = "forge_worktrees/"
    if not gitignore.exists():
        gitignore.write_text(line + "\n", encoding="utf-8")
        return
    contents = gitignore.read_text(encoding="utf-8")
    for entry in contents.splitlines():
        stripped = entry.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.rstrip("/").lstrip("/") == "forge_worktrees":
            return
    if not contents.endswith("\n"):
        contents += "\n"
    contents += line + "\n"
    gitignore.write_text(contents, encoding="utf-8")


def _copy_hooks_and_markers(project_dir: str, worktree_path: str) -> None:
    """Copy git hooks and plugin boundary markers into a fresh worktree.

    Worktrees do NOT inherit .git/hooks from the parent repo — they share
    the parent's .git database but have their own per-worktree hooks dir
    that is empty by default. Pre-commit hooks (plugin boundary checks,
    linters, etc.) won't fire in worktrees unless we explicitly copy them.

    Ported from dispatch.py._copy_hooks_to_worktree (Phase 1 convergence,
    2026-05-23). Also copies .plugin-boundary-markers if present at the
    project root, since the pre-commit hook consumes it.
    """
    main_hooks = Path(project_dir) / ".git" / "hooks"
    wt_git = Path(worktree_path) / ".git"

    if main_hooks.is_dir() and wt_git.is_file():
        gitdir_line = wt_git.read_text(encoding="utf-8").strip()
        gitdir = gitdir_line.removeprefix("gitdir: ").strip()
        wt_hooks = Path(gitdir) / "hooks"
        wt_hooks.mkdir(parents=True, exist_ok=True)
        for hook in main_hooks.iterdir():
            if hook.is_file() and not hook.name.endswith(".sample"):
                dest = wt_hooks / hook.name
                try:
                    shutil.copy2(str(hook), str(dest))
                except OSError as e:
                    logger.warning(
                        "[worktree] failed to copy hook %s -> %s: %s",
                        hook, dest, e,
                    )

    markers_src = Path(project_dir) / ".plugin-boundary-markers"
    markers_dst = Path(worktree_path) / ".plugin-boundary-markers"
    if markers_src.exists() and not markers_dst.exists():
        try:
            shutil.copy2(str(markers_src), str(markers_dst))
        except OSError as e:
            logger.warning(
                "[worktree] failed to copy plugin-boundary-markers: %s", e,
            )


# --- Core API ---

async def create(task_id: int, project_dir: str) -> WorktreeRecord:
    """Create a git worktree for the given task.

    Raises WorktreeError if the worktree directory or branch already exists,
    or if git worktree add fails.
    """
    project_id, codename = _resolve_project(project_dir)

    project_root = Path(project_dir)
    worktrees_root = project_root / "forge_worktrees"
    worktree_path = worktrees_root / f"{codename}-task-{task_id}"
    branch = f"forge-task-{task_id}"

    if worktree_path.exists():
        raise WorktreeError(
            f"Worktree path already exists: {worktree_path}. "
            f"A prior dispatch on task #{task_id} likely failed and the worktree was retained. "
            f"Inspect, then remove manually or via --cleanup-worktrees (Phase 3)."
        )

    _ensure_gitignored(project_dir)
    worktrees_root.mkdir(parents=True, exist_ok=True)

    # Try to create the worktree on a fresh branch from current HEAD. If the
    # branch already exists (orphaned from a prior failed run that didn't
    # clean up), force-delete it and retry once. The path-exists case is
    # handled above; this is purely a branch-leftover recovery, ported from
    # dispatch.py._create_isolation_worktrees.
    r = await git_run_async(
        ["worktree", "add", str(worktree_path), "-b", branch, "HEAD"],
        cwd=project_dir,
    )
    if r.returncode != 0:
        del_r = await git_run_async(
            ["branch", "-D", branch], cwd=project_dir,
        )
        if del_r.returncode != 0 and "not found" not in del_r.stderr.lower():
            raise WorktreeError(
                f"git worktree add failed and orphan-branch cleanup also failed:\n"
                f"  worktree add: {r.stderr.strip() or r.stdout.strip()}\n"
                f"  branch -D {branch}: {del_r.stderr.strip()}"
            )
        r = await git_run_async(
            ["worktree", "add", str(worktree_path), "-b", branch, "HEAD"],
            cwd=project_dir,
        )
        if r.returncode != 0:
            raise WorktreeError(
                f"git worktree add failed after orphan-branch cleanup: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )

    _copy_hooks_and_markers(project_dir, str(worktree_path))

    with db_conn(write=True) as conn:
        cursor = conn.execute(
            """INSERT INTO worktrees (task_id, project_id, path, branch, status)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, project_id, str(worktree_path), branch, STATUS_ACTIVE),
        )
        wt_id = cursor.lastrowid
        row = conn.execute(
            "SELECT id, task_id, project_id, path, branch, status, created_at, ended_at "
            "FROM worktrees WHERE id = ?",
            (wt_id,),
        ).fetchone()

    logger.info(
        "[worktree] Created task=%s at %s on branch %s",
        task_id, worktree_path, branch,
    )
    return WorktreeRecord(
        id=row["id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        path=row["path"],
        branch=row["branch"],
        status=row["status"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
    )


async def destroy(task_id: int, keep_branch: bool = False) -> None:
    """Remove the worktree (and optionally the branch) for the given task.

    Looks up the most recent worktree for the task. Safe to call regardless
    of current status; updates DB row to 'abandoned' on completion.
    Logs but does not raise if git commands fail (best-effort cleanup).
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, path, branch FROM worktrees "
            "WHERE task_id = ? AND status IN (?, ?, ?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (task_id, STATUS_ACTIVE, STATUS_MERGED, STATUS_FAILED, STATUS_CONFLICT),
        ).fetchone()
        if row is None:
            logger.warning("[worktree] destroy: no record for task=%s", task_id)
            return
        worktree_path = row["path"]
        branch = row["branch"]

    project_dir = str(Path(worktree_path).parent.parent)

    await _stash_uncommitted(worktree_path, task_id, branch)

    r = await git_run_async(
        ["worktree", "remove", "--force", worktree_path],
        cwd=project_dir,
    )
    if r.returncode != 0:
        logger.warning(
            "[worktree] git worktree remove failed for %s: %s",
            worktree_path, r.stderr.strip(),
        )

    if not keep_branch:
        r = await git_run_async(["branch", "-D", branch], cwd=project_dir)
        if r.returncode != 0:
            logger.warning(
                "[worktree] branch delete failed for %s: %s",
                branch, r.stderr.strip(),
            )

    with db_conn(write=True) as conn:
        conn.execute(
            "UPDATE worktrees SET status = ?, ended_at = ? "
            "WHERE task_id = ? AND status IN (?, ?, ?)",
            (STATUS_ABANDONED, _utcnow_iso(),
             task_id, STATUS_ACTIVE, STATUS_FAILED, STATUS_CONFLICT),
        )

    logger.info("[worktree] Destroyed task=%s", task_id)


async def merge_back(task_id: int) -> MergeResult:
    """Merge the task's worktree branch back into the project's main branch.

    Acquires a per-project file lock so concurrent merges (from parallel
    dispatch or multiple CLI invocations) are serialized.

    Strategy:
      1. Determine the project's current branch; fall back to main/master if
         the project is on a detached HEAD.
      2. Check the worktree branch is actually ahead of the project's HEAD.
         If no commits ahead, skip the merge entirely and report it.
      3. Stash any uncommitted changes in the project dir (preserved across
         the merge, restored in the finally block).
      4. ``git merge --no-edit <branch>`` — fast-forwards when possible,
         creates a merge commit when not. Returns ok if HEAD advances.
      5. If the merge fails (conflict), abort, try rebase-then-merge as a
         recovery path, fall back to ok=False with the branch preserved.
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT path, branch FROM worktrees WHERE task_id = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, STATUS_ACTIVE),
        ).fetchone()
        if row is None:
            return MergeResult(
                ok=False,
                message=f"No active worktree for task={task_id}",
            )
        worktree_path = row["path"]
        branch = row["branch"]

    project_dir = str(Path(worktree_path).parent.parent)

    # Non-blocking acquire with async polling — acquire and release both run
    # in the event loop thread (Windows msvcrt.locking requires same-thread
    # lock/unlock on the same fd).
    lock = FileLock(_merge_lock_path(project_dir), timeout=0)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + MERGE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.acquire()
            break
        except FileLockTimeout:
            if loop.time() >= deadline:
                return MergeResult(
                    ok=False,
                    message=(
                        f"Could not acquire merge lock for {project_dir} "
                        f"within {MERGE_LOCK_TIMEOUT_SECONDS}s"
                    ),
                    branch=branch,
                )
            await asyncio.sleep(0.1)

    try:
        return await _merge_back_locked(task_id, project_dir, branch)
    finally:
        lock.release()


async def _merge_back_locked(
    task_id: int, project_dir: str, branch: str,
) -> MergeResult:
    """Inner merge logic, called with the per-project lock held."""
    # Step 1: resolve project's current branch, falling back to main/master.
    current = await git_run_async(["branch", "--show-current"], cwd=project_dir)
    main_branch = current.stdout.strip()
    if not main_branch:
        for candidate in ("main", "master"):
            check = await git_run_async(
                ["show-ref", "--verify", f"refs/heads/{candidate}"],
                cwd=project_dir,
            )
            if check.returncode == 0:
                co = await git_run_async(
                    ["checkout", candidate], cwd=project_dir,
                )
                if co.returncode == 0:
                    main_branch = candidate
                    break
        if not main_branch:
            return MergeResult(
                ok=False,
                message="project HEAD is detached and no main/master ref exists",
                branch=branch,
            )

    # Step 2: verify there are commits to merge.
    ahead = await git_run_async(
        ["log", "--oneline", f"HEAD..{branch}"], cwd=project_dir,
    )
    if ahead.returncode != 0:
        return MergeResult(
            ok=False,
            message=f"could not check commits ahead: {ahead.stderr.strip()}",
            branch=branch,
        )
    if not ahead.stdout.strip():
        # Not a failure — the agent simply did not commit anything.
        with db_conn(write=True) as conn:
            conn.execute(
                "UPDATE worktrees SET status = ?, ended_at = ? "
                "WHERE task_id = ? AND status = ?",
                (STATUS_MERGED, _utcnow_iso(), task_id, STATUS_ACTIVE),
            )
        logger.info(
            "[worktree] Merged task=%s: branch %s had no commits ahead of %s",
            task_id, branch, main_branch,
        )
        return MergeResult(
            ok=True,
            message=f"no commits ahead — nothing to merge",
            branch=branch,
        )

    # Step 3: stash any uncommitted work in the project dir.
    stash_result = await git_run_async(["stash"], cwd=project_dir)
    had_stash = "Saved working directory" in (stash_result.stdout or "")

    pre_head_r = await git_run_async(["rev-parse", "HEAD"], cwd=project_dir)
    pre_head = pre_head_r.stdout.strip() if pre_head_r.returncode == 0 else ""

    try:
        # Step 4: attempt the merge.
        merge_r = await git_run_async(
            ["merge", "--no-edit", branch], cwd=project_dir,
        )
        post_head_r = await git_run_async(["rev-parse", "HEAD"], cwd=project_dir)
        post_head = post_head_r.stdout.strip() if post_head_r.returncode == 0 else ""

        if merge_r.returncode == 0 and post_head and post_head != pre_head:
            with db_conn(write=True) as conn:
                conn.execute(
                    "UPDATE worktrees SET status = ?, ended_at = ? "
                    "WHERE task_id = ? AND status = ?",
                    (STATUS_MERGED, _utcnow_iso(), task_id, STATUS_ACTIVE),
                )
            logger.info(
                "[worktree] Merged task=%s branch=%s into %s (%s -> %s)",
                task_id, branch, main_branch, pre_head[:8], post_head[:8],
            )
            return MergeResult(ok=True, message="merged", branch=branch)

        if merge_r.returncode == 0:
            # Merge reported success but HEAD didn't move — anomalous.
            return MergeResult(
                ok=False,
                message=f"merge returned 0 but HEAD unchanged at {pre_head[:8]}",
                branch=branch,
            )

        # Step 5: conflict recovery — abort, rebase, retry merge.
        await git_run_async(["merge", "--abort"], cwd=project_dir)
        rebase_r = await git_run_async(
            ["rebase", "HEAD", branch], cwd=project_dir,
        )
        if rebase_r.returncode != 0:
            await git_run_async(["rebase", "--abort"], cwd=project_dir)
            return MergeResult(
                ok=False,
                message=(
                    f"merge conflict and rebase recovery failed; "
                    f"branch {branch} preserved. "
                    f"merge stderr: {(merge_r.stderr or '').strip()[:200]}"
                ),
                branch=branch,
            )

        merge2_r = await git_run_async(
            ["merge", "--no-edit", branch], cwd=project_dir,
        )
        post2_r = await git_run_async(["rev-parse", "HEAD"], cwd=project_dir)
        post2_head = post2_r.stdout.strip() if post2_r.returncode == 0 else ""

        if merge2_r.returncode == 0 and post2_head and post2_head != pre_head:
            with db_conn(write=True) as conn:
                conn.execute(
                    "UPDATE worktrees SET status = ?, ended_at = ? "
                    "WHERE task_id = ? AND status = ?",
                    (STATUS_MERGED, _utcnow_iso(), task_id, STATUS_ACTIVE),
                )
            logger.info(
                "[worktree] Merged task=%s branch=%s (after rebase recovery)",
                task_id, branch,
            )
            return MergeResult(
                ok=True,
                message="merged after rebase recovery",
                branch=branch,
            )

        await git_run_async(["merge", "--abort"], cwd=project_dir)
        return MergeResult(
            ok=False,
            message=(
                f"merge failed even after rebase; branch {branch} preserved"
            ),
            branch=branch,
        )
    finally:
        if had_stash:
            pop_r = await git_run_async(["stash", "pop"], cwd=project_dir)
            if pop_r.returncode != 0:
                logger.warning(
                    "[worktree] stash pop failed after merge of task=%s: %s",
                    task_id, pop_r.stderr.strip(),
                )


async def mark_failed(task_id: int) -> None:
    """Mark the active worktree for task_id as failed. Worktree retained."""
    with db_conn(write=True) as conn:
        conn.execute(
            "UPDATE worktrees SET status = ?, ended_at = ? "
            "WHERE task_id = ? AND status = ?",
            (STATUS_FAILED, _utcnow_iso(), task_id, STATUS_ACTIVE),
        )
    logger.info(
        "[worktree] Marked task=%s as failed (worktree retained)", task_id,
    )


async def mark_conflict(task_id: int, result: MergeResult) -> None:
    """Mark the active worktree for task_id as conflict."""
    with db_conn(write=True) as conn:
        conn.execute(
            "UPDATE worktrees SET status = ?, ended_at = ? "
            "WHERE task_id = ? AND status = ?",
            (STATUS_CONFLICT, _utcnow_iso(), task_id, STATUS_ACTIVE),
        )
    logger.warning(
        "[worktree] Conflict on task=%s: %s", task_id, result.message,
    )


async def list_active(project_id: Optional[int] = None) -> list[WorktreeRecord]:
    """List worktrees in 'active' status. Filter by project_id if given."""
    sql = (
        "SELECT id, task_id, project_id, path, branch, status, created_at, ended_at "
        "FROM worktrees WHERE status = ?"
    )
    params: list = [STATUS_ACTIVE]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY created_at DESC"

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        WorktreeRecord(
            id=r["id"], task_id=r["task_id"], project_id=r["project_id"],
            path=r["path"], branch=r["branch"], status=r["status"],
            created_at=r["created_at"], ended_at=r["ended_at"],
        )
        for r in rows
    ]


# --- Cleanup (Phase 3) ---

def list_stale(project_id: Optional[int] = None) -> list[WorktreeRecord]:
    """List worktrees in terminal statuses (failed/conflict/abandoned)."""
    sql = (
        "SELECT id, task_id, project_id, path, branch, status, created_at, ended_at "
        "FROM worktrees WHERE status IN (?, ?, ?)"
    )
    params: list = [STATUS_FAILED, STATUS_CONFLICT, STATUS_ABANDONED]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY created_at ASC"

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        WorktreeRecord(
            id=r["id"], task_id=r["task_id"], project_id=r["project_id"],
            path=r["path"], branch=r["branch"], status=r["status"],
            created_at=r["created_at"], ended_at=r["ended_at"],
        )
        for r in rows
    ]


async def cleanup_stale_worktree(record: WorktreeRecord) -> tuple[bool, str]:
    """Clean up a single stale worktree: stash, remove directory, delete branch.

    Returns (success, message).
    """
    project_dir = str(Path(record.path).parent.parent)

    if Path(record.path).exists():
        await _stash_uncommitted(record.path, record.task_id, record.branch)

        r = await git_run_async(
            ["worktree", "remove", "--force", record.path],
            cwd=project_dir,
        )
        if r.returncode != 0:
            try:
                shutil.rmtree(record.path, ignore_errors=True)
                await git_run_async(["worktree", "prune"], cwd=project_dir)
            except Exception as e:
                return False, f"Could not remove worktree directory: {e}"

    r = await git_run_async(["branch", "-D", record.branch], cwd=project_dir)
    if r.returncode != 0 and "not found" not in r.stderr.lower():
        logger.warning(
            "[worktree] cleanup: branch delete failed for %s: %s",
            record.branch, r.stderr.strip(),
        )

    if record.status != STATUS_ABANDONED:
        with db_conn(write=True) as conn:
            conn.execute(
                "UPDATE worktrees SET status = ?, ended_at = ? WHERE id = ?",
                (STATUS_ABANDONED, _utcnow_iso(), record.id),
            )

    return True, (
        f"Cleaned up task #{record.task_id} ({record.status}) "
        f"at {Path(record.path).name}"
    )


async def cleanup_all_stale(
    project_id: Optional[int] = None,
    dry_run: bool = False,
) -> list[tuple[WorktreeRecord, bool, str]]:
    """Batch cleanup of all stale worktrees. Returns (record, success, message) tuples."""
    stale = list_stale(project_id)
    results: list[tuple[WorktreeRecord, bool, str]] = []

    for record in stale:
        disk_exists = Path(record.path).exists()
        if dry_run:
            status_label = "EXISTS" if disk_exists else "MISSING"
            results.append((
                record, True,
                f"[DRY RUN] Would clean up task #{record.task_id} "
                f"({record.status}, disk={status_label}) branch={record.branch}",
            ))
        elif not disk_exists:
            if record.status != STATUS_ABANDONED:
                with db_conn(write=True) as conn:
                    conn.execute(
                        "UPDATE worktrees SET status = ?, ended_at = ? WHERE id = ?",
                        (STATUS_ABANDONED, _utcnow_iso(), record.id),
                    )
            results.append((record, True, "Already removed from disk; DB updated"))
        else:
            success, msg = await cleanup_stale_worktree(record)
            results.append((record, success, msg))

    return results


# --- Context manager ---

class TaskWorkspace:
    """Async context manager that yields an effective working directory.

    isolate=False: yields self with path=project_dir. set_success is a no-op.
    isolate=True : creates a git worktree on enter, yields self with path set
                   to the worktree path. On exit:
                     - If an exception escaped the block: mark_failed, re-raise.
                     - If set_success(True) was called: merge_back, then destroy.
                     - Otherwise: mark_failed (worktree retained on disk).

    Example:
        async with TaskWorkspace(task_id, project_dir, isolate=True) as ws:
            outcome = await dispatch(ws.path, ...)
            ws.set_success(outcome in success_outcomes)

    Falls back to non-isolated mode (with a warning) if project_dir is not a
    git repo, so callers can pass isolate=True unconditionally.
    """

    def __init__(self, task_id: int, project_dir: str, isolate: bool):
        self.task_id = task_id
        self.project_dir = project_dir
        self.isolate = isolate
        self.path: str = project_dir
        self._success = False
        self._record: Optional[WorktreeRecord] = None

    async def __aenter__(self) -> "TaskWorkspace":
        if not self.isolate:
            return self

        if not (Path(self.project_dir) / ".git").exists():
            logger.warning(
                "[worktree] %s is not a git repo; falling back to non-isolated mode",
                self.project_dir,
            )
            self.isolate = False
            return self

        self._record = await create(self.task_id, self.project_dir)
        self.path = self._record.path
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if not self.isolate:
            return False

        if exc_type is not None:
            try:
                await mark_failed(self.task_id)
            except Exception:
                logger.exception(
                    "[worktree] mark_failed during exception unwind also failed",
                )
            return False

        if self._success:
            result = await merge_back(self.task_id)
            if result.ok:
                await destroy(self.task_id)
            else:
                await mark_conflict(self.task_id, result)
        else:
            await mark_failed(self.task_id)
        return False

    def set_success(self, ok: bool = True) -> None:
        self._success = ok


@asynccontextmanager
async def task_workspace(
    task_id: int,
    project_dir: str,
    isolate: bool,
) -> AsyncIterator[TaskWorkspace]:
    """Functional alias for TaskWorkspace, for callers preferring an asynccontextmanager."""
    ws = TaskWorkspace(task_id, project_dir, isolate)
    async with ws:
        yield ws
