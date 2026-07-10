"""Tests for the gate-audit persistence trail (task #2702).

``security_gate._gate_audit_log`` writes each gate decision to stderr AND
persists a durable row to ``agent_actions`` via ``db.log_gate_audit``. These
tests lock three invariants:

  1. A gate event writes exactly one ``agent_actions`` row (tmp_path DB).
  2. A DB error is fail-open — it never raises out of ``_gate_audit_log`` and
     never changes the gate decision the caller computes independently.
  3. The stderr emit is byte-for-byte unchanged by the new persistence, and
     still honours the ``EQUIPA_GATE_AUDIT_LOG`` env gate.

The real ``theforge.db`` is NEVER touched: every test points
``equipa.db.THEFORGE_DB`` at an isolated tmp_path database seeded with only
the ``agent_actions`` table.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from equipa import db as equipa_db
from equipa import security_gate

# Minimal DDL matching schema.sql:577-593 — only what the audit write needs.
_AGENT_ACTIONS_DDL = """
CREATE TABLE agent_actions (
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
"""


@pytest.fixture
def tmp_forge_db(tmp_path, monkeypatch):
    """Point equipa.db at an isolated tmp DB seeded with agent_actions.

    Never touches the real theforge.db. ``_SCHEMA_ENSURED`` is forced True so
    ``log_gate_audit``'s ``ensure_schema()`` call is a no-op against this
    hermetic DB (we create the one table we need directly).
    """
    db_path = tmp_path / "test_theforge.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_AGENT_ACTIONS_DDL)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)
    monkeypatch.setattr(equipa_db, "_SCHEMA_ENSURED", True)
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM agent_actions").fetchall()
    finally:
        conn.close()


class TestGateEventWritesRow:
    def test_structured_call_writes_one_row(self, tmp_forge_db):
        message = "task=2702 event=merge-succeeded branch=forge-task-2702"
        security_gate._gate_audit_log(
            message, task_id=2702, event="merge-succeeded",
        )

        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == 2702
        # No dedicated action_type column exists; tool_name is the discriminator.
        assert row["tool_name"] == "gate-audit"
        assert row["role"] == "security-gate"
        assert row["error_summary"] == "merge-succeeded"
        # The FULL message text is persisted verbatim for the audit trail.
        assert row["tool_input_preview"] == message
        assert row["cycle_number"] == 0
        assert row["turn_number"] == 0
        assert row["success"] == 1

    def test_task_id_parsed_from_message_as_fallback(self, tmp_forge_db):
        # Caller did not pass task_id; the narrow task=<n> fallback recovers it.
        security_gate._gate_audit_log("task=1234 event=merge-failed branch=x")

        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        assert rows[0]["task_id"] == 1234

    def test_no_task_id_skips_db_write(self, tmp_forge_db):
        # task_id NOT NULL in agent_actions: an unattributable event is not
        # forced into the table with a bogus id — it is skipped.
        security_gate._gate_audit_log("event=merge-attempt no-task-token-here")
        assert _rows(tmp_forge_db) == []

    def test_persistence_independent_of_stderr_env_gate(
        self, tmp_forge_db, monkeypatch
    ):
        # EQUIPA_GATE_AUDIT_LOG=0 silences stderr but the durable audit trail
        # must still be written (the whole point of persistence).
        monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "0")
        security_gate._gate_audit_log(
            "task=2702 event=merge-succeeded", task_id=2702,
            event="merge-succeeded",
        )
        assert len(_rows(tmp_forge_db)) == 1


class TestFailOpen:
    def test_db_error_does_not_raise(self, tmp_forge_db, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(equipa_db, "db_conn", _boom)

        # Must NOT raise — the audit persistence is best-effort fail-open.
        security_gate._gate_audit_log(
            "task=2702 event=merge-succeeded", task_id=2702,
            event="merge-succeeded",
        )
        # And nothing was written.
        assert _rows(tmp_forge_db) == []

    def test_db_error_does_not_alter_gate_decision(self, monkeypatch):
        # A caller computes its gate decision independently of the audit call.
        # A DB failure inside _gate_audit_log must leave that decision intact.
        def _boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(equipa_db, "db_conn", _boom)

        def gate_decision(counts: dict) -> str:
            verdict = "blocked" if counts.get("HIGH", 0) else "merged"
            security_gate._gate_audit_log(
                f"task=2702 event={verdict}", task_id=2702, event=verdict,
            )
            return verdict

        # Fail-closed decision is unchanged despite the audit DB being down.
        assert gate_decision({"HIGH": 1}) == "blocked"
        assert gate_decision({"HIGH": 0}) == "merged"

    def test_log_gate_audit_swallows_error_directly(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("no such table: agent_actions")

        monkeypatch.setattr(equipa_db, "_SCHEMA_ENSURED", True)
        monkeypatch.setattr(equipa_db, "db_conn", _boom)

        # log_gate_audit itself is the fail-open boundary; it returns None.
        assert equipa_db.log_gate_audit("m", 2702, event="e") is None


class TestStderrUnchanged:
    def test_stderr_line_is_byte_for_byte_unchanged(self, tmp_forge_db, capsys):
        message = "task=2702 event=merge-succeeded branch=x"
        security_gate._gate_audit_log(
            message, task_id=2702, event="merge-succeeded",
        )
        captured = capsys.readouterr()
        # Exactly the pre-#2702 format: prefix + message + newline, on stderr.
        assert captured.err == f"[GATE-AUDIT] {message}\n"
        assert captured.out == ""

    def test_env_gate_silences_stderr(self, tmp_forge_db, capsys, monkeypatch):
        monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "0")
        security_gate._gate_audit_log(
            "task=2702 event=merge-succeeded", task_id=2702,
        )
        assert capsys.readouterr().err == ""

    def test_default_env_emits_stderr(self, tmp_forge_db, capsys, monkeypatch):
        monkeypatch.delenv("EQUIPA_GATE_AUDIT_LOG", raising=False)
        security_gate._gate_audit_log("task=2702 event=merge-attempt", task_id=2702)
        assert captured_err_has_prefix(capsys)


def captured_err_has_prefix(capsys) -> bool:
    return capsys.readouterr().err.startswith("[GATE-AUDIT] ")


class TestLogGateAuditHelper:
    """Direct tests of ``db.log_gate_audit`` — the new persistence boundary and
    the primary file of task #2702 — exercised WITHOUT routing through
    ``_gate_audit_log`` so column-mapping and fail-open behaviour are pinned at
    the helper itself (the dispatch/cli circuit-breaker sites call it directly).
    """

    def test_helper_writes_full_column_mapping(self, tmp_forge_db):
        message = "task=2702 event=merge-succeeded branch=forge-task-2702"
        equipa_db.log_gate_audit(message, 2702, event="merge-succeeded")

        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == 2702
        assert row["tool_name"] == "gate-audit"
        assert row["role"] == "security-gate"
        assert row["tool_input_preview"] == message
        assert row["error_summary"] == "merge-succeeded"
        assert row["cycle_number"] == 0
        assert row["turn_number"] == 0
        assert row["success"] == 1
        # Columns that a gate event has no value for stay NULL, not defaulted.
        assert row["run_id"] is None
        assert row["error_type"] is None
        assert row["duration_ms"] is None

    def test_event_none_persists_null_error_summary(self, tmp_forge_db):
        # event is optional; the None branch must store NULL, not the string.
        equipa_db.log_gate_audit("task=2702 event=x", 2702, event=None)
        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        assert rows[0]["error_summary"] is None

    def test_long_event_tag_truncated_to_200_chars(self, tmp_forge_db):
        # The helper slices event[:200] so an oversized tag never relies on
        # silent DB truncation. SQLite would store the full string otherwise.
        equipa_db.log_gate_audit("task=2702", 2702, event="e" * 500)
        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        assert len(rows[0]["error_summary"]) == 200

    def test_counts_kwarg_accepted_without_error(self, tmp_forge_db):
        # counts is informational-only but must be accepted so callers can pass
        # structured data; the row is still written.
        equipa_db.log_gate_audit(
            "task=2702 event=merge-skipped C=0 H=1", 2702,
            event="merge-skipped", counts={"HIGH": 1},
        )
        assert len(_rows(tmp_forge_db)) == 1

    def test_none_task_id_returns_none_and_writes_nothing(self, tmp_forge_db):
        # task_id NOT NULL in agent_actions → an unattributable event is skipped
        # rather than forced in with a bogus id.
        assert equipa_db.log_gate_audit("no id token", None) is None
        assert _rows(tmp_forge_db) == []


class TestStructuredTaskIdPrecedence:
    """The docstring promises the structured ``task_id`` param always wins over
    a ``task=<n>`` token parsed from the message. Lock that ordering."""

    def test_structured_task_id_overrides_message_token(self, tmp_forge_db):
        # message embeds a DIFFERENT task token than the structured param.
        security_gate._gate_audit_log(
            "task=999 event=merge-succeeded", task_id=2702,
            event="merge-succeeded",
        )
        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        assert rows[0]["task_id"] == 2702

    def test_counts_flows_through_gate_audit_log(self, tmp_forge_db):
        # The security-review-blocked call site (dispatch._gated_merge_task)
        # passes counts through _gate_audit_log; verify the row still lands.
        security_gate._gate_audit_log(
            "task=2702 event=merge-skipped reason=security-review-blocked "
            "C=0 H=2 M=0 L=0 I=0",
            task_id=2702, event="merge-skipped", counts={"HIGH": 2},
        )
        rows = _rows(tmp_forge_db)
        assert len(rows) == 1
        assert rows[0]["error_summary"] == "merge-skipped"


class TestLeafModuleImportGraph:
    """Task #2702 requirement: ``security_gate`` is a leaf module, and importing
    ``equipa.db`` (done lazily inside ``_gate_audit_log``) must NOT create a
    circular import.

    Run in a FRESH subprocess: an in-process test cannot catch an import cycle
    once both modules are already cached in ``sys.modules``. Importing the leaf
    module first is the order most likely to expose a cycle.
    """

    def test_security_gate_first_then_db_no_cycle(self):
        from pathlib import Path

        repo_root = Path(equipa_db.__file__).resolve().parent.parent
        code = (
            "import equipa.security_gate as sg\n"
            "from equipa.db import log_gate_audit\n"
            "assert callable(log_gate_audit)\n"
            "assert callable(sg._gate_audit_log)\n"
            "print('import-graph-ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"import cycle / import failure:\n{result.stderr}"
        )
        assert "import-graph-ok" in result.stdout
