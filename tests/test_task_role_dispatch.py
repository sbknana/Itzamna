"""Tests for tasks.role-driven dispatch (Design Q3 / Bug 4 follow-up).

Covers the two new pieces that let a task carry its own dispatch role so
autonomous/scan modes (and --task without --role) fan out to specialist/project
roles instead of always running the dev/test loop:

1. _ensure_additive_columns idempotently adds the tasks.role column to an
   existing DB (schema.sql's CREATE TABLE IF NOT EXISTS can't).
2. _is_audit_type_task treats report-writer (early_term_exempt) roles —
   including project-overlay ones — as audit-type, so the tester is skipped
   when there is no code diff.
"""

import sqlite3

from equipa.db import _ensure_additive_columns
from equipa.loops import _is_audit_type_task


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_ensure_additive_columns_adds_tasks_role_idempotently(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()

    assert "role" not in _cols(conn, "tasks")
    _ensure_additive_columns(conn)
    conn.commit()
    assert "role" in _cols(conn, "tasks")

    # Idempotent: a second run must not raise (no ADD COLUMN IF NOT EXISTS).
    _ensure_additive_columns(conn)
    conn.commit()
    assert "role" in _cols(conn, "tasks")
    conn.close()


def _write_role(project_dir, name, frontmatter):
    roles_dir = project_dir / ".equipa" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    (roles_dir / f"{name}.md").write_text(f"---\n{fm}\n---\nbody\n", encoding="utf-8")


def test_audit_type_includes_early_term_exempt_project_role(tmp_path):
    proj = tmp_path / "projA"
    _write_role(proj, "ip-analyst", {"early_term_exempt": "true"})
    _write_role(proj, "code-writer", {"early_term_exempt": "false"})

    # Report-writer (exempt) project role -> audit-type (tester gets skipped).
    assert _is_audit_type_task({"task_type": "feature"}, "ip-analyst", str(proj)) is True
    # Non-exempt project role -> keeps its tester cycle.
    assert _is_audit_type_task({"task_type": "feature"}, "code-writer", str(proj)) is False
    # Base developer -> not audit-type.
    assert _is_audit_type_task({"task_type": "feature"}, "developer", str(proj)) is False
    # Legacy behaviour preserved: reviewer role is audit-type even with no project_dir.
    assert _is_audit_type_task({"task_type": "feature"}, "security-reviewer") is True
    # Legacy behaviour preserved: audit task_type is audit-type.
    assert _is_audit_type_task({"task_type": "audit"}, "developer", str(proj)) is True


def test_ensure_additive_columns_adds_tasks_updated_at_and_backfills(tmp_path):
    """v13: the safety net must add tasks.updated_at (schema.sql's IF NOT
    EXISTS never does) and backfill it, or update_task_timestamp breaks
    every task UPDATE with 'no such column: updated_at'."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, "
        "created_at TEXT, completed_at TEXT);"
        "INSERT INTO tasks (id, title, created_at, completed_at) "
        "VALUES (1, 'open', '2026-08-01 00:00:00', NULL), "
        "(2, 'done', '2026-08-01 00:00:00', '2026-08-05 00:00:00');"
    )
    conn.commit()

    assert "updated_at" not in _cols(conn, "tasks")
    _ensure_additive_columns(conn)
    conn.commit()
    assert "updated_at" in _cols(conn, "tasks")
    rows = dict(conn.execute("SELECT id, updated_at FROM tasks"))
    assert rows[1] == "2026-08-01 00:00:00"     # created_at
    assert rows[2] == "2026-08-05 00:00:00"     # completed_at wins

    # Idempotent.
    _ensure_additive_columns(conn)
    conn.commit()
    assert [r[1] for r in conn.execute("PRAGMA table_info(tasks)")].count("updated_at") == 1
    conn.close()


def test_ensure_current_views_rebases_stale_on_movement(tmp_path):
    """v13: _ensure_current_views must redefine v_stale_tasks onto
    COALESCE(updated_at, created_at) even when the old (created_at) view
    already exists, since CREATE VIEW IF NOT EXISTS would leave it."""
    from equipa.db import _ensure_current_views

    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, codename TEXT);"
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER, "
        "title TEXT, status TEXT, created_at TEXT, updated_at TEXT);"
        "CREATE VIEW v_stale_tasks AS "
        "SELECT t.*, p.codename as project_name, "
        "julianday('now') - julianday(t.created_at) as days_stale "
        "FROM tasks t JOIN projects p ON t.project_id = p.id "
        "WHERE t.status='in_progress' "
        "AND julianday('now') - julianday(t.created_at) > 3;"
        "INSERT INTO projects VALUES (1, 'ticker');"
        "INSERT INTO tasks VALUES (1, 1, 'old but touched', 'in_progress', "
        "'2026-01-01 00:00:00', datetime('now'));"
    )
    conn.commit()

    # Old view: created long ago, so it is stale.
    assert {r[0] for r in conn.execute("SELECT id FROM v_stale_tasks")} == {1}
    _ensure_current_views(conn)
    conn.commit()
    # New view: touched just now, so it is not.
    assert {r[0] for r in conn.execute("SELECT id FROM v_stale_tasks")} == set()
    assert "COALESCE" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='v_stale_tasks'").fetchone()[0]
    conn.close()
