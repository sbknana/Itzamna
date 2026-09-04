#!/usr/bin/env python3
"""Test suite for database migrations v11 -> v12 -> v13.

v12 stamps the ``worktrees`` table the worktree-isolation helpers already
create on demand. v13 adds the per-task movement column ``tasks.updated_at``
and re-bases ``v_stale_tasks`` on movement instead of age.

Tests:
- ``worktrees`` and its indexes exist after v12 (and survive a pre-existing
  copy, since the helpers may have created it first).
- ``tasks.updated_at`` exists after v13 and is backfilled from completed_at,
  else created_at.
- The task trigger stamps updated_at on an ordinary UPDATE and yields to an
  explicit updated_at in the same UPDATE.
- ``v_stale_tasks`` measures from updated_at: a recently-touched old task is
  not stale; an untouched old task is.
- Both migrations are idempotent.
- ``PRAGMA user_version`` ends at 13.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db_migrate import (
    get_db_version,
    migrate_v11_to_v12,
    migrate_v12_to_v13,
    set_db_version,
)


@pytest.fixture
def v11_db(tmp_path: Path):
    """A v11 database with the tables v12/v13 touch or reference."""
    db_path = tmp_path / "v11.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            codename TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium',
            blocked_by TEXT,
            due_date DATE,
            completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            role TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE VIEW v_stale_tasks AS
        SELECT t.*, p.codename as project_name,
               julianday('now') - julianday(t.created_at) as days_stale
        FROM tasks t JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'in_progress'
          AND julianday('now') - julianday(t.created_at) > 3;
        INSERT INTO projects (id, name, codename) VALUES (1, 'TICKER', 'ticker');
        INSERT INTO tasks (id, project_id, title, status, created_at)
            VALUES (100, 1, 'old and untouched', 'in_progress', '2026-08-01 00:00:00');
        INSERT INTO tasks (id, project_id, title, status, created_at, completed_at)
            VALUES (101, 1, 'finished', 'done', '2026-08-01 00:00:00', '2026-08-02 00:00:00');
    """)
    conn.commit()
    set_db_version(conn, 11)
    conn.close()
    return db_path


def _migrate(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_v11_to_v12(conn)
        set_db_version(conn, 12)
        migrate_v12_to_v13(conn)
        set_db_version(conn, 13)
    finally:
        conn.close()


def _names(conn, kind: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_v12_creates_worktrees(v11_db):
    _migrate(v11_db)
    conn = sqlite3.connect(str(v11_db))
    try:
        assert "worktrees" in _names(conn, "table")
        assert {"idx_worktrees_status", "idx_worktrees_task",
                "idx_worktrees_project"} <= _names(conn, "index")
    finally:
        conn.close()


def test_v13_adds_updated_at_and_backfills(v11_db):
    _migrate(v11_db)
    conn = sqlite3.connect(str(v11_db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "updated_at" in cols
        rows = dict(conn.execute("SELECT id, updated_at FROM tasks"))
        assert rows[100] == "2026-08-01 00:00:00"      # created_at
        assert rows[101] == "2026-08-02 00:00:00"      # completed_at wins
    finally:
        conn.close()


def test_task_trigger_stamps_and_yields(v11_db):
    _migrate(v11_db)
    conn = sqlite3.connect(str(v11_db))
    try:
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 100")
        conn.commit()
        stamped = conn.execute("SELECT updated_at FROM tasks WHERE id = 100").fetchone()[0]
        assert stamped != "2026-08-01 00:00:00"
        conn.execute("UPDATE tasks SET status = 'in_progress', updated_at = '2026-08-15 12:00:00' "
                     "WHERE id = 100")
        conn.commit()
        explicit = conn.execute("SELECT updated_at FROM tasks WHERE id = 100").fetchone()[0]
        assert explicit == "2026-08-15 12:00:00"
    finally:
        conn.close()


def test_stale_view_measures_movement(v11_db):
    _migrate(v11_db)
    conn = sqlite3.connect(str(v11_db))
    try:
        stale = {r[0] for r in conn.execute("SELECT id FROM v_stale_tasks")}
        assert stale == {100}                          # old, in progress, untouched
        conn.execute("UPDATE tasks SET description = 'touched today' WHERE id = 100")
        conn.commit()
        stale = {r[0] for r in conn.execute("SELECT id FROM v_stale_tasks")}
        assert stale == set()                          # moved: no longer stale
    finally:
        conn.close()


def test_migrations_idempotent_and_versioned(v11_db):
    _migrate(v11_db)
    _migrate(v11_db)
    conn = sqlite3.connect(str(v11_db))
    try:
        assert get_db_version(conn) == 13
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert cols.count("updated_at") == 1
    finally:
        conn.close()
