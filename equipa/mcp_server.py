"""EQUIPA MCP Server — JSON-RPC 2.0 over stdio.

Model Context Protocol server exposing EQUIPA orchestrator via MCP tools.
Uses ONLY Python stdlib (json, sys, subprocess) — no external dependencies.

Implements:
- JSON-RPC 2.0 protocol over stdio
- MCP initialization handshake
- 8 tools: dispatch, task_status, task_create, lessons, agent_logs, project_context, session_notes, session_note_add

Hardening (SECURITY-REVIEW-1728, task 2452):
- MCP-01: equipa_dispatch requires EQUIPA_MCP_TOKEN auth, is rate-limited
  (token bucket), gated by a 24-hour cost cap, and rejects unknown roles/models.
- MCP-02: equipa_task_create validates project existence + status, caps
  description size, is rate-limited, and honours EQUIPA_MCP_PROJECT_IDS allowlist.
- MCP-03: All query handlers clamp caller-supplied ``limit`` to MAX_QUERY_LIMIT.
- MCP-04: equipa_session_note_add requires auth, validates project existence +
  status, is rate-limited, and sanitizes summary/next_steps/key_points through
  lesson_sanitizer.sanitize_session_note (injection-strip + generous length
  policy, no silent truncation) so session notes cannot be written unsanitized
  through the semantic write path.

Stderr is used for logging only. Stdout is reserved for JSON-RPC messages.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Import constants and DB helper
try:
    from equipa.constants import (
        DEFAULT_ROLE_MODELS,
        DEFAULT_ROLE_TURNS,
        ROLE_PROMPTS,
        THEFORGE_DB,
    )
    from equipa.tasks import fetch_project_context
except ImportError:
    # Fallback if running as standalone
    THEFORGE_DB = Path(__file__).parent.parent / "theforge.db"
    DEFAULT_ROLE_MODELS = {}
    DEFAULT_ROLE_TURNS = {}
    ROLE_PROMPTS = {}

    def fetch_project_context(project_id: int) -> dict:
        """Minimal fallback for fetch_project_context."""
        return {"error": "fetch_project_context not available in standalone mode"}


# --- Hardening limits / allowlists ---

# Maximum rows any query tool will return, no matter what the client asks for.
# Clamping is logged so callers can see when their requested limit was reduced.
MAX_QUERY_LIMIT = 500

# Maximum bytes accepted in equipa_task_create description payload (UTF-8 encoded).
MAX_DESCRIPTION_BYTES = 32 * 1024  # 32 KB

# Token-bucket rate limits (refills continuously up to capacity).
DISPATCH_RATE_CAPACITY = 10          # 10 dispatches
DISPATCH_RATE_REFILL_SECONDS = 3600  # ...per hour per token
TASK_CREATE_RATE_CAPACITY = 100
TASK_CREATE_RATE_REFILL_SECONDS = 3600
SESSION_NOTE_RATE_CAPACITY = 100
SESSION_NOTE_RATE_REFILL_SECONDS = 3600

# Project statuses allowed as task_create targets.
ALLOWED_PROJECT_STATUSES = {"active", "planning"}

# Role + model allowlists for equipa_dispatch. Built from orchestrator constants
# where possible; the fallbacks ensure the MCP server still refuses unknown
# values even if constants import failed (standalone mode).
ALLOWED_ROLES = (
    set(ROLE_PROMPTS.keys())
    | set(DEFAULT_ROLE_TURNS.keys())
    | set(DEFAULT_ROLE_MODELS.keys())
    or {"developer", "tester", "security-reviewer", "planner", "evaluator",
        "code-reviewer", "debugger", "frontend-designer", "integration-tester",
        "qa-tester"}
)
ALLOWED_MODELS = {"opus", "sonnet", "haiku"}


def _log(msg: str) -> None:
    """Log to stderr only — never corrupt stdout."""
    print(f"[MCP] {msg}", file=sys.stderr, flush=True)


def _send_response(response: dict) -> None:
    """Send JSON-RPC response to stdout."""
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _send_error(request_id: int | str | None, code: int, message: str) -> None:
    """Send JSON-RPC error response."""
    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        }
    })


@contextmanager
def _db_conn(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Context-managed connection to TheForge DB.

    Replaces the bare connect/close pairs with a single ``with`` block that
    always closes the handle, commits on clean exit when ``write=True`` and
    rolls back on exception. Closes the QS-01 leak family in this module.
    """
    db_path = THEFORGE_DB
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# --- Auth + rate limiting ---


class _TokenBucket:
    """Simple in-memory token bucket.

    Refills at ``capacity / refill_seconds`` tokens per second up to ``capacity``.
    ``try_consume`` returns (allowed, retry_after_seconds). Buckets are keyed by
    caller token so two tokens cannot starve each other.

    State is process-local. The EQUIPA MCP server is a single stdio process per
    client, so this is sufficient; a multi-tenant deployment would need a
    shared store (Redis, etc.).
    """

    def __init__(self, capacity: int, refill_seconds: float) -> None:
        self.capacity = float(capacity)
        self.refill_rate = float(capacity) / float(refill_seconds)
        self._buckets: dict[str, tuple[float, float]] = {}

    def _refill(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (self.capacity, now))
        elapsed = max(0.0, now - last)
        tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
        self._buckets[key] = (tokens, now)
        return tokens

    def try_consume(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        tokens = self._refill(key, now)
        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return True, 0.0
        deficit = cost - tokens
        retry_after = deficit / self.refill_rate if self.refill_rate > 0 else float("inf")
        return False, retry_after


_DISPATCH_BUCKET = _TokenBucket(DISPATCH_RATE_CAPACITY, DISPATCH_RATE_REFILL_SECONDS)
_TASK_CREATE_BUCKET = _TokenBucket(TASK_CREATE_RATE_CAPACITY, TASK_CREATE_RATE_REFILL_SECONDS)
_SESSION_NOTE_BUCKET = _TokenBucket(SESSION_NOTE_RATE_CAPACITY, SESSION_NOTE_RATE_REFILL_SECONDS)


def _expected_token() -> str | None:
    """Return the configured server token, or None if auth is unconfigured."""
    token = os.environ.get("EQUIPA_MCP_TOKEN")
    if not token:
        return None
    return token


def _check_auth(args: dict) -> tuple[bool, dict | None]:
    """Validate the caller-supplied auth_token against EQUIPA_MCP_TOKEN.

    Returns (ok, error_payload). Fails closed if the server has no token
    configured — operators must explicitly set EQUIPA_MCP_TOKEN to enable
    privileged tool calls. ``auth_token`` is removed from ``args`` on success
    so individual handlers never see it.
    """
    expected = _expected_token()
    if expected is None:
        return False, {
            "error": "EQUIPA_MCP_TOKEN not configured on server; refusing call",
            "auth": "unconfigured",
        }
    supplied = args.pop("auth_token", None)
    if not supplied or supplied != expected:
        return False, {
            "error": "Invalid or missing auth_token",
            "auth": "rejected",
        }
    return True, None


def _clamp_limit(requested: Any, tool: str) -> int:
    """Clamp caller-supplied ``limit`` to [1, MAX_QUERY_LIMIT].

    Non-integer or non-positive inputs fall back to MAX_QUERY_LIMIT. Logs when
    clamping reduces the requested value so operators can see overlarge probes.
    """
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = MAX_QUERY_LIMIT
    if value <= 0:
        value = MAX_QUERY_LIMIT
    if value > MAX_QUERY_LIMIT:
        _log(f"{tool}: clamping limit {requested} -> {MAX_QUERY_LIMIT}")
        value = MAX_QUERY_LIMIT
    return value


def _project_id_allowlist() -> set[int] | None:
    """Parse EQUIPA_MCP_PROJECT_IDS into a set of allowed project IDs.

    Empty / unset means "no restriction beyond status check". Malformed entries
    are skipped with a warning rather than failing closed, because the
    allowlist is an optional second layer on top of the existence+status check.
    """
    raw = os.environ.get("EQUIPA_MCP_PROJECT_IDS", "").strip()
    if not raw:
        return None
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            _log(f"EQUIPA_MCP_PROJECT_IDS: ignoring malformed entry {part!r}")
    return ids or None


def _recent_dispatch_cost_usd(window_seconds: int = 86400) -> float:
    """Sum cost_usd across agent_runs in the last ``window_seconds``.

    Returns 0.0 if the table or column is unavailable so that an unprovisioned
    deployment is not falsely cost-capped.
    """
    try:
        with _db_conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "cost_usd" not in cols:
                return 0.0
            if "created_at" in cols:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) AS s "
                    "FROM agent_runs WHERE created_at >= datetime('now', ?)",
                    (f"-{window_seconds} seconds",),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM agent_runs"
                ).fetchone()
            return float(row["s"] or 0.0)
    except (sqlite3.Error, FileNotFoundError) as exc:
        _log(f"cost-cap query failed: {exc}; defaulting to 0.0")
        return 0.0


def _dispatch_cost_cap_usd() -> float | None:
    """Parse EQUIPA_MCP_COST_CAP_USD. Return None to disable the cap."""
    raw = os.environ.get("EQUIPA_MCP_COST_CAP_USD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _log(f"EQUIPA_MCP_COST_CAP_USD: malformed value {raw!r}; cap disabled")
        return None
    return value if value > 0 else None


# --- MCP Tool Handlers ---

def _handle_equipa_dispatch(args: dict) -> dict:
    """Spawn orchestrator subprocess for a task.

    Args:
        auth_token (str): Required. Must match EQUIPA_MCP_TOKEN env var.
        task_id (int): Task ID to dispatch
        role (str, optional): Agent role (default: developer; must be in allowlist)
        max_turns (int, optional): Max turns
        model (str, optional): Model override (must be in allowlist)

    Returns:
        dict: {"status": "spawned", "pid": int} on success, otherwise {"error": ...}.
    """
    ok, err = _check_auth(args)
    if not ok:
        return err

    token = _expected_token() or "anonymous"
    allowed, retry_after = _DISPATCH_BUCKET.try_consume(token)
    if not allowed:
        return {
            "error": "Rate limit exceeded for equipa_dispatch",
            "retry_after_seconds": round(retry_after, 2),
            "limit": f"{DISPATCH_RATE_CAPACITY}/{DISPATCH_RATE_REFILL_SECONDS}s",
        }

    cost_cap = _dispatch_cost_cap_usd()
    if cost_cap is not None:
        spent = _recent_dispatch_cost_usd()
        if spent >= cost_cap:
            return {
                "error": "Daily dispatch cost cap reached; refusing dispatch",
                "spent_usd": round(spent, 4),
                "cap_usd": cost_cap,
            }

    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id required"}
    if not isinstance(task_id, int) or task_id <= 0:
        return {"error": "task_id must be a positive integer"}

    role = args.get("role", "developer")
    if not isinstance(role, str) or role not in ALLOWED_ROLES:
        return {
            "error": f"role {role!r} not in allowlist",
            "allowed_roles": sorted(ALLOWED_ROLES),
        }

    model = args.get("model")
    if model is not None and (not isinstance(model, str) or model not in ALLOWED_MODELS):
        return {
            "error": f"model {model!r} not in allowlist",
            "allowed_models": sorted(ALLOWED_MODELS),
        }

    max_turns = args.get("max_turns")
    if max_turns is not None:
        if not isinstance(max_turns, int) or max_turns <= 0 or max_turns > 500:
            return {"error": "max_turns must be a positive integer <= 500"}

    # Build command
    cmd = [sys.executable, "-m", "equipa.cli", "--task", str(task_id), "--role", role, "--yes"]
    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])
    if model:
        cmd.extend(["--model", model])

    _log(f"Spawning: {' '.join(cmd)}")

    # Spawn detached subprocess
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    return {
        "status": "spawned",
        "pid": proc.pid,
        "task_id": task_id,
        "role": role,
    }


def _handle_equipa_task_status(args: dict) -> dict:
    """Query task status from DB.

    Args:
        task_id (int): Task ID

    Returns:
        dict: Task record including status, title, description, etc.
    """
    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id required"}

    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT t.*, p.name as project_name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()

        if not row:
            return {"error": f"Task {task_id} not found"}

        return dict(row)


def _handle_equipa_task_create(args: dict) -> dict:
    """Create a new task in TheForge.

    Args:
        auth_token (str): Required. Must match EQUIPA_MCP_TOKEN env var.
        project_id (int): Project ID — must exist and be {active, planning}.
        title (str): Task title
        description (str): Task description (<= MAX_DESCRIPTION_BYTES)
        priority (str, optional): Task priority (default: medium)

    Returns:
        dict: {"task_id": int, "status": "created"} or {"error": ...}.
    """
    ok, err = _check_auth(args)
    if not ok:
        return err

    token = _expected_token() or "anonymous"
    allowed, retry_after = _TASK_CREATE_BUCKET.try_consume(token)
    if not allowed:
        return {
            "error": "Rate limit exceeded for equipa_task_create",
            "retry_after_seconds": round(retry_after, 2),
            "limit": f"{TASK_CREATE_RATE_CAPACITY}/{TASK_CREATE_RATE_REFILL_SECONDS}s",
        }

    project_id = args.get("project_id")
    title = args.get("title")
    description = args.get("description", "")
    priority = args.get("priority", "medium")

    if not project_id or not title:
        return {"error": "project_id and title required"}
    if not isinstance(project_id, int) or project_id <= 0:
        return {"error": "project_id must be a positive integer"}

    allowlist = _project_id_allowlist()
    if allowlist is not None and project_id not in allowlist:
        return {
            "error": f"project_id {project_id} not in EQUIPA_MCP_PROJECT_IDS allowlist",
        }

    if not isinstance(description, str):
        return {"error": "description must be a string"}
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        return {
            "error": f"description exceeds {MAX_DESCRIPTION_BYTES}-byte cap",
        }

    with _db_conn(write=True) as conn:
        proj = conn.execute(
            "SELECT id, status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if proj is None:
            return {"error": f"project_id {project_id} does not exist"}
        status = (proj["status"] or "").lower()
        if status not in ALLOWED_PROJECT_STATUSES:
            return {
                "error": (
                    f"project_id {project_id} has status {status!r}; "
                    f"expected one of {sorted(ALLOWED_PROJECT_STATUSES)}"
                ),
            }

        cursor = conn.execute(
            """
            INSERT INTO tasks (project_id, title, description, priority, status)
            VALUES (?, ?, ?, ?, 'todo')
            """,
            (project_id, title, description, priority),
        )
        task_id = cursor.lastrowid

        return {
            "task_id": task_id,
            "status": "created",
            "project_id": project_id,
            "title": title,
        }


def _handle_equipa_lessons(args: dict) -> dict:
    """Query lessons_learned table.

    Args:
        limit (int, optional): Max lessons to return (default: 20, clamped to MAX_QUERY_LIMIT)
        error_type (str, optional): Filter by error type

    Returns:
        dict: {"lessons": [{"lesson": str, "error_type": str, ...}, ...]}
    """
    limit = _clamp_limit(args.get("limit", 20), "equipa_lessons")
    error_type = args.get("error_type")

    with _db_conn() as conn:
        if error_type:
            rows = conn.execute(
                """
                SELECT lesson, error_type, error_signature, times_seen, created_at
                FROM lessons_learned
                WHERE error_type LIKE ?
                ORDER BY times_seen DESC, created_at DESC
                LIMIT ?
                """,
                (f"%{error_type}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT lesson, error_type, error_signature, times_seen, created_at
                FROM lessons_learned
                ORDER BY times_seen DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "lessons": [dict(r) for r in rows],
            "count": len(rows),
            "limit": limit,
        }


def _handle_equipa_agent_logs(args: dict) -> dict:
    """Query agent_runs table.

    Args:
        task_id (int, optional): Filter by task ID
        limit (int, optional): Max runs to return (default: 10, clamped to MAX_QUERY_LIMIT)

    Returns:
        dict: {"runs": [{"task_id": int, "role": str, "outcome": str, ...}, ...]}
    """
    task_id = args.get("task_id")
    limit = _clamp_limit(args.get("limit", 10), "equipa_agent_logs")

    with _db_conn() as conn:
        # Detect available columns — schema may have duration_s (db_migrate)
        # or duration_seconds (newer schema), and created_at may be absent.
        col_info = conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        if not col_info:
            return {"runs": [], "count": 0, "limit": limit}
        col_names = {row["name"] for row in col_info}

        duration_col = "duration_seconds" if "duration_seconds" in col_names else "duration_s"
        has_created_at = "created_at" in col_names
        order_col = "created_at" if has_created_at else "id"

        select_cols = f"task_id, role, outcome, {duration_col}"
        if has_created_at:
            select_cols += ", created_at"

        if task_id:
            rows = conn.execute(
                f"SELECT {select_cols} FROM agent_runs "
                f"WHERE task_id = ? ORDER BY {order_col} DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {select_cols} FROM agent_runs "
                f"ORDER BY {order_col} DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return {
            "runs": [dict(r) for r in rows],
            "count": len(rows),
            "limit": limit,
        }


def _handle_equipa_project_context(args: dict) -> dict:
    """Fetch project context using tasks.fetch_project_context.

    Args:
        project_id (int): Project ID

    Returns:
        dict: Project context (session notes, open questions, decisions)
    """
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id required"}

    try:
        context = fetch_project_context(project_id)
        return context
    except Exception as exc:
        return {"error": f"Failed to fetch context: {exc}"}


def _handle_equipa_session_notes(args: dict) -> dict:
    """Query session_notes table.

    Args:
        project_id (int, optional): Filter by project ID
        limit (int, optional): Max notes to return (default: 5, clamped to MAX_QUERY_LIMIT)

    Returns:
        dict: {"notes": [{"project_id": int, "summary": str, ...}, ...]}
    """
    project_id = args.get("project_id")
    limit = _clamp_limit(args.get("limit", 5), "equipa_session_notes")

    with _db_conn() as conn:
        if project_id:
            rows = conn.execute(
                """
                SELECT project_id, summary, next_steps, session_date
                FROM session_notes
                WHERE project_id = ?
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT project_id, summary, next_steps, session_date
                FROM session_notes
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "notes": [dict(r) for r in rows],
            "count": len(rows),
            "limit": limit,
        }


def _handle_equipa_session_note_add(args: dict) -> dict:
    """Write a session note to TheForge, sanitizing narrative fields.

    The *write* counterpart to equipa_session_notes. It exists so a session note
    can be persisted through the sanctioned MCP interface with the mandatory
    sanitization applied *at the boundary*, rather than depending on the caller
    (e.g. the forge-end skill) to remember to run lesson_sanitizer before a raw
    write. summary/next_steps/key_points are passed through sanitize_session_note
    (injection-strip + generous length policy, no silent truncation) before
    insertion.

    Args:
        auth_token (str): Required. Must match EQUIPA_MCP_TOKEN env var.
        project_id (int): Project ID — must exist and be {active, planning}.
        summary (str): Required. What happened this session.
        next_steps (str, optional): What to do next.
        key_points (str, optional): Key points / highlights.

    Returns:
        dict: {"session_note_id": int, "status": "created", ...} or {"error": ...}.
    """
    ok, err = _check_auth(args)
    if not ok:
        return err

    token = _expected_token() or "anonymous"
    allowed, retry_after = _SESSION_NOTE_BUCKET.try_consume(token)
    if not allowed:
        return {
            "error": "Rate limit exceeded for equipa_session_note_add",
            "retry_after_seconds": round(retry_after, 2),
            "limit": f"{SESSION_NOTE_RATE_CAPACITY}/{SESSION_NOTE_RATE_REFILL_SECONDS}s",
        }

    project_id = args.get("project_id")
    if not isinstance(project_id, int) or project_id <= 0:
        return {"error": "project_id must be a positive integer"}

    allowlist = _project_id_allowlist()
    if allowlist is not None and project_id not in allowlist:
        return {
            "error": f"project_id {project_id} not in EQUIPA_MCP_PROJECT_IDS allowlist",
        }

    # Sanitize narrative fields at the boundary. Imported lazily (repo-root
    # module) so an import failure degrades to a length-capping fallback rather
    # than taking the whole server down.
    try:
        from lesson_sanitizer import sanitize_session_note
    except Exception:  # pragma: no cover - fallback if sanitizer unavailable
        def sanitize_session_note(text: Any) -> str:
            return ("" if not text else str(text))[:50_000]

    summary = sanitize_session_note(args.get("summary", ""))
    next_steps = sanitize_session_note(args.get("next_steps", ""))
    key_points = sanitize_session_note(args.get("key_points", ""))

    if not summary:
        return {"error": "summary is required (non-empty after sanitization)"}

    with _db_conn(write=True) as conn:
        proj = conn.execute(
            "SELECT id, status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if proj is None:
            return {"error": f"project_id {project_id} does not exist"}
        status = (proj["status"] or "").lower()
        if status not in ALLOWED_PROJECT_STATUSES:
            return {
                "error": (
                    f"project_id {project_id} has status {status!r}; "
                    f"expected one of {sorted(ALLOWED_PROJECT_STATUSES)}"
                ),
            }

        cursor = conn.execute(
            """
            INSERT INTO session_notes
                (project_id, summary, key_points, next_steps, session_date)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (project_id, summary, key_points, next_steps),
        )
        note_id = cursor.lastrowid

        return {
            "session_note_id": note_id,
            "status": "created",
            "project_id": project_id,
            "summary_chars": len(summary),
        }


# --- Tool Registry ---

TOOLS = {
    "equipa_dispatch": {
        "description": "Spawn EQUIPA orchestrator subprocess for a task (requires auth_token)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "auth_token": {"type": "string", "description": "Server token (matches EQUIPA_MCP_TOKEN)"},
                "task_id": {"type": "integer", "description": "Task ID to dispatch"},
                "role": {"type": "string", "description": "Agent role (default: developer)", "default": "developer"},
                "max_turns": {"type": "integer", "description": "Max turns"},
                "model": {"type": "string", "description": "Model override (opus/sonnet/haiku)"},
            },
            "required": ["auth_token", "task_id"],
        },
        "handler": _handle_equipa_dispatch,
    },
    "equipa_task_status": {
        "description": "Query task status from TheForge DB",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
        "handler": _handle_equipa_task_status,
    },
    "equipa_task_create": {
        "description": "Create a new task in TheForge (requires auth_token)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "auth_token": {"type": "string", "description": "Server token (matches EQUIPA_MCP_TOKEN)"},
                "project_id": {"type": "integer", "description": "Project ID (must exist, status active/planning)"},
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": f"Task description (max {MAX_DESCRIPTION_BYTES} bytes)"},
                "priority": {"type": "string", "description": "Priority (default: medium)", "default": "medium"},
            },
            "required": ["auth_token", "project_id", "title"],
        },
        "handler": _handle_equipa_task_create,
    },
    "equipa_lessons": {
        "description": "Query lessons_learned table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"Max lessons (default 20, clamped to {MAX_QUERY_LIMIT})", "default": 20},
                "error_type": {"type": "string", "description": "Filter by error type"},
            },
        },
        "handler": _handle_equipa_lessons,
    },
    "equipa_agent_logs": {
        "description": "Query agent_runs table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Filter by task ID"},
                "limit": {"type": "integer", "description": f"Max runs (default 10, clamped to {MAX_QUERY_LIMIT})", "default": 10},
            },
        },
        "handler": _handle_equipa_agent_logs,
    },
    "equipa_project_context": {
        "description": "Fetch project context (session notes, open questions, decisions)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
        "handler": _handle_equipa_project_context,
    },
    "equipa_session_notes": {
        "description": "Query session_notes table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Filter by project ID"},
                "limit": {"type": "integer", "description": f"Max notes (default 5, clamped to {MAX_QUERY_LIMIT})", "default": 5},
            },
        },
        "handler": _handle_equipa_session_notes,
    },
    "equipa_session_note_add": {
        "description": "Write a session note to TheForge with mandatory sanitization (requires auth_token)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "auth_token": {"type": "string", "description": "Server token (matches EQUIPA_MCP_TOKEN)"},
                "project_id": {"type": "integer", "description": "Project ID (must exist, status active/planning)"},
                "summary": {"type": "string", "description": "What happened this session (sanitized; up to ~50k chars)"},
                "next_steps": {"type": "string", "description": "What to do next (sanitized)"},
                "key_points": {"type": "string", "description": "Key points / highlights (sanitized)"},
            },
            "required": ["auth_token", "project_id", "summary"],
        },
        "handler": _handle_equipa_session_note_add,
    },
}


# --- JSON-RPC Handlers ---

def _handle_initialize(params: dict, request_id: int | str) -> None:
    """Handle initialize request."""
    _log(f"Received initialize: {params}")

    # MCP initialization response
    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "equipa-mcp-server",
                "version": "1.0.0",
            },
        }
    })


def _handle_initialized(params: dict) -> None:
    """Handle initialized notification."""
    _log("Received initialized notification")
    # No response for notifications


def _handle_tools_list(params: dict, request_id: int | str) -> None:
    """Handle tools/list request."""
    _log("Received tools/list")

    tools_list = []
    for name, spec in TOOLS.items():
        tools_list.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        })

    _send_response({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": tools_list,
        }
    })


def _handle_tools_call(params: dict, request_id: int | str) -> None:
    """Handle tools/call request."""
    tool_name = params.get("name")
    args = params.get("arguments", {})

    # Avoid logging the auth_token verbatim.
    redacted = {k: ("***" if k == "auth_token" else v) for k, v in args.items()}
    _log(f"Received tools/call: {tool_name} with args {redacted}")

    if tool_name not in TOOLS:
        _send_error(request_id, -32601, f"Unknown tool: {tool_name}")
        return

    try:
        handler = TOOLS[tool_name]["handler"]
        # Pass a shallow copy so auth_token pop in handlers does not mutate
        # the caller-provided params (matters for replay/tests).
        result = handler(dict(args))

        _send_response({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2),
                    }
                ],
            }
        })
    except Exception as exc:
        _log(f"Tool error: {exc}")
        _send_error(request_id, -32603, f"Tool execution failed: {exc}")


# --- Main Loop ---

def run_server() -> None:
    """Main MCP server loop — read JSON-RPC from stdin, respond on stdout."""
    _log("EQUIPA MCP Server starting...")
    if _expected_token() is None:
        _log(
            "WARNING: EQUIPA_MCP_TOKEN is not set; privileged tool calls "
            "(dispatch, task_create) will be rejected until it is configured."
        )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"Invalid JSON: {exc}")
            _send_error(None, -32700, "Parse error")
            continue

        # Extract fields
        jsonrpc = request.get("jsonrpc")
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        if jsonrpc != "2.0":
            _send_error(request_id, -32600, "Invalid Request")
            continue

        # Dispatch based on method
        if method == "initialize":
            _handle_initialize(params, request_id)
        elif method == "notifications/initialized":
            _handle_initialized(params)
        elif method == "tools/list":
            _handle_tools_list(params, request_id)
        elif method == "tools/call":
            _handle_tools_call(params, request_id)
        else:
            _send_error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Entry point for MCP server."""
    try:
        run_server()
    except KeyboardInterrupt:
        _log("Server interrupted")
        sys.exit(0)
    except Exception as exc:
        _log(f"Fatal error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
