"""EQUIPA database layer — connection, schema, and core record functions.

Layer 2: Imports only from equipa.constants. All other DB-dependent modules
import from this module instead of the monolith.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from equipa.constants import THEFORGE_DB

logger = logging.getLogger(__name__)


# --- Connection ---

def get_db_connection(write: bool = False) -> sqlite3.Connection:
    """Open a connection to TheForge database.

    Args:
        write: If True, open in read-write mode. Default is read-only.

    Note:
        Prefer :func:`db_conn` for a context-managed handle that uniformly
        commits on success, rolls back on error, and always closes — closing
        the QS-01 leak family that the bare ``connect()``/``close()`` pattern
        kept reintroducing.
    """
    if not THEFORGE_DB.exists():
        raise FileNotFoundError(f"TheForge database not found at {THEFORGE_DB}")

    if write:
        conn = sqlite3.connect(str(THEFORGE_DB))
    else:
        uri = f"file:{THEFORGE_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # Enforce the 29 FOREIGN KEY relationships declared in schema.sql. SQLite
    # defaults foreign_keys OFF per-connection, so without this every db.py-routed
    # write could insert a dangling reference (e.g. an agent_runs row whose
    # task_id/project_id points at no row) and schema.sql's FK constraints would
    # be silently unenforced. heartbeat.py:_connect already sets this; db.py
    # diverged (SR-2700 S1). It is connection-level, so it is applied to read-only
    # handles too — harmless there (no writes occur) and it keeps the two code
    # paths identical. Must be set outside any transaction, which is the case on a
    # freshly-opened connection.
    conn.execute("PRAGMA foreign_keys = ON")

    # Wait up to 5s for a competing lock to clear before raising
    # sqlite3.OperationalError, matching heartbeat.py:_connect. EQUIPA runs N
    # parallel agents writing telemetry to one shared SQLite file, so brief lock
    # contention is expected; busy_timeout lets a blocked writer retry silently
    # instead of failing the agent run on the first "database is locked". Applied
    # to read-only handles too — it is a no-op write (connection-level setting),
    # and it keeps a read that races a writer from erroring immediately.
    #
    # NOTE: journal_mode is deliberately left at SQLite's default (rollback
    # journal). WAL is intentionally NOT enabled here: theforge.db is accessed
    # cross-host over an SMB share (Windows Z: + Linux local path), and WAL
    # relies on shared-memory / mmap coordination that is unsafe on network
    # filesystems and can silently corrupt the DB across hosts. Any journal_mode
    # change is out of scope for this connection layer — do not add one.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_conn(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Context-managed TheForge DB connection.

    Replaces the bare ``conn = get_db_connection(...)`` / ``conn.close()``
    pattern with a single ``with`` block that:

      * always closes the connection (even on exception) — closes the QS-01
        connection-leak family that re-surfaced across 10+ security reviews;
      * commits on clean exit when ``write=True``;
      * rolls back on exception when ``write=True`` so partial writes never
        leak past the failure boundary.

    Read-only handles do not commit/rollback (the underlying connection is
    opened in URI ``mode=ro``), but they still get the guaranteed close.

    The connection is obtained from :func:`get_db_connection`, so every handle
    yielded here already has ``PRAGMA busy_timeout = 5000`` applied (read-only
    and write alike) — a blocked writer waits up to 5s for a competing lock to
    clear instead of failing immediately under EQUIPA's N-parallel-agent load.
    See ``get_db_connection`` for why WAL journal_mode is deliberately NOT
    enabled (SMB cross-host access makes WAL unsafe).

    Example:
        >>> with db_conn(write=True) as conn:
        ...     conn.execute("INSERT INTO t (x) VALUES (?)", (1,))

    Args:
        write: If True, open in read-write mode. Default is read-only.

    Yields:
        sqlite3.Connection: an open handle with ``row_factory = sqlite3.Row``.
    """
    conn = get_db_connection(write=write)
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("[DB] rollback failed during db_conn cleanup")
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            logger.exception("[DB] close failed during db_conn cleanup")


# --- Schema ---

_SCHEMA_ENSURED = False

# Canonical schema lives at the repo root.  This module is at
# <repo>/equipa/db.py, so the schema file is one directory up.
SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class SchemaNotInitialised(RuntimeError):
    """Raised when TheForge DB schema cannot be applied or located.

    Production callers should run ``db_migrate.py`` (or ``equipa_setup.py``)
    before importing modules that touch the DB.  This exception signals that
    the safety-net path failed and the orchestrator must not continue.
    """


def _make_schema_idempotent(schema_sql: str) -> str:
    """Rewrite CREATE statements in schema.sql to be idempotent.

    Mirrors the logic in tests/conftest.py:_ensure_full_schema so we can
    treat schema.sql as the single source of truth without duplicating
    table definitions inside this module.
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


def ensure_schema() -> None:
    """Apply the canonical ``schema.sql`` to TheForge DB if needed.

    Safety net: the primary schema management path is ``db_migrate.py``
    (for upgrades) and ``equipa_setup.py`` (for fresh installs).  This
    function exists so library callers can safely touch the DB even when
    those entry points have not run — but unlike the previous inline
    ``CREATE TABLE`` block it does not duplicate table definitions.

    Raises:
        SchemaNotInitialised: if ``schema.sql`` is missing or applying it
            fails.  Callers must NOT swallow this — a missing schema means
            downstream telemetry and lesson lookups will silently lose data.
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return

    if not SCHEMA_SQL_PATH.exists():
        raise SchemaNotInitialised(
            f"Canonical schema file not found at {SCHEMA_SQL_PATH}. "
            "Run equipa_setup.py or db_migrate.py before using the DB."
        )

    try:
        schema_sql = _make_schema_idempotent(SCHEMA_SQL_PATH.read_text())
    except OSError as e:
        raise SchemaNotInitialised(
            f"Could not read schema file at {SCHEMA_SQL_PATH}: {e}"
        ) from e

    # get_db_connection raises FileNotFoundError if the DB file does not
    # exist.  In production db_migrate.py / equipa_setup.py creates the
    # file first; in test worktrees the DB may start absent, so fall back
    # to sqlite3.connect (which auto-creates) before applying schema.sql.
    try:
        conn = get_db_connection(write=True)
    except FileNotFoundError:
        conn = sqlite3.connect(str(THEFORGE_DB))
        conn.row_factory = sqlite3.Row

    try:
        conn.executescript(schema_sql)
        conn.commit()
        _ensure_additive_columns(conn)
        conn.commit()
    except sqlite3.Error as e:
        logger.exception("[Schema] Failed to apply schema.sql: %s", e)
        raise SchemaNotInitialised(
            f"Failed to apply {SCHEMA_SQL_PATH.name}: {e}"
        ) from e
    finally:
        conn.close()

    _SCHEMA_ENSURED = True


def _ensure_additive_columns(conn) -> None:
    """Add nullable columns that post-date a table's CREATE in existing DBs.

    ``ensure_schema`` applies ``schema.sql`` with CREATE TABLE IF NOT EXISTS,
    which is a no-op for tables that already exist — so a column added to a
    table definition *after* that table was first created never reaches an
    existing database. SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS``, so we check ``PRAGMA table_info`` and add only what is missing.
    Idempotent, additive (nullable, no data change), and independent of the
    ``db_migrate`` version counter — safe to run on every startup.
    """
    additive = {
        # tasks.role lets a task carry its dispatch role so autonomous/scan
        # modes (and --task without --role) fan out to specialist/project roles.
        "tasks": [("role", "TEXT")],
    }
    for table, cols in additive.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# --- Record Functions ---

# Match the FILES_CHANGED footer the developer/code-reviewer/security-reviewer
# emit. Tolerant of leading whitespace and the trailing colon variants observed
# in production agent output. Anchored to a line start so prose mid-paragraph
# doesn't false-match (the "FILES_CHANGED is REQUIRED" instruction line in the
# developer prompt template, for example).
_FILES_CHANGED_BLOCK_RE = re.compile(
    r"^[ \t]*FILES[_ ]CHANGED[ \t]*:[ \t]*(.*?)(?=^[ \t]*[A-Z][A-Z_ ]{2,}[ \t]*:|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Lines we should ignore inside a FILES_CHANGED block. The developer template
# explicitly says "If you changed no files, write `FILES_CHANGED: none`" — that
# sentinel must NOT count as a real file. Bullets / dashes are valid list
# markers; we strip them before checking the sentinel.
_FILES_CHANGED_NONE_SENTINELS = {"none", "n/a", "(none)", "-", ""}


def _parse_files_changed_block(result_text: str) -> list[str]:
    """Extract the file list from the agent's FILES_CHANGED footer.

    Returns the list of distinct file paths claimed in the block. Used as a
    fallback for runs where ``files_changed_set`` was not attached to the
    result dict (older callers, synthesized results, retry shims).
    """
    if not result_text:
        return []
    match = _FILES_CHANGED_BLOCK_RE.search(result_text)
    if not match:
        return []
    body = match.group(1)
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip()
        # Strip common list markers ("- foo.py", "* foo.py", "1. foo.py").
        line = re.sub(r"^([\-\*•]|\d+\.)\s+", "", line)
        # Drop trailing inline comments and the orchestrator's status hints
        # like " (created)" / " (modified)" so the path key dedups cleanly.
        line = re.sub(r"\s+\((?:created|modified|new|updated|deleted)\)\s*$",
                      "", line, flags=re.IGNORECASE)
        if not line or line.startswith("#"):
            continue
        if line.lower() in _FILES_CHANGED_NONE_SENTINELS:
            return []
        if line in seen:
            continue
        seen.add(line)
        files.append(line)
    return files


# Upper bound for files_changed_count to defend the alerting/quality-scoring
# pipeline against absurd values (forged or accidentally inflated). Picked well
# above any plausible single-diff size in a monorepo.
_FILES_CHANGED_MAX = 10_000


def _clamp_files_changed(value: int, source: str) -> int:
    """Clamp a non-negative file count to ``_FILES_CHANGED_MAX``.

    Logs a warning when clamping occurs so downstream investigators can spot
    fabricated or runaway values. Negative inputs collapse to 0.
    """
    if value < 0:
        return 0
    if value > _FILES_CHANGED_MAX:
        try:
            from equipa.output import log
            log(
                f"[telemetry] files_changed_count from {source}={value} exceeds "
                f"max {_FILES_CHANGED_MAX}; clamping. Possible fabrication."
            )
        except Exception:
            pass
        return _FILES_CHANGED_MAX
    return value


def _resolve_files_changed_count(result: dict | None) -> int:
    """Determine how many files an agent run actually changed.

    Trust model (post-#2314 security re-review):
      1. Explicit ``result["files_changed_count"]`` — caller pre-computed it
         (e.g., from a git diff). Must be a real int (``bool`` rejected).
      2. ``result["files_changed_set"]`` — populated by ``agent_runner.run_agent``
         from observed Edit/Write/NotebookEdit tool-call inputs. AUTHORITATIVE
         when present, even when empty. Agent-emitted ``FILES_CHANGED:`` footers
         are never trusted as a fallback once this key is set, because a vacuous
         run could forge it. Callers MUST always populate this key.
      3. ``result["files_changed"]`` — legacy alias used by some loop callers.
      4. ``FILES_CHANGED:`` footer in ``result_text`` — last resort, agent
         self-report, lowest trust. Only used when neither structured key is
         present.

    Missing ``files_changed_set`` is treated as a callsite bug, logged at WARN,
    and the function returns 0 rather than trusting the agent footer. All
    branches clamp to ``_FILES_CHANGED_MAX``.
    """
    if not isinstance(result, dict):
        return 0

    # Phase B: reject bool — isinstance(True, int) is True in Python and would
    # otherwise silently coerce True/False into 1/0 in the column.
    explicit = result.get("files_changed_count")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0:
        return _clamp_files_changed(explicit, "explicit")

    # Phase A: files_changed_set is authoritative when present. Do NOT fall
    # through to the agent-controlled footer parse, even when the set is empty.
    if "files_changed_set" in result:
        value = result["files_changed_set"]
        if isinstance(value, (list, tuple, set)):
            return _clamp_files_changed(len(value), "files_changed_set")
        # Key present but malformed — log and treat as 0 (do NOT trust footer).
        try:
            from equipa.output import log
            log(
                "[telemetry] files_changed_set present but not a "
                f"list/tuple/set (got {type(value).__name__}); treating as 0."
            )
        except Exception:
            pass
        return 0

    # files_changed_set absent → callsite bug. Warn and return 0; do NOT
    # silently fall through to the agent footer.
    try:
        from equipa.output import log
        log(
            "[telemetry] WARN: result missing 'files_changed_set' in "
            "_resolve_files_changed_count; agent_runner.run_agent must set "
            "this on every exit path. Returning 0 (footer parse skipped)."
        )
    except Exception:
        pass

    # Legacy alias still trusted (set by trusted loop callers, not the agent).
    legacy = result.get("files_changed")
    if isinstance(legacy, (list, tuple, set)):
        return _clamp_files_changed(len(legacy), "files_changed")

    return 0


def record_agent_run(
    task: dict | int,
    result: dict | None,
    outcome: str,
    role: str = "developer",
    model: str = "opus",
    max_turns: int = 25,
    cycle_number: int = 1,
    continuation_count: int = 0,
    output: list | None = None,
    prompt_version: str | None = None,
) -> None:
    """Record agent execution telemetry to TheForge agent_runs table.

    Never crashes the orchestrator — all errors are logged and swallowed.
    Reads turns_allocated from result dict if available (set by dynamic budget system).
    prompt_version: which prompt version was used (e.g., "baseline", "v2").
        If None, reads from _last_prompt_version[role].
    """
    try:
        from equipa.output import log
        from equipa.tasks import get_task_complexity

        # Late import to avoid circular dependency
        from equipa.prompts import _last_prompt_version

        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None
        complexity = get_task_complexity(task) if isinstance(task, dict) else None
        success = 1 if outcome in ("tests_passed", "no_tests") else 0
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0
        duration = result.get("duration", 0) if isinstance(result, dict) else 0
        cost = result.get("cost") if isinstance(result, dict) else None
        errors = result.get("errors", []) if isinstance(result, dict) else []
        turns_allocated = result.get("turns_allocated") if isinstance(result, dict) else None
        error_type = None
        error_summary = None

        # Resolve prompt version from A/B testing tracker if not explicitly passed
        if prompt_version is None:
            prompt_version = _last_prompt_version.get(role, "baseline")

        # Early termination gets priority for error_type/error_summary
        if isinstance(result, dict) and result.get("early_terminated"):
            error_type = "early_terminated"
            error_summary = result.get("early_term_reason", "early termination")[:500]
        elif errors:
            error_summary = errors[0][:500] if errors[0] else None
            if "timed out" in (error_summary or "").lower():
                error_type = "timeout"
            elif "max_turns" in (error_summary or "").lower():
                error_type = "max_turns"
            elif "loop detected" in (error_summary or "").lower():
                error_type = "loop_detected"
            else:
                error_type = "agent_error"
        files_changed = _resolve_files_changed_count(result)

        with db_conn(write=True) as conn:
            conn.execute(
                """INSERT INTO agent_runs
                   (task_id, project_id, role, model, complexity, num_turns,
                    max_turns_allowed, duration_seconds, cost_usd, outcome,
                    success, cycle_number, continuation_count, files_changed_count,
                    error_type, error_summary, turns_allocated, prompt_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, project_id, role, model, complexity, num_turns,
                 max_turns, duration, cost, outcome,
                 success, cycle_number, continuation_count, files_changed,
                 error_type, error_summary, turns_allocated, prompt_version),
            )
        budget_info = f", allocated={turns_allocated}" if turns_allocated else ""
        version_info = f", prompt={prompt_version}" if prompt_version != "baseline" else ""
        log(f"  [Telemetry] Recorded agent run: role={role}, outcome={outcome}, "
            f"turns={num_turns}/{max_turns}{budget_info}{version_info}, "
            f"duration={duration:.0f}s", output)
    except Exception:
        # Telemetry must NEVER crash the orchestrator. Catch broadly here, but
        # use logger.exception() so ops can grep for [Telemetry] tracebacks.
        logger.exception("[Telemetry] Failed to record agent run")


def _get_latest_agent_run_id(task_id: int) -> int | None:
    """Get the most recently inserted agent_run ID for a task.

    Returns the ID or None if not found. Never raises.
    """
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return row["id"] if row else None
    except sqlite3.Error:
        logger.exception("[Telemetry] Failed to look up latest agent run id")
        return None


# --- Action Logging ---

def classify_error(error_text: str) -> str:
    """Classify an error string into a category for the error_type field.

    Returns one of: timeout, file_not_found, permission, syntax_error,
    import_error, test_failure, unknown.
    """
    if not error_text:
        return "unknown"
    lower = error_text.lower()
    if "timed out" in lower:
        return "timeout"
    if "no such file" in lower or "not found" in lower:
        return "file_not_found"
    if "permission denied" in lower:
        return "permission"
    if "syntaxerror" in lower:
        return "syntax_error"
    if "modulenotfounderror" in lower or "importerror" in lower:
        return "import_error"
    if "failed" in lower or "assertionerror" in lower:
        return "test_failure"
    return "unknown"


def log_agent_action(
    task_id: int,
    run_id: int | None,
    cycle: int,
    role: str,
    turn: int,
    tool_name: str,
    tool_input_preview: str | None,
    input_hash: str | None,
    output_length: int | None,
    success: bool,
    error_type: str | None,
    error_summary: str | None,
    duration_ms: int | None,
) -> None:
    """Insert a single agent action record into the database.

    Never crashes the orchestrator — all errors are logged and swallowed.
    """
    try:
        with db_conn(write=True) as conn:
            conn.execute(
                """INSERT INTO agent_actions
                   (task_id, run_id, cycle_number, role, turn_number, tool_name,
                    tool_input_preview, input_hash, output_length, success,
                    error_type, error_summary, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, run_id, cycle, role, turn, tool_name,
                 tool_input_preview, input_hash, output_length,
                 1 if success else 0, error_type, error_summary, duration_ms),
            )
    except Exception:
        # Telemetry must NEVER crash the orchestrator (broad except is intentional
        # here). Previously this was a silent `pass` — now ops can grep
        # [Telemetry] in logs for swallowed errors.
        logger.exception("[Telemetry] Failed to log agent action")


def log_gate_audit(
    message: str,
    task_id: int | None,
    event: str | None = None,
    counts: dict | None = None,
) -> None:
    """Best-effort persist of a security-gate audit event to ``agent_actions``.

    Companion to the stderr ``[GATE-AUDIT]`` emit in
    :func:`equipa.security_gate._gate_audit_log` (task #2702). The stderr line
    remains the primary, synchronous record; this adds a durable row so a
    post-hoc audit of "why did this branch merge or block" survives log
    rotation / a lost nohup file.

    FAIL-OPEN (task #2702): any DB error — locked file, missing table, bad
    schema — is logged and swallowed. This function must NEVER raise into the
    merge/gate path. The gate DECISION itself stays fail-closed in
    ``dispatch._gated_merge_task``; only this audit persistence is
    non-blocking.

    ``agent_actions`` has no dedicated ``action_type``/``message`` column, so
    the gate event is mapped onto the existing columns:

      * ``tool_name``          = ``"gate-audit"``  (the action-type discriminator)
      * ``tool_input_preview`` = the full audit ``message`` text
      * ``role``               = ``"security-gate"``
      * ``error_summary``      = ``event`` tag when supplied (queryable filter)
      * ``cycle_number`` / ``turn_number`` = 0 (a gate event is not tied to a
        dev-test cycle or agent turn)

    When ``task_id`` is ``None`` (not supplied by the caller / not parseable)
    the DB row is skipped, because ``agent_actions.task_id`` is ``NOT NULL``;
    the stderr line already carries the event text in that case.

    Args:
        message: the full audit line text (stored verbatim for the audit trail).
        task_id: the task the gate event belongs to; ``None`` skips the write.
        event: short event tag (e.g. ``"merge-succeeded"``) stored as a
            queryable filter in ``error_summary``. Optional.
        counts: finding counts dict; currently informational only (the message
            text already renders them). Accepted so callers can pass structured
            data without stringifying it themselves.
    """
    if task_id is None:
        return
    try:
        ensure_schema()
        # error_summary is a first-200-chars column in the schema; keep the
        # event tag well within that so we never rely on silent truncation.
        event_tag = event[:200] if event else None
        with db_conn(write=True) as conn:
            conn.execute(
                """INSERT INTO agent_actions
                   (task_id, run_id, cycle_number, role, turn_number, tool_name,
                    tool_input_preview, input_hash, output_length, success,
                    error_type, error_summary, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, None, 0, "security-gate", 0, "gate-audit",
                 message, None, None, 1,
                 None, event_tag, None),
            )
    except Exception:
        # AUDIT persistence is best-effort fail-open (task #2702): a locked DB,
        # missing table, or schema error must never change gate behavior or
        # raise into the merge path. Broad except is intentional; log so ops
        # can grep [GATE-AUDIT] for swallowed persistence failures.
        logger.exception(
            "[GATE-AUDIT] Failed to persist gate audit for task %s", task_id
        )


def bulk_log_agent_actions(
    action_log: list[dict],
    task_id: int,
    run_id: int | None,
    cycle: int,
    role: str,
) -> None:
    """Bulk insert all actions from an action_log list into the database.

    More efficient than individual inserts — uses a single transaction.
    Never crashes the orchestrator.
    """
    if not action_log:
        return
    try:
        ensure_schema()
        with db_conn(write=True) as conn:
            conn.executemany(
                """INSERT INTO agent_actions
                   (task_id, run_id, cycle_number, role, turn_number, tool_name,
                    tool_input_preview, input_hash, output_length, success,
                    error_type, error_summary, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (task_id, run_id, cycle, role, a["turn"], a["tool"],
                     a.get("input_preview"), a.get("input_hash"),
                     a.get("output_length"), 1 if a.get("success", True) else 0,
                     a.get("error_type"), a.get("error_summary"),
                     a.get("duration_ms"))
                    for a in action_log
                ],
            )
    except Exception:
        # Telemetry must NEVER crash the orchestrator.
        logger.exception("[Telemetry] Failed to bulk insert agent actions")


# --- Task Status ---

def update_task_status(
    task_id: int,
    outcome: str,
    output: list | None = None,
) -> None:
    """Update task status in TheForge based on dev-test outcome.

    Called by the orchestrator after run_dev_test_loop completes, so agents
    don't need to handle DB updates themselves (they often run out of turns).

    Maps outcomes to statuses:
        tests_passed, no_tests -> done
        Everything else (blocked, failed, timeout, no_progress) -> blocked
    """
    from equipa.output import log

    success_outcomes = ("tests_passed", "no_tests", "early_completed_no_changes")
    new_status = "done" if outcome in success_outcomes else "blocked"

    try:
        with db_conn(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                log(f"  [DB] Task {task_id} not found — skipping status update.", output)
                return
            current = row["status"]

            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE completed_at END WHERE id = ?",
                (new_status, new_status, task_id),
            )
        log(f"  [DB] Task {task_id}: {current} -> {new_status} (outcome: {outcome})", output)
    except sqlite3.Error as e:
        # Real logic path (not telemetry) — narrow to sqlite errors only so
        # programmer bugs (KeyError, AttributeError) still surface as crashes.
        log(f"  [DB] ERROR updating task {task_id}: {e}", output)
        logger.exception("[DB] Failed to update task %s status", task_id)
