#!/usr/bin/env python3
"""Guards for the personal-PM schema split (task 2707).

The public orchestrator product must NOT ship the owner's personal-PM data
model. Those 8 tables (+ 2 views) were moved from ``schema.sql`` into
``schema_personal.sql``, applied only when ``features.personal_pm_tables`` is
enabled (default TRUE, so the owner's prod install keeps them).

These tests pin the two non-negotiable safety properties:

  * ADDITIVE-ONLY — neither schema file contains a DROP/ALTER/RENAME/DELETE, so
    pulling this code can never rewrite or destroy data in an existing DB.
  * FRESH-INSTALL SEMANTICS — a fresh core install always gets the core tables;
    the personal tables appear only when the flag opts in; re-applying either
    file is an idempotent no-op that leaves existing data untouched.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from equipa import db as equipa_db
from equipa.config import DEFAULT_FEATURE_FLAGS, is_feature_enabled

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "schema.sql"
SCHEMA_PERSONAL_SQL = REPO_ROOT / "schema_personal.sql"

# The 8 tables the task moves out of the public product schema.
PERSONAL_TABLES = [
    "competitors",
    "content_tickler",
    "posting_schedule",
    "product_opportunities",
    "social_media_posts",
    "writing_style",
    "reminders",
    "voice_messages",
]

# Views that read exclusively from the personal tables and move with them.
PERSONAL_VIEWS = ["v_upcoming_reminders", "v_content_alerts"]

# A few unmistakably-core tables that must stay in schema.sql.
CORE_TABLES = ["projects", "tasks", "decisions", "agent_runs", "open_questions"]

# Statements that would mutate/destroy an existing DB. The whole point of the
# task is that the changeset contains none of these. Note the intentional
# precision: ``ON DELETE CASCADE`` (a referential action, not a DELETE
# statement) and a trigger's ``UPDATE ... SET`` body are legitimate additive
# schema features, so they must NOT trip this guard — trigger bodies are
# stripped before scanning and ``DELETE FROM`` (statement) is matched rather
# than the bare ``DELETE`` keyword.
_DESTRUCTIVE_RE = re.compile(
    r"\b(?:DROP\s+(?:TABLE|VIEW|INDEX|TRIGGER)"
    r"|ALTER\s+TABLE"
    r"|RENAME\s+(?:TO|COLUMN)"
    r"|DELETE\s+FROM"
    r"|UPDATE\s+\w+\s+SET"
    r"|TRUNCATE)\b",
    re.IGNORECASE,
)


def _strip_trigger_bodies(sql: str) -> str:
    """Remove CREATE TRIGGER ... END; blocks, whose bodies legitimately hold DML."""
    return re.sub(
        r"CREATE\s+TRIGGER.*?END\s*;", "", sql, flags=re.IGNORECASE | re.DOTALL
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _view_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    }


# --- Additive-only guard -------------------------------------------------


@pytest.mark.parametrize("schema_path", [SCHEMA_SQL, SCHEMA_PERSONAL_SQL])
def test_schema_files_are_additive_only(schema_path: Path) -> None:
    """Neither schema file may contain any destructive DDL/DML."""
    assert schema_path.exists(), f"{schema_path} missing"
    scannable = _strip_trigger_bodies(schema_path.read_text())
    offenders = _DESTRUCTIVE_RE.findall(scannable)
    assert not offenders, (
        f"{schema_path.name} contains non-additive statements: {offenders}"
    )


def test_personal_tables_only_in_personal_file() -> None:
    """Each moved table/view is defined in schema_personal.sql, not schema.sql."""
    core_text = SCHEMA_SQL.read_text()
    personal_text = SCHEMA_PERSONAL_SQL.read_text()
    for name in PERSONAL_TABLES + PERSONAL_VIEWS:
        assert re.search(rf"\b{name}\b", personal_text), (
            f"{name} should be defined in schema_personal.sql"
        )
        assert not re.search(rf"CREATE (TABLE|VIEW)[^;]*\b{name}\b", core_text), (
            f"{name} must NOT be created in core schema.sql"
        )


def test_personal_schema_uses_if_not_exists_exclusively() -> None:
    """Every CREATE in schema_personal.sql is guarded with IF NOT EXISTS."""
    text = SCHEMA_PERSONAL_SQL.read_text()
    bare_creates = re.findall(
        r"CREATE (?:TABLE|VIEW)(?!\s+IF\s+NOT\s+EXISTS)", text, re.IGNORECASE
    )
    assert not bare_creates, f"unguarded CREATE in schema_personal.sql: {bare_creates}"


# --- Feature-flag default ------------------------------------------------


def test_personal_pm_flag_defaults_true() -> None:
    """The owner prod install keeps personal tables unless it opts out."""
    assert DEFAULT_FEATURE_FLAGS.get("personal_pm_tables") is True
    assert is_feature_enabled(None, "personal_pm_tables") is True
    # A public install can opt out explicitly.
    off = {"features": {"personal_pm_tables": False}}
    assert is_feature_enabled(off, "personal_pm_tables") is False


# --- Fresh-install behaviour (raw SQL) -----------------------------------


def test_fresh_core_install_omits_personal_tables(tmp_path: Path) -> None:
    """Applying schema.sql alone creates core tables but no personal ones."""
    db_path = tmp_path / "core_only.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL.read_text())
        conn.commit()
        tables = _table_names(conn)
        views = _view_names(conn)
    finally:
        conn.close()

    for core in CORE_TABLES:
        assert core in tables, f"core table {core} missing from fresh core install"
    for personal in PERSONAL_TABLES:
        assert personal not in tables, (
            f"personal table {personal} leaked into core-only install"
        )
    for view in PERSONAL_VIEWS:
        assert view not in views, f"personal view {view} leaked into core-only install"


def test_fresh_install_with_personal_creates_all(tmp_path: Path) -> None:
    """Applying both files creates the personal tables and views too."""
    db_path = tmp_path / "with_personal.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL.read_text())
        conn.executescript(SCHEMA_PERSONAL_SQL.read_text())
        conn.commit()
        tables = _table_names(conn)
        views = _view_names(conn)
    finally:
        conn.close()

    for personal in PERSONAL_TABLES:
        assert personal in tables, f"{personal} missing after applying personal schema"
    for view in PERSONAL_VIEWS:
        assert view in views, f"{view} missing after applying personal schema"


# --- ensure_schema() flag-gated fresh install ----------------------------


def _run_ensure_schema(monkeypatch, tmp_path: Path, apply_personal: bool) -> Path:
    """Point ensure_schema at a temp DB and run it with the given personal flag."""
    db_path = tmp_path / f"ensure_{apply_personal}.db"
    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)
    monkeypatch.setattr(equipa_db, "_SCHEMA_ENSURED", False)
    equipa_db.ensure_schema(apply_personal=apply_personal)
    return db_path


def test_ensure_schema_flag_off_skips_personal(monkeypatch, tmp_path: Path) -> None:
    db_path = _run_ensure_schema(monkeypatch, tmp_path, apply_personal=False)
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    for core in CORE_TABLES:
        assert core in tables
    for personal in PERSONAL_TABLES:
        assert personal not in tables


def test_ensure_schema_flag_on_creates_personal(monkeypatch, tmp_path: Path) -> None:
    db_path = _run_ensure_schema(monkeypatch, tmp_path, apply_personal=True)
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    for personal in PERSONAL_TABLES:
        assert personal in tables


# --- Idempotency / data-safety on re-apply -------------------------------


def test_reapply_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    """Re-applying the schema over a populated DB errors nothing and drops nothing.

    Simulates the live-prod case: the DB already has the tables and data, and
    pulling this code (which re-runs ensure_schema) must touch NOTHING.
    """
    db_path = tmp_path / "idempotent.db"
    core_sql = equipa_db._make_schema_idempotent(SCHEMA_SQL.read_text())
    personal_sql = equipa_db._make_schema_idempotent(SCHEMA_PERSONAL_SQL.read_text())

    conn = sqlite3.connect(db_path)
    try:
        # First install: core + personal, then seed a row into a personal table.
        conn.executescript(core_sql)
        conn.executescript(personal_sql)
        conn.execute(
            "INSERT INTO projects (name) VALUES ('acme')",
        )
        project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
        conn.execute(
            "INSERT INTO reminders (project_id, title, reminder_date) "
            "VALUES (?, 'ship', '2026-07-11')",
            (project_id,),
        )
        conn.commit()

        # Re-apply BOTH schema files: idempotent CREATE ... IF NOT EXISTS must
        # neither raise nor disturb the seeded rows.
        conn.executescript(core_sql)
        conn.executescript(personal_sql)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert (
            conn.execute("SELECT title FROM reminders").fetchone()[0] == "ship"
        )
    finally:
        conn.close()


# --- Default (flag-resolved) ensure_schema path --------------------------
# The tests above force apply_personal explicitly. Production callers use the
# default apply_personal=None path, which resolves the flag through
# _should_apply_personal_schema(). These pin that real code path.


def test_should_apply_personal_schema_resolves_flag(monkeypatch) -> None:
    """The helper honours the resolved ``personal_pm_tables`` flag.

    Uses the real ``is_feature_enabled`` resolution over a crafted config so an
    explicit opt-out disables the personal schema while the default (empty
    config, flag defaults TRUE) keeps it on. ``_should_apply_personal_schema``
    imports ``load_dispatch_config`` lazily, so patching it on ``equipa.config``
    is picked up at call time.
    """
    import equipa.config as equipa_config

    monkeypatch.setattr(
        equipa_config,
        "load_dispatch_config",
        lambda _path: {"features": {"personal_pm_tables": False}},
    )
    assert equipa_db._should_apply_personal_schema() is False

    monkeypatch.setattr(equipa_config, "load_dispatch_config", lambda _path: {})
    assert equipa_db._should_apply_personal_schema() is True


def _run_ensure_schema_default(monkeypatch, tmp_path: Path, label: str) -> Path:
    """Run ensure_schema() with the DEFAULT (flag-resolved) personal path."""
    db_path = tmp_path / f"default_{label}.db"
    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)
    monkeypatch.setattr(equipa_db, "_SCHEMA_ENSURED", False)
    equipa_db.ensure_schema()  # apply_personal=None → resolve the flag
    return db_path


def test_ensure_schema_default_flag_off_skips_personal(monkeypatch, tmp_path: Path) -> None:
    """Default ensure_schema() with the flag resolved OFF omits personal tables."""
    monkeypatch.setattr(equipa_db, "_should_apply_personal_schema", lambda: False)
    db_path = _run_ensure_schema_default(monkeypatch, tmp_path, "off")
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    for core in CORE_TABLES:
        assert core in tables, f"core table {core} missing on default install"
    for personal in PERSONAL_TABLES:
        assert personal not in tables, f"{personal} leaked with flag resolved off"


def test_ensure_schema_default_flag_on_creates_personal(monkeypatch, tmp_path: Path) -> None:
    """Default ensure_schema() with the flag resolved ON creates personal tables."""
    monkeypatch.setattr(equipa_db, "_should_apply_personal_schema", lambda: True)
    db_path = _run_ensure_schema_default(monkeypatch, tmp_path, "on")
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    for personal in PERSONAL_TABLES:
        assert personal in tables, f"{personal} missing with flag resolved on"


def test_ensure_schema_missing_personal_file_is_safe(monkeypatch, tmp_path: Path) -> None:
    """Both halves of the guard hold: flag ON but file absent creates only core.

    Simulates a public checkout that shipped without ``schema_personal.sql``
    while the flag is still on — ensure_schema() must not raise and must not
    conjure the personal tables (the guard is ``flag AND file exists``).
    """
    missing = tmp_path / "no_such_schema_personal.sql"
    assert not missing.exists()
    monkeypatch.setattr(equipa_db, "SCHEMA_PERSONAL_SQL_PATH", missing)

    db_path = tmp_path / "missing_personal.db"
    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)
    monkeypatch.setattr(equipa_db, "_SCHEMA_ENSURED", False)
    equipa_db.ensure_schema(apply_personal=True)  # flag on, but file is gone

    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    for core in CORE_TABLES:
        assert core in tables, f"core table {core} missing when personal file absent"
    for personal in PERSONAL_TABLES:
        assert personal not in tables, (
            f"{personal} created despite schema_personal.sql being absent"
        )
