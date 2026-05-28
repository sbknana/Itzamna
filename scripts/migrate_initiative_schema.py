#!/usr/bin/env python3
"""Schema migration for EQUIPA initiative concept (Phase 1).

Adds two pieces of schema to TheForge:

1. ``initiatives`` table — first-class long-horizon work.
2. ``tasks.initiative_id`` column — nullable FK linking a task to an
   initiative. Tasks with ``initiative_id IS NULL`` continue to behave
   exactly as today (backward compatible).

The migration is IDEMPOTENT: it is safe to re-run. Existing rows and
data are never mutated; only missing schema is created.

Usage::

    python scripts/migrate_initiative_schema.py [path/to/theforge.db]

If no path is supplied, the script defaults to ``equipa.constants.THEFORGE_DB``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


# S3 hardening: CHECK constraints bound the two free-text fields at the
# DB layer so a caller that bypasses the CLI guard
# (equipa.cli._validate_initiative_input) cannot insert a multi-megabyte
# goal that would later be injected into every sibling-task system prompt.
# The cap values mirror the CLI constants INITIATIVE_NAME_MAX (200) and
# INITIATIVE_GOAL_MAX (8192); keep them in sync if either changes.
INITIATIVES_DDL = """
CREATE TABLE IF NOT EXISTS initiatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    CHECK (length(name) <= 200 AND length(goal) <= 8192)
);
"""

INITIATIVES_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_initiatives_project_status
    ON initiatives(project_id, status);
"""

TASKS_INITIATIVE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_tasks_initiative
    ON tasks(initiative_id) WHERE initiative_id IS NOT NULL;
"""


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``table.column`` already exists in the database."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def apply_migration(conn: sqlite3.Connection) -> dict[str, bool]:
    """Apply the initiative schema migration to ``conn``.

    Returns a small report describing what was created vs. already present.
    The function is idempotent and never raises if the schema is already
    up to date.
    """
    report = {
        "initiatives_table_created": False,
        "initiative_id_column_added": False,
    }

    if not _table_exists(conn, "tasks"):
        raise RuntimeError(
            "Cannot apply initiative migration: 'tasks' table missing. "
            "Run the base EQUIPA schema bootstrap first."
        )

    existed_initiatives = _table_exists(conn, "initiatives")
    conn.executescript(INITIATIVES_DDL)
    conn.executescript(INITIATIVES_INDEX_DDL)
    report["initiatives_table_created"] = not existed_initiatives

    if not _column_exists(conn, "tasks", "initiative_id"):
        # SQLite ALTER TABLE ADD COLUMN: nullable column with FK reference.
        # The FK is recorded in the schema but enforced only when
        # PRAGMA foreign_keys = ON; left to caller policy.
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN initiative_id INTEGER "
            "REFERENCES initiatives(id)"
        )
        report["initiative_id_column_added"] = True

    conn.executescript(TASKS_INITIATIVE_INDEX_DDL)
    conn.commit()
    return report


def describe_table(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return PRAGMA table_info rows for ``table`` (debug helper)."""
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        db_path = Path(argv[1])
    else:
        # Lazy import so this script also runs standalone in CI sandboxes
        # where the equipa package may not be importable.
        try:
            from equipa.constants import THEFORGE_DB
        except ImportError:
            print(
                "ERROR: no DB path supplied and equipa package not importable",
                file=sys.stderr,
            )
            return 2
        db_path = Path(THEFORGE_DB)

    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        report = apply_migration(conn)
    finally:
        conn.close()

    print(f"Initiative migration applied to {db_path}")
    print(f"  initiatives table created: {report['initiatives_table_created']}")
    print(
        "  tasks.initiative_id column added: "
        f"{report['initiative_id_column_added']}"
    )

    # Re-open in read mode to verify final shape (describe_table)
    conn = sqlite3.connect(str(db_path))
    try:
        print("\ntasks columns:")
        for row in describe_table(conn, "tasks"):
            print(f"  {row[1]} ({row[2]})")
        print("\ninitiatives columns:")
        for row in describe_table(conn, "initiatives"):
            print(f"  {row[1]} ({row[2]})")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
