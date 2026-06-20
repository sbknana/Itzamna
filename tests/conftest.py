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


def _ensure_full_schema():
    """Apply schema.sql to the test database, creating any missing tables.

    Converts CREATE TABLE to CREATE TABLE IF NOT EXISTS and
    CREATE VIEW to CREATE VIEW IF NOT EXISTS so this is idempotent.
    CREATE INDEX already uses IF NOT EXISTS in the schema file.
    """
    from equipa.constants import THEFORGE_DB

    schema_path = REPO_ROOT / "schema.sql"
    if not schema_path.exists():
        print(f"  [conftest] WARNING: schema.sql not found at {schema_path}")
        return

    schema_sql = schema_path.read_text()

    # Make CREATE TABLE idempotent
    schema_sql = re.sub(
        r"CREATE TABLE(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE TABLE IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    # Make CREATE VIEW idempotent
    schema_sql = re.sub(
        r"CREATE VIEW(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE VIEW IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    # Make CREATE TRIGGER idempotent
    schema_sql = re.sub(
        r"CREATE TRIGGER(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE TRIGGER IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    # Make CREATE INDEX idempotent (most already are)
    schema_sql = re.sub(
        r"CREATE INDEX(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE INDEX IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    # Make CREATE UNIQUE INDEX idempotent
    schema_sql = re.sub(
        r"CREATE UNIQUE INDEX(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE UNIQUE INDEX IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )

    conn = sqlite3.connect(THEFORGE_DB)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    except Exception as e:
        print(f"  [conftest] WARNING: schema.sql partial apply: {e}")
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
