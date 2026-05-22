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


class TestPhaseA3GitDiffRange(unittest.TestCase):
    """Phase A3 regression tests: pre_head..post_head diff range.

    Attempt-2 used HEAD~1..HEAD and had three failure modes:
      (a) multi-commit cycles under-counted (only last commit seen).
      (b) non-writer roles inherited the developer's diff.
      (c) deferred commits caused stale-HEAD overrides of fresh edits.
    These fixtures exercise the real ``_git_diff_files_changed_count`` and
    ``_git_rev_parse_head`` helpers against on-disk git repos, then drive
    the integration through ``record_agent_run`` to confirm the DB row.
    """

    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix="equipa-test-a3-")
        # init a quiet repo with a deterministic identity
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.repo_dir, check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=self.repo_dir, check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=self.repo_dir, check=True)
        sp.run(["git", "config", "commit.gpgsign", "false"],
               cwd=self.repo_dir, check=True)
        # initial commit so HEAD resolves
        (Path(self.repo_dir) / "README.md").write_text("seed\n")
        sp.run(["git", "add", "README.md"], cwd=self.repo_dir, check=True)
        sp.run(["git", "commit", "-q", "-m", "seed"], cwd=self.repo_dir, check=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.repo_dir, ignore_errors=True)

    def _commit_files(self, files: list[tuple[str, str]], message: str) -> None:
        """Write each (relpath, content) and create one commit."""
        import subprocess as sp
        for relpath, content in files:
            target = Path(self.repo_dir) / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        sp.run(["git", "add", "-A"], cwd=self.repo_dir, check=True)
        sp.run(["git", "commit", "-q", "-m", message],
               cwd=self.repo_dir, check=True)

    def _head_sha(self) -> str:
        import subprocess as sp
        out = sp.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir,
                     capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_multi_commit_cycle_counts_all_unique_files(self):
        """A developer cycle with 3 commits touching 6 unique files reports 6
        (NOT 2 from only the last commit, the attempt-2 HEAD~1..HEAD bug).
        """
        import asyncio
        from equipa.agent_runner import (
            _git_diff_files_changed_count, _git_rev_parse_head,
        )

        pre_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        self.assertIsNotNone(pre_head)

        self._commit_files([("a.py", "a"), ("b.py", "b")], "c1")
        self._commit_files([("c.py", "c"), ("d.py", "d")], "c2")
        self._commit_files([("e.py", "e"), ("f.py", "f")], "c3")

        post_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        self.assertNotEqual(pre_head, post_head)

        count = asyncio.run(_git_diff_files_changed_count(
            self.repo_dir, pre_head, post_head,
        ))
        self.assertEqual(count, 6,
                         "multi-commit cycle must count all 6 unique files")

    def test_no_commits_means_pre_head_equals_post_head(self):
        """A non-writer role makes no commits; pre_head == post_head, so the
        cross-check should NEVER override files_changed_count (which would
        otherwise leak the developer's diff into the reviewer's row).
        """
        import asyncio
        from equipa.agent_runner import _git_rev_parse_head

        pre_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        # reviewer role does nothing — no commits added
        post_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        self.assertEqual(pre_head, post_head,
                         "no commits => pre_head must equal post_head")

    def test_bash_driven_writes_are_caught_by_git_diff(self):
        """Bash-driven file writes (sed -i, tee, cp) are invisible to
        files_changed_set, but git diff sees them. One commit touching 2
        files via Bash should report count=2.
        """
        import asyncio
        from equipa.agent_runner import (
            _git_diff_files_changed_count, _git_rev_parse_head,
        )

        pre_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        # simulate two Bash-driven writes landed in one commit
        self._commit_files(
            [("bash_one.txt", "echoed"), ("bash_two.txt", "tee'd")],
            "bash writes",
        )
        post_head = asyncio.run(_git_rev_parse_head(self.repo_dir))

        count = asyncio.run(_git_diff_files_changed_count(
            self.repo_dir, pre_head, post_head,
        ))
        self.assertEqual(count, 2)

    def test_unique_files_across_commits_not_double_counted(self):
        """A file edited in two commits is counted once, not twice."""
        import asyncio
        from equipa.agent_runner import (
            _git_diff_files_changed_count, _git_rev_parse_head,
        )

        pre_head = asyncio.run(_git_rev_parse_head(self.repo_dir))
        self._commit_files([("same.py", "v1")], "c1")
        self._commit_files([("same.py", "v2"), ("other.py", "x")], "c2")
        post_head = asyncio.run(_git_rev_parse_head(self.repo_dir))

        count = asyncio.run(_git_diff_files_changed_count(
            self.repo_dir, pre_head, post_head,
        ))
        self.assertEqual(count, 2,
                         "same.py edited twice still counts once; +other.py => 2")

    def test_rev_parse_returns_none_when_no_commits(self):
        """A fresh repo with no commits returns None — caller skips override."""
        import asyncio
        from equipa.agent_runner import _git_rev_parse_head
        import subprocess as sp

        empty_dir = tempfile.mkdtemp(prefix="equipa-test-a3-empty-")
        try:
            sp.run(["git", "init", "-q", "-b", "main"], cwd=empty_dir, check=True)
            sha = asyncio.run(_git_rev_parse_head(empty_dir))
            self.assertIsNone(sha,
                              "empty repo HEAD should resolve to None")
        finally:
            import shutil
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_rev_parse_returns_none_for_non_repo(self):
        """A directory that is not a git repo returns None, never raises."""
        import asyncio
        from equipa.agent_runner import _git_rev_parse_head

        non_repo = tempfile.mkdtemp(prefix="equipa-test-a3-nonrepo-")
        try:
            sha = asyncio.run(_git_rev_parse_head(non_repo))
            self.assertIsNone(sha)
        finally:
            import shutil
            shutil.rmtree(non_repo, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
