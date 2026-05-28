"""Regression tests for the Phase 1 v3 initiative migration.

S2486-01 HIGH fix coverage: ``CREATE TABLE IF NOT EXISTS`` is a no-op
when the table already exists, so any TheForge DB that ran the v1
migration silently lacks the v2 CHECK constraint on
(name, goal) length. The v3 migration must detect this and rebuild the
table — these tests prove it does.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.migrate_initiative_schema import apply_migration  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Simulated v1 DDL — initiatives table WITHOUT the v2 CHECK constraint.
# This mirrors what would have been deployed by the Phase 1 v1 migration.
V1_INITIATIVES_DDL = """
CREATE TABLE initiatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
"""

BASE_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    codename TEXT
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo'
);
"""


@pytest.fixture
def v1_db(tmp_path: Path) -> sqlite3.Connection:
    """A DB with the v1 schema shape: initiatives WITHOUT the CHECK."""
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(BASE_SCHEMA)
    conn.executescript(V1_INITIATIVES_DDL)
    conn.execute(
        "INSERT INTO projects (id, name, codename) "
        "VALUES (1, 'TestProject', 'tp')"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Faithfulness of the simulation: v1 schema lets oversized goals through.
# ---------------------------------------------------------------------------

def test_v1_schema_accepts_oversize_goal(v1_db):
    """Sanity-check: the SIMULATED v1 schema lacks the CHECK, so it
    accepts a > 8192 char goal. If this ever stops being true the
    rest of the test logic is comparing against the wrong baseline.
    """
    huge_goal = "x" * 9000
    v1_db.execute(
        "INSERT INTO initiatives (project_id, name, goal) "
        "VALUES (1, 'baseline', ?)",
        (huge_goal,),
    )
    v1_db.commit()
    row = v1_db.execute(
        "SELECT length(goal) FROM initiatives WHERE name = 'baseline'"
    ).fetchone()
    assert row[0] == 9000


# ---------------------------------------------------------------------------
# The headline regression: v3 migration adds the CHECK on existing DBs.
# ---------------------------------------------------------------------------

def test_v3_migration_rebuilds_v1_table_with_check(v1_db):
    """Apply v3 migration to v1 schema → CHECK constraint is enforced."""
    # Seed a normal-sized row that should survive the rebuild intact.
    v1_db.execute(
        "INSERT INTO initiatives (project_id, name, goal, status) "
        "VALUES (1, 'survive', 'normal goal text', 'active')"
    )
    v1_db.commit()

    report = apply_migration(v1_db)
    assert report["initiatives_check_added"] is True, (
        "Migration should have detected v1 schema and rebuilt the table"
    )

    # Post-migration, > 8192 char goal MUST raise IntegrityError.
    with pytest.raises(sqlite3.IntegrityError):
        v1_db.execute(
            "INSERT INTO initiatives (project_id, name, goal) "
            "VALUES (1, 'post-migration', ?)",
            ("y" * 8193,),
        )
        v1_db.commit()
    v1_db.rollback()

    # Pre-existing normal row survived the rebuild.
    row = v1_db.execute(
        "SELECT goal FROM initiatives WHERE name = 'survive'"
    ).fetchone()
    assert row is not None
    assert row[0] == "normal goal text"


def test_v3_migration_preserves_row_count(v1_db):
    """Row count and id ordering survive the rename → recreate → insert
    pipeline."""
    rows = [
        (1, "a", "first"),
        (1, "b", "second"),
        (1, "c", "third"),
    ]
    v1_db.executemany(
        "INSERT INTO initiatives (project_id, name, goal) VALUES (?, ?, ?)",
        rows,
    )
    v1_db.commit()
    pre_count = v1_db.execute("SELECT COUNT(*) FROM initiatives").fetchone()[0]
    assert pre_count == 3

    apply_migration(v1_db)

    post_count = v1_db.execute("SELECT COUNT(*) FROM initiatives").fetchone()[0]
    assert post_count == 3
    # ids preserved (INSERT ... SELECT carries the column over).
    ids = [r[0] for r in v1_db.execute(
        "SELECT id FROM initiatives ORDER BY id"
    ).fetchall()]
    assert ids == [1, 2, 3]


def test_v3_migration_truncates_oversized_pre_existing_rows(v1_db):
    """Pre-existing rows whose values exceed the v2 caps are TRUNCATED
    (not rejected), so the migration can complete on production DBs
    that ran v1 unbounded.
    """
    huge_name = "n" * 500       # > 200-char cap
    huge_goal = "g" * 10_000    # > 8192-char cap
    v1_db.execute(
        "INSERT INTO initiatives (project_id, name, goal) VALUES (1, ?, ?)",
        (huge_name, huge_goal),
    )
    v1_db.commit()

    apply_migration(v1_db)

    row = v1_db.execute(
        "SELECT length(name), length(goal) FROM initiatives"
    ).fetchone()
    assert row[0] == 200, f"name not truncated to 200 (got {row[0]})"
    assert row[1] == 8192, f"goal not truncated to 8192 (got {row[1]})"


# ---------------------------------------------------------------------------
# Idempotency: second + third runs are no-ops once CHECK is present.
# ---------------------------------------------------------------------------

def test_v3_migration_is_idempotent_on_already_migrated_db(v1_db):
    """First run rebuilds; subsequent runs detect the CHECK and skip rebuild."""
    first = apply_migration(v1_db)
    assert first["initiatives_check_added"] is True

    second = apply_migration(v1_db)
    assert second["initiatives_check_added"] is False, (
        "Second migration run should be a no-op (CHECK already present)"
    )

    third = apply_migration(v1_db)
    assert third["initiatives_check_added"] is False


def test_v3_migration_on_fresh_db_does_not_rebuild(tmp_path: Path):
    """On a brand-new DB the v3 migration takes the fast path: CREATE
    TABLE includes the CHECK from the start; no rebuild needed."""
    db_path = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(BASE_SCHEMA)
    conn.commit()

    report = apply_migration(conn)

    assert report["initiatives_table_created"] is True
    assert report["initiatives_check_added"] is False, (
        "Fresh-install path should NOT take the rebuild branch"
    )

    # CHECK is enforced from row zero.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO initiatives (project_id, name, goal) "
            "VALUES (1, 'ok', ?)",
            ("x" * 9000,),
        )
        conn.commit()
    conn.rollback()
