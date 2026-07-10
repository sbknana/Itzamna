#!/usr/bin/env python3
"""Tests for PRAGMA foreign_keys enforcement in equipa.db connections.

SR-2700 S1: db.py's ``get_db_connection`` diverged from
``heartbeat.py:_connect`` — the latter sets BOTH ``foreign_keys = ON`` and
``busy_timeout = 5000``, while db.py only set the timeout. schema.sql declares
29 FOREIGN KEY relationships that were silently unenforced on every
db.py-routed write. These tests pin the fix so it cannot regress:

  1. Every handle db.py hands out (read/write, plain and context-managed)
     reports ``PRAGMA foreign_keys == 1``.
  2. Both setup PRAGMAs (foreign_keys, busy_timeout) are applied.
  3. An INSERT with a dangling foreign key into a child table raises
     ``sqlite3.IntegrityError`` — proving enforcement is actually live.

Enforcement is connection-level and affects NEW writes only; existing rows
are untouched and no schema change is involved. Tests run entirely against a
tmp_path DB — the real theforge.db is never opened.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3

import pytest

from equipa import db as equipa_db


@pytest.fixture
def temp_forge_db(tmp_path, monkeypatch):
    """Create a tmp_path TheForge DB with the canonical schema applied.

    Points ``equipa.db.THEFORGE_DB`` at the temp file so ``get_db_connection``
    and ``db_conn`` operate on it instead of the production database. Returns
    the ``Path`` to the temp DB.
    """
    db_path = tmp_path / "theforge_test.db"

    schema_sql = equipa_db._make_schema_idempotent(
        equipa_db.SCHEMA_SQL_PATH.read_text()
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)
    return db_path


def _foreign_keys_flag(conn: sqlite3.Connection) -> int:
    """Return the current per-connection PRAGMA foreign_keys value (0/1)."""
    return conn.execute("PRAGMA foreign_keys").fetchone()[0]


def test_get_db_connection_write_enables_foreign_keys(temp_forge_db):
    """A write handle from get_db_connection reports foreign_keys == 1."""
    conn = equipa_db.get_db_connection(write=True)
    try:
        assert _foreign_keys_flag(conn) == 1
    finally:
        conn.close()


def test_get_db_connection_read_enables_foreign_keys(temp_forge_db):
    """A read-only handle also reports foreign_keys == 1 (connection-level)."""
    conn = equipa_db.get_db_connection(write=False)
    try:
        assert _foreign_keys_flag(conn) == 1
    finally:
        conn.close()


def test_get_db_connection_still_sets_busy_timeout(temp_forge_db):
    """Both setup PRAGMAs are applied — foreign_keys did not displace timeout.

    Guards the 'connection now issues TWO setup PRAGMAs' note: over-strict
    mocks that counted a single execute() call would silently miss this.
    """
    conn = equipa_db.get_db_connection(write=True)
    try:
        assert _foreign_keys_flag(conn) == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_db_conn_write_enables_foreign_keys(temp_forge_db):
    """The context-managed write handle reports foreign_keys == 1."""
    with equipa_db.db_conn(write=True) as conn:
        assert _foreign_keys_flag(conn) == 1


def test_db_conn_read_enables_foreign_keys(temp_forge_db):
    """The context-managed read handle reports foreign_keys == 1."""
    with equipa_db.db_conn(write=False) as conn:
        assert _foreign_keys_flag(conn) == 1


def test_dangling_foreign_key_insert_raises_integrity_error(temp_forge_db):
    """Inserting a child row that references a missing parent is rejected.

    tasks.project_id has FOREIGN KEY (project_id) REFERENCES projects(id).
    With enforcement on, a task pointing at a non-existent project must raise
    sqlite3.IntegrityError instead of silently persisting a dangling row.
    """
    with pytest.raises(sqlite3.IntegrityError):
        with equipa_db.db_conn(write=True) as conn:
            conn.execute(
                "INSERT INTO tasks (project_id, title) VALUES (?, ?)",
                (999_999, "orphan task with no parent project"),
            )


def test_valid_foreign_key_insert_succeeds(temp_forge_db):
    """A child row that references an existing parent still inserts cleanly.

    Confirms enforcement does not reject legitimate writes: create the parent
    project first, then a task referencing it succeeds.
    """
    with equipa_db.db_conn(write=True) as conn:
        cur = conn.execute(
            "INSERT INTO projects (name) VALUES (?)", ("temp-project",)
        )
        project_id = cur.lastrowid
        conn.execute(
            "INSERT INTO tasks (project_id, title) VALUES (?, ?)",
            (project_id, "task with valid parent"),
        )

    with equipa_db.db_conn(write=False) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
    assert count == 1
