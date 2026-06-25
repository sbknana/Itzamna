#!/usr/bin/env python3
"""Schema-drift guard for TheForge (#2491 scope addition).

Background
----------
EQUIPA hit *three* confirmed schema-drift symptoms in one session:

* the cycle-3 security reviewer's ``describe_table`` reported an
  ``open_questions`` schema WITHOUT the ``priority`` column, producing a
  FALSE-POSITIVE HIGH that nearly blocked Initiative Phase 2 (item 7);
* ``forgesmith_impact.py`` resolved the DB to the wrong path (fixed 6aa4af8);
* ``forgesmith_litm.py`` queried ``agent_runs.output`` — a column that does
  not exist.

The common failure mode is code (or a reviewer) reading a schema that does
not match the *real* production schema, then making a wrong decision. This
test pins the contract: every column EQUIPA's SQL actually depends on must
exist in the canonical schema, reconstructed from the same sources that build
a production DB. If a future migration drops/renames one of these columns, or
a query references a column that was never added, CI fails here instead of at
runtime (or worse, as a false-positive security HIGH).

The canonical schema is ``schema.sql`` (base bootstrap) plus
``scripts/migrate_initiative_schema.py::apply_migration`` (the Phase 1/2
additive initiative columns). Both are the exact artifacts a real DB is built
from, so this test would have caught the ``priority`` false-HIGH.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "schema.sql"
INITIATIVE_MIGRATION = REPO_ROOT / "scripts" / "migrate_initiative_schema.py"


# Drift-sensitive (table -> columns) contract. These are columns EQUIPA SQL
# queries reference directly; each one, if missing, has caused or could cause
# a runtime failure or a false security finding. Keep this in sync when a
# query starts depending on a NEW column — that is the whole point: the diff
# that adds the dependency must also prove the column exists.
REQUIRED_COLUMNS: dict[str, list[str]] = {
    # Item 7 regression: the reviewer's describe_table claimed priority was
    # absent. It is present (TEXT DEFAULT 'medium'); file_open_question writes
    # it. A drift here re-opens the false-HIGH gate failure.
    "open_questions": ["id", "project_id", "question", "context", "priority"],
    # initiative_runner._read_task_cost / _read_task_gate_outcome.
    "agent_runs": ["id", "task_id", "project_id", "cost_usd", "outcome"],
    # initiative_runner.fetch_initiative / mark_paused / add_cost.
    "initiatives": [
        "id", "project_id", "name", "goal", "status", "total_cost",
        "started_at", "paused_at", "pause_reason", "created_at",
        "completed_at",
    ],
    # initiative_runner.fetch_initiative_tasks selects these explicitly.
    # ``role`` is now part of the canonical schema (added to schema.sql with the
    # tasks.role dispatch feature) AND back-filled onto legacy DBs by
    # db._ensure_additive_columns. fetch_initiative_tasks still selects it
    # CONDITIONALLY (NULL AS role when absent) so it stays safe on a pre-migration
    # DB — that resilience is pinned by test_tasks_role_legacy_db_resilient below.
    "tasks": [
        "id", "project_id", "title", "status", "blocked_by",
        "initiative_id", "role",
    ],
}


def _load_initiative_migration():
    """Import scripts/migrate_initiative_schema.py by path (not a package)."""
    spec = importlib.util.spec_from_file_location(
        "migrate_initiative_schema", INITIATIVE_MIGRATION
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise RuntimeError(f"cannot load {INITIATIVE_MIGRATION}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canonical_conn() -> sqlite3.Connection:
    """Build the canonical production schema in memory.

    schema.sql (base) + the initiative migration (Phase 1/2 columns) — the
    exact artifacts a real TheForge DB is constructed from.
    """
    assert SCHEMA_SQL.is_file(), f"missing {SCHEMA_SQL}"
    assert INITIATIVE_MIGRATION.is_file(), f"missing {INITIATIVE_MIGRATION}"

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    _load_initiative_migration().apply_migration(conn)
    yield conn
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_required_columns_exist(
    canonical_conn: sqlite3.Connection, table: str
) -> None:
    """Every drift-sensitive column EQUIPA queries must exist in the schema."""
    present = _columns(canonical_conn, table)
    assert present, f"table '{table}' missing from canonical schema"
    missing = [c for c in REQUIRED_COLUMNS[table] if c not in present]
    assert not missing, (
        f"schema drift: {table} is missing column(s) {missing}; "
        f"present columns = {sorted(present)}"
    )


def test_open_questions_priority_present(
    canonical_conn: sqlite3.Connection,
) -> None:
    """Direct regression for the #2489 cycle-3 false-positive HIGH (item 7).

    A reviewer's describe_table reported open_questions WITHOUT priority and
    produced a HIGH on the assumption the halt path would crash. The real
    schema HAS priority (default 'medium'). This pins that fact so the same
    false-HIGH cannot recur from a stale/wrong schema read.
    """
    info = {
        row[1]: row
        for row in canonical_conn.execute("PRAGMA table_info(open_questions)")
    }
    assert "priority" in info, (
        "open_questions.priority is absent from the canonical schema — the "
        "#2489 false-HIGH would be reproducible"
    )


def test_initiatives_phase2_columns_present(
    canonical_conn: sqlite3.Connection,
) -> None:
    """The Phase 2 additive columns must survive the migration."""
    present = _columns(canonical_conn, "initiatives")
    for col in ("total_cost", "started_at", "paused_at", "pause_reason"):
        assert col in present, f"initiatives.{col} missing after migration"


def test_tasks_role_is_canonical(
    canonical_conn: sqlite3.Connection,
) -> None:
    """``tasks.role`` is part of the canonical schema (tasks.role dispatch feature).

    It used to be intentionally absent (#2491); it was promoted to canonical when
    dispatch-by-stored-role landed (schema.sql + db._ensure_additive_columns
    back-fill for legacy DBs). fetch_initiative_tasks must still run cleanly on
    the canonical schema.
    """
    from equipa.initiative_runner import fetch_initiative_tasks

    assert "role" in _columns(canonical_conn, "tasks"), (
        "tasks.role missing from the canonical schema — the dispatch-by-role "
        "feature requires it (schema.sql)."
    )
    canonical_conn.row_factory = sqlite3.Row
    try:
        result = fetch_initiative_tasks(canonical_conn, initiative_id=-1)
    finally:
        canonical_conn.row_factory = None
    assert result == []


def test_tasks_role_legacy_db_resilient() -> None:
    """fetch_initiative_tasks must not crash on a pre-migration DB lacking role.

    Preserves the #2491 regression guard now that role is canonical: a legacy
    ``tasks`` table without a ``role`` column must still be queryable — the
    conditional ``NULL AS role`` branch in fetch_initiative_tasks handles it.
    """
    from equipa.initiative_runner import fetch_initiative_tasks

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, "
            "status TEXT, blocked_by TEXT, initiative_id INTEGER)"
        )
        conn.commit()
        assert "role" not in _columns(conn, "tasks")
        conn.row_factory = sqlite3.Row
        assert fetch_initiative_tasks(conn, initiative_id=-1) == []
    finally:
        conn.close()


def test_theforge_db_respects_env_var() -> None:
    """constants.THEFORGE_DB must honour the THEFORGE_DB env var (question c).

    The open question 'should THEFORGE_DB env var be respected consistently?'
    resolves to YES: equipa.constants is the single source of truth and reads
    the env var with a sane repo-relative default. Pinning it here prevents a
    regression to a hardcoded/relative path (the root cause of the stale-DB
    drift symptoms).
    """
    import importlib
    import os

    custom = "/tmp/forge-drift-test-custom.db"
    prev = os.environ.get("THEFORGE_DB")
    os.environ["THEFORGE_DB"] = custom
    try:
        import equipa.constants as constants

        importlib.reload(constants)
        assert str(constants.THEFORGE_DB) == custom
    finally:
        if prev is None:
            os.environ.pop("THEFORGE_DB", None)
        else:
            os.environ["THEFORGE_DB"] = prev
        import equipa.constants as constants

        importlib.reload(constants)
