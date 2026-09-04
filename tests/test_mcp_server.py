"""Tests for equipa.mcp_server — MCP JSON-RPC 2.0 over stdio.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_isolated_db(db_path: Path) -> None:
    """Create a self-contained TheForge DB with the minimum schema the MCP
    server's tools touch. Keeping this inline (rather than running the full
    migration suite) avoids coupling the MCP test to migration ordering and
    keeps the fixture fast.

    Mirrors the columns referenced by mcp_server._handle_* (tasks, projects,
    lessons_learned, agent_runs, session_notes, open_questions, decisions).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                codename TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                priority TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'todo',
                completed_at TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE TABLE lessons_learned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson TEXT NOT NULL,
                error_type TEXT,
                error_signature TEXT,
                times_seen INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                role TEXT,
                outcome TEXT,
                duration_seconds REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE session_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                summary TEXT,
                next_steps TEXT,
                session_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE open_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                question TEXT,
                context TEXT,
                resolved INTEGER DEFAULT 0
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                topic TEXT,
                decision TEXT,
                rationale TEXT,
                alternatives_considered TEXT,
                decision_type TEXT DEFAULT 'general',
                status TEXT DEFAULT 'open',
                resolved_by_task_id INTEGER,
                verified_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO projects (id, name, codename) VALUES (23, 'Equipa', 'eq')"
        )
        conn.commit()
    finally:
        conn.close()


def _send_request(proc: subprocess.Popen, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """Send JSON-RPC request to MCP server and read response."""
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
    }
    if params is not None:
        request["params"] = params

    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    # Read response
    response_line = proc.stdout.readline()
    return json.loads(response_line)


def _send_notification(proc: subprocess.Popen, method: str, params: dict | None = None) -> None:
    """Send JSON-RPC notification (no response expected)."""
    request = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        request["params"] = params

    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()


@pytest.fixture
def isolated_db(tmp_path: Path) -> Path:
    """Create an isolated TheForge DB inside tmp_path.

    The MCP server reads THEFORGE_DB at import time (equipa.constants),
    so the fixture that spawns the server must export this env var BEFORE
    starting the subprocess. Otherwise tests leak rows into the production
    DB at /srv/forge-share/AI_Stuff/Equipa-repo/theforge.db.
    """
    db_path = tmp_path / "theforge.db"
    _build_isolated_db(db_path)
    return db_path


TEST_TOKEN = "test-token-abc123"


def _spawn_server(db_path: Path, *, token: str | None = TEST_TOKEN,
                  cost_cap_usd: str | None = None,
                  project_allowlist: str | None = None) -> subprocess.Popen:
    """Spawn the MCP server subprocess with a controlled environment."""
    env = os.environ.copy()
    env["THEFORGE_DB"] = str(db_path)
    if token is None:
        env.pop("EQUIPA_MCP_TOKEN", None)
    else:
        env["EQUIPA_MCP_TOKEN"] = token
    if cost_cap_usd is None:
        env.pop("EQUIPA_MCP_COST_CAP_USD", None)
    else:
        env["EQUIPA_MCP_COST_CAP_USD"] = cost_cap_usd
    if project_allowlist is None:
        env.pop("EQUIPA_MCP_PROJECT_IDS", None)
    else:
        env["EQUIPA_MCP_PROJECT_IDS"] = project_allowlist
    return subprocess.Popen(
        [sys.executable, "-m", "equipa.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


@pytest.fixture
def mcp_server(isolated_db: Path):
    """Spawn MCP server subprocess for testing, pinned to an isolated DB.

    THEFORGE_DB must be set in the subprocess env BEFORE Popen — the MCP
    server resolves the DB path once at import time via equipa.constants.
    A test EQUIPA_MCP_TOKEN is also set so privileged tools can be exercised.
    """
    proc = _spawn_server(isolated_db)
    yield proc
    _stop_server(proc)


def test_initialize(mcp_server):
    """Test initialize handshake."""
    response = _send_request(mcp_server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert "serverInfo" in response["result"]
    assert response["result"]["serverInfo"]["name"] == "equipa-mcp-server"


def test_initialized_notification(mcp_server):
    """Test initialized notification (no response)."""
    # Send initialize first
    _send_request(mcp_server, "initialize", {})

    # Send initialized notification
    _send_notification(mcp_server, "notifications/initialized")

    # No response expected — server should remain alive
    # Test by sending another request
    response = _send_request(mcp_server, "tools/list", {}, request_id=2)
    assert response["jsonrpc"] == "2.0"


def test_tools_list(mcp_server):
    """Test tools/list returns all 10 tools."""
    response = _send_request(mcp_server, "tools/list", {})

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    assert "tools" in response["result"]

    tools = response["result"]["tools"]
    tool_names = {t["name"] for t in tools}

    expected = {
        "equipa_dispatch",
        "equipa_task_status",
        "equipa_task_create",
        "equipa_lessons",
        "equipa_agent_logs",
        "equipa_project_context",
        "equipa_session_notes",
        "equipa_session_note_add",
        "equipa_lesson_add",
        "equipa_decision_add",
    }

    assert tool_names == expected

    # Verify each tool has required fields
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


def test_task_status_missing_arg(mcp_server):
    """Test equipa_task_status with missing task_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_status",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "task_id required" in content["error"]


def test_task_status_nonexistent(mcp_server):
    """Test equipa_task_status with nonexistent task."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_status",
        "arguments": {"task_id": 999999},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content


def test_lessons_default(mcp_server):
    """Test equipa_lessons with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_lessons",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "lessons" in content
    assert "count" in content
    assert isinstance(content["lessons"], list)


def test_agent_logs_default(mcp_server):
    """Test equipa_agent_logs with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_agent_logs",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "runs" in content
    assert "count" in content
    assert isinstance(content["runs"], list)


def test_session_notes_default(mcp_server):
    """Test equipa_session_notes with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_session_notes",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "notes" in content
    assert "count" in content
    assert isinstance(content["notes"], list)


def test_project_context_missing_arg(mcp_server):
    """Test equipa_project_context with missing project_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_project_context",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "project_id required" in content["error"]


def test_unknown_tool(mcp_server):
    """Test calling unknown tool returns error."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "unknown_tool",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_unknown_method(mcp_server):
    """Test calling unknown method returns error."""
    response = _send_request(mcp_server, "unknown/method", {})

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_invalid_json(mcp_server):
    """Test sending invalid JSON returns parse error."""
    mcp_server.stdin.write("not valid json\n")
    mcp_server.stdin.flush()

    response_line = mcp_server.stdout.readline()
    response = json.loads(response_line)

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32700


def test_task_create_success(mcp_server, isolated_db):
    """Test equipa_task_create creates a task in the ISOLATED DB only."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_create",
        "arguments": {
            "auth_token": TEST_TOKEN,
            "project_id": 23,  # EQUIPA project
            "title": "MCP Test Task",
            "description": "Created by test_mcp_server.py",
            "priority": "low",
        },
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "task_id" in content
    assert content["status"] == "created"
    assert content["title"] == "MCP Test Task"

    # Verify the row landed in the isolated DB, NOT the production DB.
    conn = sqlite3.connect(str(isolated_db))
    try:
        row = conn.execute(
            "SELECT title, description, priority, status FROM tasks WHERE id = ?",
            (content["task_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "MCP Test Task"
    assert row[1] == "Created by test_mcp_server.py"
    assert row[2] == "low"
    assert row[3] == "todo"


def test_no_test_rows_in_production_db():
    """Regression guard: the test suite must NEVER write 'MCP Test Task'
    rows into the production TheForge DB.

    Historically the mcp_server fixture inherited THEFORGE_DB from the
    ambient environment, which in CI/dev runs from /srv/.../Equipa-repo
    resolves to the live production DB. Every pytest run leaked an
    'MCP Test Task' / 'Created by test_mcp_server.py' stub at project_id=23
    (e.g. ids 2147, 2152, 2153, 2186, 2187, 2188, 2189 on 2026-05-03).

    This test asserts no such rows exist after the suite runs against the
    production DB at the canonical path. It is a no-op when the production
    DB is absent (e.g. in CI without the live DB mounted).
    """
    prod_db = REPO_ROOT / "theforge.db"
    if not prod_db.exists():
        pytest.skip(f"Production DB not present at {prod_db}; nothing to guard.")

    conn = sqlite3.connect(str(prod_db))
    try:
        # tasks table must exist on the production DB; if it does not,
        # the path is not a real TheForge DB — skip rather than fail.
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if tbl is None:
            pytest.skip(f"tasks table missing in {prod_db}; not a TheForge DB.")

        leaked = conn.execute(
            """
            SELECT id, title FROM tasks
            WHERE project_id = 23
              AND title = 'MCP Test Task'
            """
        ).fetchall()
    finally:
        conn.close()

    assert leaked == [], (
        f"Found {len(leaked)} 'MCP Test Task' rows leaked into production DB "
        f"({prod_db}): {leaked}. The mcp_server pytest fixture must isolate "
        "THEFORGE_DB to a tmp_path."
    )


def test_dispatch_missing_arg(mcp_server):
    """Test equipa_dispatch with valid auth but missing task_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {"auth_token": TEST_TOKEN},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "task_id required" in content["error"]


# --- MCP-01: authentication + rate limit + role/model allowlist ---


def test_dispatch_rejects_missing_token(mcp_server):
    """equipa_dispatch refuses calls that omit the auth_token argument."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {"task_id": 1},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "auth_token" in content["error"].lower()


def test_dispatch_rejects_bad_token(mcp_server):
    """equipa_dispatch refuses calls with a wrong auth_token."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {"auth_token": "wrong-token", "task_id": 1},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "auth_token" in content["error"].lower()


def test_dispatch_rejects_unconfigured_token(isolated_db):
    """Server without EQUIPA_MCP_TOKEN env fails closed on privileged calls."""
    proc = _spawn_server(isolated_db, token=None)
    try:
        response = _send_request(proc, "tools/call", {
            "name": "equipa_dispatch",
            "arguments": {"auth_token": "anything", "task_id": 1},
        })
        content = json.loads(response["result"]["content"][0]["text"])
        assert content.get("auth") == "unconfigured"
    finally:
        _stop_server(proc)


def test_dispatch_rejects_unknown_role(mcp_server):
    """Roles outside the orchestrator allowlist are rejected before subprocess spawn."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {
            "auth_token": TEST_TOKEN,
            "task_id": 1,
            "role": "nonsense-role",
        },
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "allowed_roles" in content
    assert "not in allowlist" in content["error"]


def test_dispatch_rejects_unknown_model(mcp_server):
    """Models outside {opus, sonnet, haiku} are rejected before subprocess spawn."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {
            "auth_token": TEST_TOKEN,
            "task_id": 1,
            "role": "developer",
            "model": "gpt-4",
        },
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "allowed_models" in content
    assert "not in allowlist" in content["error"]


def test_dispatch_rate_limit_fires(monkeypatch):
    """Token bucket exhausts after DISPATCH_RATE_CAPACITY direct handler calls."""
    from equipa import mcp_server as srv

    # Use a fresh bucket so prior tests don't bleed state.
    monkeypatch.setattr(srv, "_DISPATCH_BUCKET",
                        srv._TokenBucket(srv.DISPATCH_RATE_CAPACITY,
                                         srv.DISPATCH_RATE_REFILL_SECONDS))
    monkeypatch.setenv("EQUIPA_MCP_TOKEN", TEST_TOKEN)

    # Skip the actual subprocess spawn — we only need to exhaust the bucket.
    class _FakeProc:
        pid = 0

    monkeypatch.setattr(srv.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    base_args = {"auth_token": TEST_TOKEN, "task_id": 1, "role": "developer"}

    for _ in range(srv.DISPATCH_RATE_CAPACITY):
        result = srv._handle_equipa_dispatch(dict(base_args))
        assert result.get("status") == "spawned", result

    # Next call must be rate-limited.
    blocked = srv._handle_equipa_dispatch(dict(base_args))
    assert "Rate limit exceeded" in blocked.get("error", "")
    assert "retry_after_seconds" in blocked


def test_dispatch_cost_cap_blocks(monkeypatch, isolated_db):
    """When recent spend exceeds the cap, dispatch is refused before spawn."""
    proc = _spawn_server(isolated_db, cost_cap_usd="0.01")
    try:
        # Insert a costly run to push past the 0.01 USD cap.
        conn = sqlite3.connect(str(isolated_db))
        try:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN cost_usd REAL DEFAULT 0")
            conn.execute(
                "INSERT INTO agent_runs (task_id, role, outcome, duration_seconds, cost_usd) "
                "VALUES (1, 'developer', 'success', 1.0, 5.0)"
            )
            conn.commit()
        finally:
            conn.close()

        response = _send_request(proc, "tools/call", {
            "name": "equipa_dispatch",
            "arguments": {
                "auth_token": TEST_TOKEN,
                "task_id": 1,
                "role": "developer",
            },
        })
        content = json.loads(response["result"]["content"][0]["text"])
        assert "cost cap" in content.get("error", "").lower()
        assert content.get("cap_usd") == 0.01
    finally:
        _stop_server(proc)


# --- MCP-02: task_create validation + project allowlist ---


def test_task_create_rejects_missing_token(mcp_server):
    """equipa_task_create requires auth_token."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_create",
        "arguments": {"project_id": 23, "title": "x"},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "auth_token" in content["error"].lower()


def test_task_create_rejects_nonexistent_project(mcp_server):
    """equipa_task_create refuses project_ids not present in projects table."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_create",
        "arguments": {
            "auth_token": TEST_TOKEN,
            "project_id": 9999,
            "title": "should fail",
        },
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "does not exist" in content.get("error", "")


def test_task_create_rejects_inactive_project(isolated_db):
    """A project with status='completed' is not a valid task_create target."""
    conn = sqlite3.connect(str(isolated_db))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, codename, status) "
            "VALUES (77, 'Archive', 'arc', 'completed')"
        )
        conn.commit()
    finally:
        conn.close()

    proc = _spawn_server(isolated_db)
    try:
        response = _send_request(proc, "tools/call", {
            "name": "equipa_task_create",
            "arguments": {
                "auth_token": TEST_TOKEN,
                "project_id": 77,
                "title": "should fail",
            },
        })
        content = json.loads(response["result"]["content"][0]["text"])
        assert "expected one of" in content.get("error", "")
    finally:
        _stop_server(proc)


def test_task_create_rejects_oversize_description(mcp_server):
    """description payloads beyond MAX_DESCRIPTION_BYTES are refused."""
    from equipa.mcp_server import MAX_DESCRIPTION_BYTES

    payload = "x" * (MAX_DESCRIPTION_BYTES + 1)
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_create",
        "arguments": {
            "auth_token": TEST_TOKEN,
            "project_id": 23,
            "title": "too big",
            "description": payload,
        },
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert "exceeds" in content.get("error", "")


def test_task_create_respects_project_allowlist(isolated_db):
    """When EQUIPA_MCP_PROJECT_IDS is set, projects outside are refused."""
    proc = _spawn_server(isolated_db, project_allowlist="99,100")
    try:
        response = _send_request(proc, "tools/call", {
            "name": "equipa_task_create",
            "arguments": {
                "auth_token": TEST_TOKEN,
                "project_id": 23,
                "title": "blocked by allowlist",
            },
        })
        content = json.loads(response["result"]["content"][0]["text"])
        assert "allowlist" in content.get("error", "").lower()
    finally:
        _stop_server(proc)


def test_task_create_rate_limit_fires(monkeypatch):
    """Token bucket exhausts after TASK_CREATE_RATE_CAPACITY handler calls."""
    from equipa import mcp_server as srv

    monkeypatch.setattr(srv, "_TASK_CREATE_BUCKET",
                        srv._TokenBucket(2, 3600))
    monkeypatch.setenv("EQUIPA_MCP_TOKEN", TEST_TOKEN)

    # Patch the DB context so we don't need a real DB for this unit test.
    class _FakeCursor:
        lastrowid = 1

    class _FakeConn:
        def execute(self, sql, params=()):
            if sql.strip().startswith("SELECT id, status"):
                class _Row:
                    def __getitem__(self, k):
                        return {"id": params[0], "status": "active"}[k]
                return type("R", (), {"fetchone": lambda self_: _Row()})()
            return _FakeCursor()

    from contextlib import contextmanager

    @contextmanager
    def fake_conn(write=False):
        yield _FakeConn()

    monkeypatch.setattr(srv, "_db_conn", fake_conn)

    base = {"auth_token": TEST_TOKEN, "project_id": 23, "title": "t"}
    assert srv._handle_equipa_task_create(dict(base)).get("status") == "created"
    assert srv._handle_equipa_task_create(dict(base)).get("status") == "created"
    blocked = srv._handle_equipa_task_create(dict(base))
    assert "Rate limit exceeded" in blocked.get("error", "")


# --- MCP-03: query limit clamp ---


def test_lessons_limit_clamped(mcp_server):
    """Caller-supplied lessons limit is clamped to MAX_QUERY_LIMIT."""
    from equipa.mcp_server import MAX_QUERY_LIMIT

    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_lessons",
        "arguments": {"limit": 10_000_000},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert content.get("limit") == MAX_QUERY_LIMIT


def test_agent_logs_limit_clamped(mcp_server):
    """Caller-supplied agent_logs limit is clamped to MAX_QUERY_LIMIT."""
    from equipa.mcp_server import MAX_QUERY_LIMIT

    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_agent_logs",
        "arguments": {"limit": 10_000_000},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert content.get("limit") == MAX_QUERY_LIMIT


def test_session_notes_limit_clamped(mcp_server):
    """Caller-supplied session_notes limit is clamped to MAX_QUERY_LIMIT."""
    from equipa.mcp_server import MAX_QUERY_LIMIT

    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_session_notes",
        "arguments": {"limit": 10_000_000},
    })
    content = json.loads(response["result"]["content"][0]["text"])
    assert content.get("limit") == MAX_QUERY_LIMIT


def test_clamp_limit_unit():
    """_clamp_limit unit coverage for non-int, zero, negative and oversize inputs."""
    from equipa.mcp_server import MAX_QUERY_LIMIT, _clamp_limit

    assert _clamp_limit(5, "t") == 5
    assert _clamp_limit("12", "t") == 12
    assert _clamp_limit("not a number", "t") == MAX_QUERY_LIMIT
    assert _clamp_limit(0, "t") == MAX_QUERY_LIMIT
    assert _clamp_limit(-1, "t") == MAX_QUERY_LIMIT
    assert _clamp_limit(MAX_QUERY_LIMIT + 1, "t") == MAX_QUERY_LIMIT


def test_cli_mcp_server_flag():
    """Test that --mcp-server flag launches the server."""
    # This test would require full EQUIPA setup, so just verify the module can be imported
    import equipa.mcp_server
    assert hasattr(equipa.mcp_server, "run_server")
    assert callable(equipa.mcp_server.run_server)


# --- equipa_decision_add (MCP-06) ---

def _decision_add(server, **overrides) -> dict:
    """Call equipa_decision_add with sane defaults, returning the parsed payload."""
    args = {
        "auth_token": TEST_TOKEN,
        "project_id": 23,
        "topic": "Adopt the Hooper reverser",
        "decision": "Prototype both reversal mechanisms on one circuit.",
    }
    args.update(overrides)
    response = _send_request(server, "tools/call", {
        "name": "equipa_decision_add",
        "arguments": args,
    })
    return json.loads(response["result"]["content"][0]["text"])


def test_decision_add_happy_path(mcp_server, isolated_db):
    """A well-formed call persists a decision and reports its id."""
    content = _decision_add(
        mcp_server,
        rationale="Comparative data beats proving one approach.",
        alternatives_considered="Bench rig only.",
        decision_type="architectural",
        status="decided",
    )

    assert "error" not in content, content
    assert content["status"] == "created"
    assert content["project_id"] == 23
    assert content["decision_type"] == "architectural"
    assert content["decision_status"] == "decided"
    assert isinstance(content["decision_id"], int)

    conn = sqlite3.connect(str(isolated_db))
    try:
        row = conn.execute(
            "SELECT project_id, topic, decision, rationale, decision_type, status "
            "FROM decisions WHERE id = ?",
            (content["decision_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == 23
    assert row[1] == "Adopt the Hooper reverser"
    assert row[4] == "architectural"
    assert row[5] == "decided"


def test_decision_add_defaults(mcp_server):
    """decision_type and status default to general/open when omitted."""
    content = _decision_add(mcp_server)

    assert "error" not in content, content
    assert content["decision_type"] == "general"
    assert content["decision_status"] == "open"


def test_decision_add_requires_auth(isolated_db):
    """Without a matching token the write is refused."""
    proc = _spawn_server(isolated_db)
    try:
        content = _decision_add(proc, auth_token="wrong-token")
        assert "error" in content
    finally:
        _stop_server(proc)


def test_decision_add_rejects_unknown_vocabulary(mcp_server):
    """decision_type and status are checked against the accepted vocabularies."""
    bad_type = _decision_add(mcp_server, decision_type="not-a-category")
    assert "error" in bad_type
    assert "decision_type" in bad_type["error"]

    bad_status = _decision_add(mcp_server, status="not-a-status")
    assert "error" in bad_status
    assert "status" in bad_status["error"]


def test_decision_add_validates_project(mcp_server):
    """A nonexistent project_id is rejected before any insert."""
    content = _decision_add(mcp_server, project_id=999999)
    assert "error" in content
    assert "does not exist" in content["error"]


def test_decision_add_requires_topic_and_decision(mcp_server):
    """Empty topic or decision is rejected after sanitization."""
    assert "error" in _decision_add(mcp_server, topic="")
    assert "error" in _decision_add(mcp_server, decision="")


def test_decision_body_cap_exceeds_rationale_cap():
    """The narrative body must not be capped at the rationale limit.

    Decision bodies accrete amendments; measured against a 636-row production
    table 1.9% already exceed MAX_DECISION_LENGTH, the longest at 32,344 chars.
    Capping the body there would silently destroy real records — the failure
    mode behind Equipa task #100027.
    """
    from lesson_sanitizer import (
        MAX_DECISION_LENGTH,
        sanitize_decision,
        sanitize_decision_body,
    )

    # Prose, not filler. A long unbroken alphanumeric run would be removed
    # wholesale by the base64-blob injection pattern ([A-Za-z0-9+/]{80,}), which
    # is correct behaviour but makes "x" * N useless as a length fixture.
    sentence = "The reverser rotates the car body through the frog. "
    long_body = sentence * 400  # ~20.8k chars, comfortably over the 8k cap

    assert len(sanitize_decision_body(long_body)) > MAX_DECISION_LENGTH
    assert len(sanitize_decision(long_body)) == MAX_DECISION_LENGTH


def test_sanitize_strips_base64_blobs():
    """Long unbroken alphanumeric runs are stripped as suspected encoded payloads.

    Documented because it is easy to mistake for data loss: the injection pattern
    [A-Za-z0-9+/]{80,} removes base64-shaped blobs wholesale, and unlike the
    length caps this removal is not logged. Ordinary prose is unaffected.
    """
    from lesson_sanitizer import sanitize_decision_body

    assert sanitize_decision_body("A" * 200) == ""
    assert "reverser" in sanitize_decision_body("The reverser turns the car.")


