#!/usr/bin/env python3
"""Shared pytest fixtures for EQUIPA test suite.

Ensures database schema is created before any test runs by executing
schema.sql with CREATE TABLE IF NOT EXISTS semantics.

Copyright 2026 Forgeborn
"""

import re
import sqlite3
import sys
from pathlib import Path

# Add parent directory (repo root) to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Also expose scripts/ so modules relocated there during repo cleanup (e.g.
# forgesmith_simba, forgesmith_impact) import by bare name in tests, exactly as
# forgesmith.py already arranges them at runtime. This makes `from
# forgesmith_simba import ...` resolve to scripts/forgesmith_simba.py on every
# platform, with no reliance on the repo-root symlink that Windows checkouts
# (core.symlinks=false) materialize as a broken text file.
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if _SCRIPTS_DIR.is_dir():
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_idempotent(schema_sql: str) -> str:
    """Rewrite CREATE statements to be idempotent (IF NOT EXISTS).

    Covers TABLE / VIEW / TRIGGER / INDEX / UNIQUE INDEX so applying a schema
    file more than once (or over an already-populated DB) is a safe no-op.
    """
    for keyword in ("TABLE", "VIEW", "TRIGGER", "INDEX"):
        schema_sql = re.sub(
            rf"CREATE {keyword}(?!\s+IF\s+NOT\s+EXISTS)",
            f"CREATE {keyword} IF NOT EXISTS",
            schema_sql,
            flags=re.IGNORECASE,
        )
    schema_sql = re.sub(
        r"CREATE UNIQUE INDEX(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE UNIQUE INDEX IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    return schema_sql


def _apply_schema_file(conn, schema_path):
    """Idempotently apply one schema .sql file to an open connection."""
    if not schema_path.exists():
        print(f"  [conftest] WARNING: schema not found at {schema_path}")
        return
    try:
        conn.executescript(_make_idempotent(schema_path.read_text()))
        conn.commit()
    except Exception as e:
        print(f"  [conftest] WARNING: {schema_path.name} partial apply: {e}")


def _ensure_full_schema():
    """Apply BOTH schema files to the test database, creating missing tables.

    The test suite needs every table — including the owner-only personal-PM
    tables split into schema_personal.sql (task 2707) — so both files are
    applied here regardless of the personal_pm_tables feature flag. Both are
    rewritten to CREATE ... IF NOT EXISTS, so re-applying is a safe no-op.
    """
    from equipa.constants import THEFORGE_DB

    conn = sqlite3.connect(THEFORGE_DB)
    try:
        _apply_schema_file(conn, REPO_ROOT / "schema.sql")
        _apply_schema_file(conn, REPO_ROOT / "schema_personal.sql")
    finally:
        conn.close()


def pytest_configure(config):
    """Ensure database schema exists before any tests collect."""
    try:
        from equipa import db as equipa_db

        # Reset ensure_schema cache so it re-runs for test DB
        equipa_db._SCHEMA_ENSURED = False
        equipa_db.ensure_schema()

        # Apply the full schema.sql for tables not covered by ensure_schema()
        _ensure_full_schema()
    except (ImportError, ModuleNotFoundError) as e:
        print(f"  [conftest] WARNING: could not import equipa.db: {e}")
        print("  [conftest] Schema setup skipped — tests requiring DB may fail.")


def pytest_collection_modifyitems(session, config, items):
    """After collection, call setup_test_data() for modules that define it.

    This replaces the manual setup that was done in each module's run_all_tests().
    """
    setup_modules = set()
    for item in items:
        module = item.module
        if module not in setup_modules and hasattr(module, "setup_test_data"):
            try:
                module.setup_test_data()
            except Exception as e:
                print(f"  [conftest] WARNING: setup_test_data() failed for {module.__name__}: {e}")
            setup_modules.add(module)
