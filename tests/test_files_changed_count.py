#!/usr/bin/env python3
"""Regression tests for agent_runs.files_changed_count population (task #2314).

Before this fix the schema column existed but no code path populated it.
``record_agent_run`` always inserted 0 because it looked for an explicit
``result["files_changed_count"]`` key that no caller set. As a result the
vacuous-pass alert ``success=1 AND files_changed_count=0`` was 100% false-
positive and had to be disabled.

These tests exercise ``_resolve_files_changed_count`` (the new helper) and
the end-to-end insert through ``record_agent_run``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from equipa.db import (
    _FILES_CHANGED_MAX,
    _parse_files_changed_block,
    _resolve_files_changed_count,
)


class TestResolveFilesChangedCount(unittest.TestCase):
    """Unit tests for the resolution helper."""

    def test_none_or_non_dict_returns_zero(self):
        self.assertEqual(_resolve_files_changed_count(None), 0)
        self.assertEqual(_resolve_files_changed_count("not a dict"), 0)
        self.assertEqual(_resolve_files_changed_count(42), 0)

    def test_explicit_count_wins(self):
        result = {"files_changed_count": 7, "files_changed_set": ["a.py", "b.py"]}
        self.assertEqual(_resolve_files_changed_count(result), 7)

    def test_explicit_zero_is_respected(self):
        # An explicit 0 from the caller must be trusted — the caller deliberately
        # said "no files changed" and we should not silently shadow that with a
        # parsed footer count.
        result = {
            "files_changed_count": 0,
            "result_text": "FILES_CHANGED: hallucinated.py",
        }
        self.assertEqual(_resolve_files_changed_count(result), 0)

    def test_explicit_negative_falls_through(self):
        # Negative values are nonsense; ignore and use the next signal.
        result = {
            "files_changed_count": -1,
            "files_changed_set": ["a.py", "b.py", "c.py"],
        }
        self.assertEqual(_resolve_files_changed_count(result), 3)

    def test_files_changed_set_used_when_no_explicit_count(self):
        result = {"files_changed_set": ["equipa/db.py", "equipa/loops.py"]}
        self.assertEqual(_resolve_files_changed_count(result), 2)

    def test_files_changed_list_fallback(self):
        # Some loop callers attach this key instead of files_changed_set.
        result = {"files_changed": ["foo.py"]}
        self.assertEqual(_resolve_files_changed_count(result), 1)

    def test_set_input_supported(self):
        result = {"files_changed_set": {"a.py", "b.py", "c.py"}}
        self.assertEqual(_resolve_files_changed_count(result), 3)

    def test_footer_alone_is_not_trusted(self):
        # Security re-review (#2314 Phase A): the FILES_CHANGED footer is
        # agent-controlled. When files_changed_set is absent, the helper logs
        # a callsite warning and returns 0 rather than trusting the footer.
        result_text = (
            "RESULT: success\n"
            "SUMMARY: did the work\n"
            "FILES_CHANGED:\n"
            "- equipa/db.py\n"
            "- tests/test_files_changed_count.py\n"
            "DECISIONS: none\n"
            "BLOCKERS: none\n"
        )
        self.assertEqual(_resolve_files_changed_count({"result_text": result_text}), 0)

    def test_empty_files_changed_set_blocks_footer_forgery(self):
        # Phase-A verification probe from the task spec:
        #   {"result_text": "FILES_CHANGED:\n- forged.py\n", "files_changed_set": []}
        # MUST return 0, NOT 1. A vacuous-pass agent could otherwise forge a
        # footer to claim it landed work it never actually did.
        result = {
            "result_text": "FILES_CHANGED:\n- forged.py\n",
            "files_changed_set": [],
        }
        self.assertEqual(_resolve_files_changed_count(result), 0)

    def test_files_changed_set_overrides_footer(self):
        # Structured signal from agent_runner is more trustworthy than a
        # footer the agent could have typoed.
        result = {
            "files_changed_set": ["a.py", "b.py", "c.py"],
            "result_text": "FILES_CHANGED: only-one.py",
        }
        self.assertEqual(_resolve_files_changed_count(result), 3)

    def test_empty_result_dict_is_zero(self):
        self.assertEqual(_resolve_files_changed_count({}), 0)

    # --- Phase B: bool must be rejected -------------------------------------

    def test_explicit_true_rejected(self):
        # Phase-B verification probe: isinstance(True, int) is True in Python,
        # so a naive int-check would silently coerce True into 1. The helper
        # must reject bools and fall through.
        result = {"files_changed_count": True, "files_changed_set": []}
        self.assertEqual(_resolve_files_changed_count(result), 0)

    def test_explicit_false_rejected(self):
        result = {"files_changed_count": False, "files_changed_set": ["a.py"]}
        # files_changed_count rejected → falls through to files_changed_set.
        self.assertEqual(_resolve_files_changed_count(result), 1)

    # --- Phase C: upper-bound clamp -----------------------------------------

    def test_explicit_giant_int_is_clamped(self):
        # Phase-C verification probe: implausible counts get clamped to
        # _FILES_CHANGED_MAX so downstream alerting/quality-scoring is not
        # skewed by fabricated or runaway values.
        result = {"files_changed_count": 999_999_999_999}
        self.assertEqual(_resolve_files_changed_count(result), _FILES_CHANGED_MAX)

    def test_clamp_applies_to_files_changed_set(self):
        result = {"files_changed_set": [f"f{i}.py" for i in range(_FILES_CHANGED_MAX + 5)]}
        self.assertEqual(_resolve_files_changed_count(result), _FILES_CHANGED_MAX)

    def test_malformed_files_changed_set_is_zero(self):
        # If files_changed_set is present but the wrong type, treat as 0; do
        # NOT silently fall through to the footer.
        result = {
            "files_changed_set": "not-a-list",
            "result_text": "FILES_CHANGED:\n- a.py\n- b.py\n",
        }
        self.assertEqual(_resolve_files_changed_count(result), 0)


class TestParseFilesChangedBlock(unittest.TestCase):
    """Unit tests for the footer parser."""

    def test_handles_blank_input(self):
        self.assertEqual(_parse_files_changed_block(""), [])
        self.assertEqual(_parse_files_changed_block(None or ""), [])

    def test_does_not_match_instruction_prose(self):
        # The developer prompt contains the literal text "FILES_CHANGED is
        # REQUIRED — list every file..." We must not interpret that as a
        # footer.
        result_text = (
            "Before your edit, remember: FILES_CHANGED is required so the "
            "orchestrator can verify your work."
        )
        # The colon after FILES_CHANGED here isn't on a line start before
        # actual file entries — should yield nothing meaningful. Even if it
        # matches, the parsed content is prose and gets dropped via list/none
        # heuristics.
        parsed = _parse_files_changed_block(result_text)
        # We accept either [] (no match) OR a list whose items are real-looking
        # paths; assert no false multi-file count.
        self.assertLessEqual(len(parsed), 1)

    def test_strips_bullets_and_status_markers(self):
        text = (
            "FILES_CHANGED:\n"
            "* src/a.py (created)\n"
            "- src/b.py (modified)\n"
            "1. src/c.py\n"
            "DECISIONS: x\n"
        )
        self.assertEqual(
            _parse_files_changed_block(text),
            ["src/a.py", "src/b.py", "src/c.py"],
        )

    def test_case_insensitive_header(self):
        text = "files_changed: equipa/db.py\nDECISIONS: none\n"
        self.assertEqual(_parse_files_changed_block(text), ["equipa/db.py"])


class TestRecordAgentRunEndToEnd(unittest.TestCase):
    """End-to-end: insert via record_agent_run and read the column back."""

    def setUp(self):
        # Use a private temp DB so we don't depend on the global test fixture.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Minimum schema needed by record_agent_run.
        self.conn.executescript(
            """
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                project_id INTEGER,
                role TEXT,
                model TEXT,
                complexity TEXT,
                num_turns INTEGER,
                max_turns_allowed INTEGER,
                duration_seconds REAL,
                cost_usd REAL,
                outcome TEXT,
                success INTEGER,
                cycle_number INTEGER,
                continuation_count INTEGER,
                files_changed_count INTEGER DEFAULT 0,
                error_type TEXT,
                error_summary TEXT,
                turns_allocated INTEGER,
                prompt_version TEXT,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()
        self.conn.close()

        # Point equipa.constants.THEFORGE_DB at the temp DB so db_conn opens it.
        import equipa.constants as constants
        import equipa.db as equipa_db
        self._original_db = constants.THEFORGE_DB
        constants.THEFORGE_DB = Path(self.db_path)
        equipa_db.THEFORGE_DB = Path(self.db_path)
        equipa_db._SCHEMA_ENSURED = True  # skip migration apply

    def tearDown(self):
        import equipa.constants as constants
        import equipa.db as equipa_db
        constants.THEFORGE_DB = self._original_db
        equipa_db.THEFORGE_DB = self._original_db
        os.unlink(self.db_path)

    def _read_back(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(
                "SELECT role, success, files_changed_count, outcome "
                "FROM agent_runs ORDER BY id"
            ))
        finally:
            conn.close()

    def test_developer_with_edits_records_nonzero(self):
        """A developer cycle that touched 3 files reports files_changed_count=3."""
        from equipa.db import record_agent_run

        task = {"id": 9001, "project_id": 23, "title": "x", "description": "y"}
        # Patch get_task_complexity since the tasks table isn't seeded.
        with patch("equipa.tasks.get_task_complexity", return_value="medium"):
            record_agent_run(
                task,
                result={
                    "num_turns": 12,
                    "duration": 90.0,
                    "files_changed_set": ["equipa/db.py", "equipa/loops.py", "tests/x.py"],
                },
                outcome="tests_passed",
                role="developer",
                model="opus",
                max_turns=25,
            )

        rows = self._read_back()
        self.assertEqual(len(rows), 1, "expected one row")
        self.assertEqual(rows[0]["role"], "developer")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(
            rows[0]["files_changed_count"], 3,
            "fix regression: developer agent that wrote 3 files must record 3, not 0",
        )

    def test_vacuous_pass_records_zero(self):
        """Success outcome with NO file edits records files_changed_count=0.

        This is the exact signal forgemind-alerts.py uses to flag vacuous
        passes: success=1 AND files_changed_count=0.
        """
        from equipa.db import record_agent_run

        task = {"id": 9002, "project_id": 23, "title": "x", "description": "y"}
        with patch("equipa.tasks.get_task_complexity", return_value="medium"):
            record_agent_run(
                task,
                result={
                    "num_turns": 5,
                    "duration": 30.0,
                    "files_changed_set": [],  # no Edit/Write tool calls
                    "result_text": "RESULT: success\nFILES_CHANGED: none\n",
                },
                outcome="tests_passed",
                role="developer",
                model="opus",
                max_turns=25,
            )

        rows = self._read_back()
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["files_changed_count"], 0)

    def test_security_reviewer_records_zero_files(self):
        """Reviewers don't edit code; their count should be 0."""
        from equipa.db import record_agent_run

        task = {"id": 9003, "project_id": 23, "title": "x", "description": "y"}
        with patch("equipa.tasks.get_task_complexity", return_value="medium"):
            record_agent_run(
                task,
                result={"num_turns": 8, "duration": 60.0, "files_changed_set": []},
                outcome="no_tests",
                role="security-reviewer",
                model="opus",
                max_turns=15,
            )

        rows = self._read_back()
        self.assertEqual(rows[0]["role"], "security-reviewer")
        self.assertEqual(rows[0]["files_changed_count"], 0)

    def test_footer_only_result_does_not_inflate_count(self):
        """Result with no structured signal records 0, even with a footer.

        Security re-review (#2314 Phase A): agent-emitted footers are no longer
        a fallback. A run that reaches record_agent_run without
        ``files_changed_set`` is treated as a callsite bug — the count is 0,
        not whatever the agent typed in the footer.
        """
        from equipa.db import record_agent_run

        task = {"id": 9004, "project_id": 23, "title": "x", "description": "y"}
        result_text = (
            "RESULT: success\n"
            "SUMMARY: implemented\n"
            "FILES_CHANGED:\n"
            "- equipa/x.py\n"
            "- equipa/y.py\n"
            "DECISIONS: none\n"
        )
        with patch("equipa.tasks.get_task_complexity", return_value="medium"):
            record_agent_run(
                task,
                result={"num_turns": 10, "duration": 40.0, "result_text": result_text},
                outcome="tests_passed",
                role="developer",
                model="opus",
                max_turns=25,
            )

        rows = self._read_back()
        self.assertEqual(rows[0]["files_changed_count"], 0)

    def test_explicit_count_from_caller_takes_precedence(self):
        """Phase-D path: when the streaming runner wrote a canonical
        ``files_changed_count`` (derived from git diff HEAD~1), the DB row
        uses that value rather than the tool-call-derived set length.
        """
        from equipa.db import record_agent_run

        task = {"id": 9005, "project_id": 23, "title": "x", "description": "y"}
        with patch("equipa.tasks.get_task_complexity", return_value="medium"):
            record_agent_run(
                task,
                result={
                    "num_turns": 9,
                    "duration": 70.0,
                    # tool-call observation under-counted (Bash-driven writes
                    # are invisible to it) — git diff is authoritative.
                    "files_changed_set": ["a.py"],
                    "files_changed_count": 4,
                },
                outcome="tests_passed",
                role="developer",
                model="opus",
                max_turns=25,
            )

        rows = self._read_back()
        self.assertEqual(rows[0]["files_changed_count"], 4)


if __name__ == "__main__":
    unittest.main()
