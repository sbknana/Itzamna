#!/usr/bin/env python3
"""EQUIPA Database Migration System.

Detects the current schema version of an existing TheForge database and applies
incremental migrations to bring it up to the latest version. Backs up the DB
before any changes.

Version detection:
- PRAGMA user_version (set by migrations and schema.sql for new installs)
- Fingerprinting (for legacy DBs that predate the migration system)

Usage:
    # Standalone
    python db_migrate.py /path/to/theforge.db

    # From equipa_setup.py (called automatically)
    from db_migrate import run_migrations
    success, from_ver, to_ver = run_migrations("/path/to/theforge.db")

Stdlib only — no pip dependencies required.

Copyright 2026 Forgeborn
"""

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# The schema version that matches the current schema.sql
CURRENT_VERSION = 13


# ============================================================
# Version detection
# ============================================================

def get_db_version(conn):
    """Read the schema version from SQLite's PRAGMA user_version.

    Returns 0 for databases that have never been versioned.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_db_version(conn, version):
    """Set the schema version via PRAGMA user_version.

    PRAGMA statements can't use parameters, so we validate the int first.
    """
    version = int(version)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def detect_legacy_version(conn):
    """Fingerprint unversioned databases by which tables exist.

    Used for databases created before the migration system was added.
    Returns the detected version number (0, 1, 2, or 3).
    """
    tables = set(
        row[0] for row in
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )

    # v3: has the agent messaging/observability tables
    if "agent_messages" in tables and "agent_actions" in tables:
        return 3

    # v2: has ForgeSmith self-improvement tables
    if "lessons_learned" in tables and "rubric_scores" in tables:
        return 2

    # v1: has the core project management tables
    if "projects" in tables and "tasks" in tables:
        return 1

    # v0: fresh or near-empty database
    return 0


def get_effective_version(conn):
    """Get the database version, falling back to fingerprinting.

    Checks PRAGMA user_version first. If 0, tries fingerprinting to
    detect legacy databases that predate the version marker.
    """
    version = get_db_version(conn)
    if version == 0:
        detected = detect_legacy_version(conn)
        if detected > 0:
            return detected
    return version


# ============================================================
# Backup
# ============================================================

def backup_database(db_path):
    """Create a timestamped backup of the database file.

    Returns the Path to the backup file.
    Raises OSError if the copy fails.
    """
    db_path = Path(db_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}_backup_{ts}{db_path.suffix}"
    backup_path = db_path.parent / backup_name
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


# ============================================================
# Audit log
# ============================================================

def ensure_schema_migrations_table(conn):
    """Create the schema_migrations audit log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL,
            description TEXT,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def log_migration(conn, from_ver, to_ver, description):
    """Record a completed migration step in the audit log."""
    conn.execute(
        "INSERT INTO schema_migrations (from_version, to_version, description) "
        "VALUES (?, ?, ?)",
        (from_ver, to_ver, description),
    )
    conn.commit()


# ============================================================
# Migration functions
# ============================================================

def migrate_v0_to_v1(conn):
    """Stamp existing v1 databases. No schema changes needed.

    v1 databases already have the core 19 tables (projects, tasks,
    decisions, etc.). This migration just sets the version marker so
    future upgrades know where we are.
    """
    pass  # No-op — v1 schema is the baseline


def migrate_v1_to_v2(conn):
    """Add ForgeSmith and agent tracking tables (v1.0 -> v2.1).

    Adds 9 tables:
      agent_runs, voice_messages, api_keys,
      lessons_learned, agent_episodes, forgesmith_runs,
      forgesmith_changes, rubric_scores, rubric_evolution_history

    Adds 2 indexes on agent_runs.
    Adds 2 views: v_cost_by_project, v_cost_by_role.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            project_id INTEGER,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            turns_used INTEGER DEFAULT 0,
            duration_s REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            success INTEGER DEFAULT 0,
            outcome TEXT,
            output_tail TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS voice_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reply_to INTEGER,
            metadata TEXT,
            created_at DATETIME DEFAULT (datetime('now')),
            processed_at DATETIME
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL,
            notes TEXT,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lessons_learned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            role TEXT,
            error_type TEXT,
            error_signature TEXT,
            lesson TEXT NOT NULL,
            source TEXT DEFAULT 'forgesmith',
            times_seen INTEGER DEFAULT 1,
            times_injected INTEGER DEFAULT 0,
            effectiveness_score REAL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS agent_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            role TEXT,
            task_type TEXT,
            project_id INTEGER,
            approach_summary TEXT,
            turns_used INTEGER,
            outcome TEXT,
            error_patterns TEXT,
            reflection TEXT,
            q_value REAL DEFAULT 0.5,
            created_at TEXT DEFAULT (datetime('now')),
            times_injected INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS forgesmith_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            agent_runs_analyzed INTEGER DEFAULT 0,
            changes_made INTEGER DEFAULT 0,
            summary TEXT,
            mode TEXT DEFAULT 'auto'
        );

        CREATE TABLE IF NOT EXISTS forgesmith_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            target_file TEXT,
            old_value TEXT,
            new_value TEXT,
            rationale TEXT NOT NULL,
            evidence TEXT,
            effectiveness_score REAL,
            reverted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rubric_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id INTEGER NOT NULL,
            task_id INTEGER,
            project_id INTEGER,
            role TEXT NOT NULL,
            rubric_version INTEGER DEFAULT 1,
            criteria_scores TEXT NOT NULL,
            total_score REAL NOT NULL,
            max_possible REAL NOT NULL,
            normalized_score REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rubric_evolution_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rubric_version INTEGER NOT NULL,
            role TEXT NOT NULL,
            criterion TEXT NOT NULL,
            old_weight REAL NOT NULL,
            new_weight REAL NOT NULL,
            correlation REAL NOT NULL,
            sample_size_success INTEGER,
            sample_size_failure INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_agent_runs_project
            ON agent_runs(project_id);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_role
            ON agent_runs(role);

        CREATE VIEW IF NOT EXISTS v_cost_by_project AS
        SELECT
            p.codename,
            COUNT(ar.id) as total_runs,
            SUM(ar.turns_used) as total_turns,
            ROUND(SUM(ar.duration_s), 1) as total_duration_s,
            ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
            SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs,
            SUM(CASE WHEN ar.success = 0 THEN 1 ELSE 0 END) as failed_runs
        FROM agent_runs ar
        JOIN projects p ON ar.project_id = p.id
        GROUP BY p.codename
        ORDER BY total_cost_usd DESC;

        CREATE VIEW IF NOT EXISTS v_cost_by_role AS
        SELECT
            ar.role,
            COUNT(ar.id) as total_runs,
            SUM(ar.turns_used) as total_turns,
            ROUND(SUM(ar.duration_s), 1) as total_duration_s,
            ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
            ROUND(AVG(ar.cost_usd), 4) as avg_cost_per_run,
            SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs
        FROM agent_runs ar
        GROUP BY ar.role
        ORDER BY total_cost_usd DESC;
    """)


def migrate_v2_to_v3(conn):
    """Add agent messaging and action logging tables (v2.1 -> v3.0).

    Adds 2 tables: agent_messages, agent_actions.
    Adds 2 indexes on agent_actions.
    Adds prompt_version column to agent_runs (for A/B testing).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            cycle_number INTEGER NOT NULL,
            from_role TEXT NOT NULL,
            to_role TEXT NOT NULL,
            message_type TEXT NOT NULL,
            content TEXT NOT NULL,
            read_by_cycle INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            run_id INTEGER,
            cycle_number INTEGER NOT NULL,
            role TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            tool_input_preview TEXT,
            input_hash TEXT,
            output_length INTEGER,
            success INTEGER NOT NULL DEFAULT 1,
            error_type TEXT,
            error_summary TEXT,
            duration_ms INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_agent_actions_task
            ON agent_actions(task_id, cycle_number);
        CREATE INDEX IF NOT EXISTS idx_agent_actions_tool
            ON agent_actions(tool_name, success);
    """)

    # Add prompt_version column to agent_runs if it doesn't exist.
    # SQLite's ALTER TABLE ADD COLUMN doesn't support IF NOT EXISTS,
    # so we catch the "duplicate column" error.
    try:
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN prompt_version TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


def migrate_v3_to_v4(conn):
    """Add impact_assessment column to forgesmith_changes (v3.0 -> v4.0).

    Stores JSON impact analysis results (affected roles, task types, risk level)
    for each ForgeSmith change. HIGH-risk changes are blocked from auto-apply.
    """
    try:
        conn.execute(
            "ALTER TABLE forgesmith_changes "
            "ADD COLUMN impact_assessment TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


def migrate_v4_to_v5(conn):
    """Add embedding columns and lesson graph table (v4.0 -> v5.0).

    Adds:
    - embedding TEXT column to lessons_learned (for semantic search)
    - embedding TEXT column to agent_episodes (for episode similarity)
    - lesson_graph_edges table (for relationship mapping between lessons)
    - Indexes on src_id and dst_id for graph traversal performance
    """
    # Add embedding column to lessons_learned
    try:
        conn.execute(
            "ALTER TABLE lessons_learned "
            "ADD COLUMN embedding TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add embedding column to agent_episodes
    try:
        conn.execute(
            "ALTER TABLE agent_episodes "
            "ADD COLUMN embedding TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Create lesson_graph_edges table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lesson_graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(src_id, dst_id, edge_type)
        );

        CREATE INDEX IF NOT EXISTS idx_lesson_graph_src
            ON lesson_graph_edges(src_id);
        CREATE INDEX IF NOT EXISTS idx_lesson_graph_dst
            ON lesson_graph_edges(dst_id);
    """)


def migrate_v5_to_v6(conn):
    """Add decision staleness tracking (v5.0 -> v6.0).

    Adds:
    - last_validated DATETIME column to decisions table
    - v_stale_decisions view for decisions unvalidated for 60+ days
    """
    # Add last_validated column to decisions
    try:
        conn.execute(
            "ALTER TABLE decisions "
            "ADD COLUMN last_validated DATETIME DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Create v_stale_decisions view
    conn.executescript("""
        DROP VIEW IF EXISTS v_stale_decisions;
        CREATE VIEW v_stale_decisions AS
        SELECT d.*, p.codename as project_name,
               julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) as days_since_validation
        FROM decisions d
        JOIN projects p ON d.project_id = p.id
        WHERE julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) > 60;
    """)


def migrate_v6_to_v7(conn):
    """Add decision type, status, resolution tracking (v6.0 -> v7.0).

    Adds to decisions table:
    - decision_type TEXT (general, security_finding, architectural,
      trade_off, resolution) DEFAULT 'general'
    - status TEXT (open, resolved, superseded, wont_fix,
      failed_resolution) DEFAULT 'open'
    - resolved_by_task_id INTEGER (nullable FK to tasks)
    - verified_at DATETIME (nullable)

    Adds indexes:
    - idx_decisions_type on (decision_type)
    - idx_decisions_status on (status)
    - idx_decisions_resolved_by on (resolved_by_task_id)

    Adds view:
    - v_open_security_findings: open security findings with project name

    Recreates v_stale_decisions to include new columns in WHERE logic.
    """
    # Add decision_type column
    try:
        conn.execute(
            "ALTER TABLE decisions "
            "ADD COLUMN decision_type TEXT NOT NULL DEFAULT 'general'"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add status column
    try:
        conn.execute(
            "ALTER TABLE decisions "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add resolved_by_task_id column (nullable FK to tasks)
    try:
        conn.execute(
            "ALTER TABLE decisions "
            "ADD COLUMN resolved_by_task_id INTEGER DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add verified_at timestamp
    try:
        conn.execute(
            "ALTER TABLE decisions "
            "ADD COLUMN verified_at DATETIME DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Create indexes for the new columns
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_decisions_type
            ON decisions(decision_type);
        CREATE INDEX IF NOT EXISTS idx_decisions_status
            ON decisions(status);
        CREATE INDEX IF NOT EXISTS idx_decisions_resolved_by
            ON decisions(resolved_by_task_id);
    """)

    # Create v_open_security_findings view
    conn.executescript("""
        CREATE VIEW IF NOT EXISTS v_open_security_findings AS
        SELECT d.id, d.project_id, p.codename as project_name,
               d.topic, d.decision, d.rationale,
               d.decided_at, d.last_validated,
               d.resolved_by_task_id
        FROM decisions d
        JOIN projects p ON d.project_id = p.id
        WHERE d.decision_type = 'security_finding'
          AND d.status = 'open';
    """)

    # Recreate v_stale_decisions to exclude resolved/wont_fix decisions
    conn.executescript("""
        DROP VIEW IF EXISTS v_stale_decisions;
        CREATE VIEW v_stale_decisions AS
        SELECT d.*, p.codename as project_name,
               julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) as days_since_validation
        FROM decisions d
        JOIN projects p ON d.project_id = p.id
        WHERE d.status NOT IN ('resolved', 'wont_fix', 'failed_resolution')
          AND julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) > 60;
    """)


def migrate_v7_to_v8(conn):
    """Add partial unique index on lessons_learned (v7.0 -> v8.0).

    Adds:
    - UNIQUE INDEX idx_lessons_sig_source_active on
      lessons_learned(error_signature, source) WHERE active = 1

    The index supports the INSERT ... ON CONFLICT(error_signature, source)
    upsert path in equipa.loops._create_review_lessons, collapsing the prior
    SELECT + UPDATE/INSERT round-trips into a single statement.

    Scoping the index to active = 1 preserves the original dedup semantics
    (only active rows are deduplicated; deactivated lessons can coexist with
    a newer active one for the same signature+source).

    Before creating the index, deactivates duplicate active rows by keeping
    the most-recently-updated row in each (error_signature, source) group
    and folding the duplicates' times_seen counts into the survivor.
    """
    # Skip if the lessons_learned table doesn't exist yet. The table is
    # created lazily by the orchestrator on first run (see schema.sql /
    # ensure_schema), and minimal test fixtures may upgrade through this
    # version without it. The unique index is only meaningful once the
    # table exists; ensure_schema() will recreate it via schema.sql.
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'lessons_learned'"
    ).fetchone()
    if not table_exists:
        return

    # Find duplicate (error_signature, source) groups among active rows.
    # The legacy code's SELECT-then-INSERT had a benign race that could
    # produce duplicates under concurrent writers; clean those up so the
    # UNIQUE INDEX creation succeeds.
    duplicate_groups = conn.execute(
        """SELECT error_signature, source, COUNT(*) AS n
           FROM lessons_learned
           WHERE active = 1
           GROUP BY error_signature, source
           HAVING n > 1"""
    ).fetchall()

    for row in duplicate_groups:
        sig = row["error_signature"] if hasattr(row, "keys") else row[0]
        src = row["source"] if hasattr(row, "keys") else row[1]
        rows = conn.execute(
            """SELECT id, COALESCE(times_seen, 1) AS times_seen
               FROM lessons_learned
               WHERE active = 1
                 AND error_signature IS ?
                 AND source IS ?
               ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, id DESC""",
            (sig, src),
        ).fetchall()
        if len(rows) <= 1:
            continue
        keeper_id = rows[0]["id"] if hasattr(rows[0], "keys") else rows[0][0]
        loser_total = sum(
            (r["times_seen"] if hasattr(r, "keys") else r[1]) for r in rows[1:]
        )
        loser_ids = [
            (r["id"] if hasattr(r, "keys") else r[0]) for r in rows[1:]
        ]
        # Fold duplicates' times_seen into the keeper, then deactivate them.
        placeholders = ",".join("?" * len(loser_ids))
        conn.execute(
            "UPDATE lessons_learned "
            "SET times_seen = COALESCE(times_seen, 1) + ?, "
            "    updated_at = datetime('now') "
            "WHERE id = ?",
            (loser_total, keeper_id),
        )
        conn.execute(
            f"UPDATE lessons_learned SET active = 0 WHERE id IN ({placeholders})",
            loser_ids,
        )
    conn.commit()

    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_sig_source_active
            ON lessons_learned(error_signature, source) WHERE active = 1;
    """)


def migrate_v8_to_v9(conn):
    """Add Task Flow tables (v8 -> v9).

    Adds three tables for OpenClaw-inspired multi-step orchestration:

    * ``flows``           - one row per multi-step orchestration. Tracks state
                            (queued/running/paused/cancelled/done/failed) and
                            a monotonic ``revision`` counter for optimistic
                            concurrency / restart recovery.
    * ``flow_revisions``  - append-only audit log of every state transition.
    * ``flow_tasks``      - many-to-many between flows and managed/mirrored
                            child tasks.

    The schema is idempotent (CREATE TABLE IF NOT EXISTS) so it is safe to
    re-run after partial failure.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            parent_task_id INTEGER,
            title TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued','running','paused','cancelled','done','failed')),
            revision INTEGER NOT NULL DEFAULT 0,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancelled_reason TEXT,
            completed_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_flows_project_state ON flows(project_id, state);
        CREATE INDEX IF NOT EXISTS idx_flows_state ON flows(state);

        CREATE TABLE IF NOT EXISTS flow_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            event TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (flow_id) REFERENCES flows(id),
            UNIQUE(flow_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_flow_revisions_flow
            ON flow_revisions(flow_id, revision);

        CREATE TABLE IF NOT EXISTS flow_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            role TEXT,
            relationship TEXT NOT NULL DEFAULT 'managed'
                CHECK (relationship IN ('managed','mirrored')),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','running','done','failed','cancelled')),
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY (flow_id) REFERENCES flows(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            UNIQUE(flow_id, task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_flow_tasks_flow ON flow_tasks(flow_id);
        CREATE INDEX IF NOT EXISTS idx_flow_tasks_task ON flow_tasks(task_id);
    """)
    conn.commit()


def migrate_v9_to_v10(conn):
    """Add Paperclip config-version tracking tables (v9 -> v10).

    Adds two tables for snapshotting orchestrator configuration files
    (per PLAN-1067.md A1):

    * ``config_versions``       - one row per snapshot of the config tree.
                                  ``content_sha`` is a SHA-256 over the
                                  sorted (file_path, file_sha) pairs and is
                                  used to dedup identical snapshots.
    * ``config_version_files``  - the actual file blobs (plain text) for a
                                  given version. ON DELETE CASCADE so that
                                  removing a version cleans up its files.

    The schema is idempotent (CREATE TABLE IF NOT EXISTS) so it is safe to
    re-run after partial failure.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS config_versions (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            created_at TEXT,
            source TEXT CHECK (source IN ('manual','auto-dispatch','auto-cli','auto-rollback')),
            commit_message TEXT,
            content_sha TEXT NOT NULL,
            parent_version_id INTEGER,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE INDEX IF NOT EXISTS idx_config_versions_project_created
            ON config_versions(project_id, created_at);

        CREATE TABLE IF NOT EXISTS config_version_files (
            id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            content_blob TEXT NOT NULL,
            file_sha TEXT NOT NULL,
            byte_size INTEGER,
            FOREIGN KEY(version_id) REFERENCES config_versions(id) ON DELETE CASCADE,
            UNIQUE(version_id, file_path)
        );
        CREATE INDEX IF NOT EXISTS idx_cvf_version
            ON config_version_files(version_id);
    """)
    conn.commit()


def migrate_v10_to_v11(conn):
    """Add Paperclip agent-session capture table (v10 -> v11).

    Adds the ``agent_sessions`` table per PLAN-1067.md B1. This is the
    persistence layer for capturing the rolling state of an in-flight agent
    so that the orchestrator can resume / postmortem after a crash, kill,
    or context compaction.

    state_json SHAPE (stable contract — additions must be backwards
    compatible; existing readers must tolerate unknown keys)::

        {
            "open_files":         [str, ...],
            "files_changed":      [str, ...],
            "files_read":         [str, ...],
            "recent_tool_calls":  [
                {"tool": str, "args_hash": str, "ok": bool, "turn": int},
                ...
            ],
            "partial_reasoning":  str,    # truncated to 8 KB
            "turn_count":         int,
            "compaction_count":   int,
            "soft_checkpoint_path": str,
        }

    This is a strict superset of the existing soft-checkpoint dict, so
    ``open_files`` / ``files_changed`` / ``files_read`` / ``turn_count``
    / ``compaction_count`` reuse the names already produced upstream.

    ``cycle_id`` is an opaque string from the perspective of the persistence
    layer — it is either a heartbeat-tick UUID or a flow-revision marker,
    written and read by callers that know its provenance.

    Per the operator decision in PLAN-1067, ``expires_at`` is set by the
    capture-side caller (created_at + 14 days). The schema does not enforce
    the default; this keeps the column truthful for callers that want
    to override (e.g. shorter expiry for security-sensitive sessions).

    FK behaviour: ``agent_sessions`` does NOT cascade-delete with
    ``tasks`` — sessions are intentionally kept after task purge so they
    are available for postmortem analysis.

    The schema is idempotent (CREATE TABLE / CREATE INDEX IF NOT EXISTS)
    so it is safe to re-run after partial failure.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            cycle_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            byte_size INTEGER,
            created_at TEXT,
            last_seen_at TEXT,
            expires_at TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_task_role_seen
            ON agent_sessions(task_id, role, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_expires
            ON agent_sessions(expires_at);
    """)
    conn.commit()


def migrate_v11_to_v12(conn):
    """Worktree isolation tracking (v11 -> v12).

    The ``worktrees`` table that the per-task git worktree isolation helpers
    (task 2488) already create on demand, stamped as a schema version so a
    fresh install and an upgraded one agree. Idempotent: CREATE IF NOT
    EXISTS matches the DDL the helpers wrote.
    """
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            branch TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status);
        CREATE INDEX IF NOT EXISTS idx_worktrees_task ON worktrees(task_id);
        CREATE INDEX IF NOT EXISTS idx_worktrees_project ON worktrees(project_id);
    """)
    conn.commit()


def migrate_v12_to_v13(conn):
    """Task movement: tasks.updated_at (v12 -> v13).

    * ``tasks.updated_at`` - the per-task movement signal the schema never
      had. Backfilled from completed_at, else created_at, and kept current by
      a trigger that yields to an explicit value set in the same UPDATE.
    * ``v_stale_tasks`` - re-based on updated_at so it measures movement,
      not age since creation (which fired on every long-lived task).

    (An earlier draft of v13 also added a ``conditions`` checklist table;
    that was withdrawn - conditions are TICKER's concept and live in TICKER's
    own store, not in TheForge - so this migration is updated_at only.)
    """
    cursor = conn.cursor()
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(tasks)").fetchall()}
    if "updated_at" not in cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN updated_at DATETIME")
        # Backfill from whatever timestamps the table actually has. Minimal
        # tasks tables (some migration-chain fixtures) carry neither, so an
        # unconditional COALESCE(completed_at, created_at) would raise "no
        # such column"; a real production tasks table has both.
        have = [c for c in ("completed_at", "created_at") if c in cols]
        if have:
            expr = f"COALESCE({', '.join(have)})" if len(have) > 1 else have[0]
            cursor.execute(
                f"UPDATE tasks SET updated_at = {expr} WHERE updated_at IS NULL"
            )
    cursor.executescript("""
        CREATE TRIGGER IF NOT EXISTS update_task_timestamp
        AFTER UPDATE ON tasks
        WHEN NEW.updated_at IS OLD.updated_at
        BEGIN
            UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
        END;

        DROP VIEW IF EXISTS v_stale_tasks;
        CREATE VIEW v_stale_tasks AS
        SELECT t.*, p.codename as project_name,
               julianday('now') - julianday(COALESCE(t.updated_at, t.created_at)) as days_stale
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'in_progress'
          AND julianday('now') - julianday(COALESCE(t.updated_at, t.created_at)) > 3;
    """)
    conn.commit()


# Migration registry: version -> (description, function)
MIGRATIONS = {
    1: ("Baseline schema stamp (v0 -> v1)", migrate_v0_to_v1),
    2: ("ForgeSmith + agent tracking (v1 -> v2)", migrate_v1_to_v2),
    3: ("Agent messaging + action logging (v2 -> v3)", migrate_v2_to_v3),
    4: ("Impact assessment for ForgeSmith changes (v3 -> v4)", migrate_v3_to_v4),
    5: ("Embedding columns + lesson graph (v4 -> v5)", migrate_v4_to_v5),
    6: ("Decision staleness tracking (v5 -> v6)", migrate_v5_to_v6),
    7: ("Decision type/status + resolution tracking (v6 -> v7)", migrate_v6_to_v7),
    8: ("Partial unique index on lessons_learned (v7 -> v8)", migrate_v7_to_v8),
    9: ("Task Flow tables (v8 -> v9)", migrate_v8_to_v9),
    10: ("Paperclip config version tables (v9 -> v10)", migrate_v9_to_v10),
    11: ("Paperclip agent_sessions table (v10 -> v11)", migrate_v10_to_v11),
    12: ("Worktree isolation tracking (v11 -> v12)", migrate_v11_to_v12),
    13: ("Task updated_at movement signal (v12 -> v13)", migrate_v12_to_v13),
}


# ============================================================
# Main migration runner
# ============================================================

def run_migrations(db_path, silent=False):
    """Run all pending migrations on a database.

    Steps:
    1. Detect current version (PRAGMA or fingerprint)
    2. Back up the database
    3. Apply each migration in order
    4. Update PRAGMA user_version after each step
    5. Log each migration to schema_migrations table

    Args:
        db_path: Path to the SQLite database file.
        silent: If True, suppress print output.

    Returns:
        (success, from_version, to_version) tuple.
        success is True if all migrations applied cleanly.
    """
    db_path = Path(db_path)

    if not db_path.exists():
        if not silent:
            print(f"  ERROR: Database not found: {db_path}")
        return False, 0, 0

    conn = sqlite3.connect(str(db_path))
    try:
        from_version = get_effective_version(conn)
    finally:
        conn.close()

    if from_version >= CURRENT_VERSION:
        if not silent:
            print(f"  Database is up to date (v{from_version}).")
        return True, from_version, from_version

    # Back up before making any changes
    if not silent:
        print(f"  Detected schema version: v{from_version} (current is v{CURRENT_VERSION})")

    try:
        backup_path = backup_database(db_path)
        if not silent:
            print(f"  Backup created: {backup_path.name}")
    except OSError as e:
        if not silent:
            print(f"  WARNING: Could not create backup: {e}")
            print(f"  Proceeding without backup is risky.")
            response = input("  Continue anyway? (y/N): ").strip().lower()
            if response not in ("y", "yes"):
                print("  Migration cancelled.")
                return False, from_version, from_version
        else:
            return False, from_version, from_version

    # Apply migrations sequentially
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema_migrations_table(conn)

        current = from_version
        for target_ver in range(from_version + 1, CURRENT_VERSION + 1):
            if target_ver not in MIGRATIONS:
                if not silent:
                    print(f"  ERROR: No migration defined for v{target_ver}")
                return False, from_version, current

            description, migrate_fn = MIGRATIONS[target_ver]

            try:
                migrate_fn(conn)
                set_db_version(conn, target_ver)
                log_migration(conn, current, target_ver, description)
                current = target_ver
                if not silent:
                    print(f"    Applied migration {target_ver}: {description}")
            except Exception as e:
                if not silent:
                    print(f"  ERROR during migration {target_ver}: {e}")
                    print(f"  Database may be in a partial state.")
                    print(f"  Restore from backup: {backup_path.name}")
                return False, from_version, current

    finally:
        conn.close()

    if not silent:
        print(f"  Database upgraded successfully: v{from_version} -> v{current}")

    return True, from_version, current


# ============================================================
# CLI entry point
# ============================================================

def main():
    """Standalone migration tool.

    Usage: python db_migrate.py /path/to/theforge.db
    """
    if len(sys.argv) < 2:
        print("Usage: python db_migrate.py <path-to-theforge.db>")
        print()
        print("Detects the current schema version and applies any pending")
        print("migrations to bring the database up to date.")
        sys.exit(1)

    db_path = sys.argv[1]
    if not os.path.exists(db_path):
        print(f"ERROR: File not found: {db_path}")
        sys.exit(1)

    print(f"EQUIPA Database Migration Tool (target: v{CURRENT_VERSION})")
    print(f"Database: {db_path}")
    print()

    success, from_ver, to_ver = run_migrations(db_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

