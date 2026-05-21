"""Tests for cross-attempt memory injection (equipa/dispatch.py).

Validates that failed attempt reflections are correctly built and
injected into task descriptions so retry agents avoid repeating mistakes.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from equipa.dispatch import (
    _ATTEMPT_MARKER,
    _build_dispatch_attempt_reflection,
    _inject_attempt_reflections,
    cleanup_failed_attempt,
)


# ---------------------------------------------------------------------------
# _build_dispatch_attempt_reflection
# ---------------------------------------------------------------------------


class TestBuildReflection:
    """Tests for _build_dispatch_attempt_reflection."""

    def test_cycles_exhausted(self) -> None:
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="cycles_exhausted",
            cycles=5,
            result={"duration": 120, "cost": 0.50},
        )
        assert "ATTEMPT 1 FAILED" in result
        assert "exhausted 5 dev-test cycles" in result
        assert "DO NOT repeat" in result

    def test_cost_limit(self) -> None:
        result = _build_dispatch_attempt_reflection(
            attempt=2,
            outcome="cost_limit",
            cycles=3,
            result={"duration": 60, "cost": 2.50},
        )
        assert "ATTEMPT 2 FAILED" in result
        assert "cost limit" in result
        assert "$2.50" in result

    def test_blocked(self) -> None:
        result = _build_dispatch_attempt_reflection(
            attempt=3,
            outcome="early_completed_blocked",
            cycles=1,
            result={"duration": 30, "cost": 0.10},
        )
        assert "blocked" in result

    def test_loop_detected(self) -> None:
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="loop_detected",
            cycles=4,
            result={"duration": 90, "cost": 0.80},
        )
        assert "loop detected" in result

    def test_extracts_files_from_raw_output(self) -> None:
        raw_output = (
            "RESULT: failed\n"
            "SUMMARY: Tried to fix the router\n"
            "FILES_CHANGED: src/router.py, src/middleware.py\n"
            "BLOCKERS: Missing dependency 'fastapi'\n"
            "REFLECTION: Approached via middleware injection but lacked deps\n"
        )
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="cycles_exhausted",
            cycles=3,
            result={"duration": 120, "cost": 0.50, "raw_output": raw_output},
        )
        assert "router.py" in result
        assert "middleware.py" in result
        assert "Missing dependency" in result
        assert "middleware injection" in result

    def test_no_raw_output(self) -> None:
        """Should not crash when raw_output is missing."""
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="cycles_exhausted",
            cycles=2,
            result={"duration": 30},
        )
        assert "ATTEMPT 1 FAILED" in result

    def test_unknown_outcome(self) -> None:
        """Should handle novel outcome strings gracefully."""
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="something_new",
            cycles=1,
            result={"duration": 10, "cost": 0.01},
        )
        assert "something_new" in result

    def test_tests_inconclusive_explains_skip_pattern(self) -> None:
        """Task #2242: the all-tests-skipped outcome must surface a
        clear, actionable reason in the cross-attempt reflection so
        the retry knows it needs to either supply env defaults or
        rewrite the env-gated tests."""
        result = _build_dispatch_attempt_reflection(
            attempt=1,
            outcome="tests_inconclusive",
            cycles=1,
            result={"duration": 5, "cost": 0.0},
        )
        assert "tests_inconclusive" in result
        assert "skipped" in result.lower()
        assert "env" in result.lower()


# ---------------------------------------------------------------------------
# _inject_attempt_reflections
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory SQLite with a minimal tasks table."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, description TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (id, description) VALUES (1, 'Fix the login bug')"
    )
    conn.commit()
    return conn


class TestInjectReflections:
    """Tests for _inject_attempt_reflections."""

    def test_injects_reflection_block(self, db: sqlite3.Connection) -> None:
        reflections = [
            "ATTEMPT 1 FAILED (exhausted 5 cycles, 120s): DO NOT repeat."
        ]
        _inject_attempt_reflections(db, 1, reflections)
        db.commit()

        desc = db.execute(
            "SELECT description FROM tasks WHERE id = 1"
        ).fetchone()[0]
        assert _ATTEMPT_MARKER in desc
        assert "ATTEMPT 1 FAILED" in desc
        assert desc.startswith("Fix the login bug")

    def test_strips_prior_block_on_reinject(
        self, db: sqlite3.Connection
    ) -> None:
        """Re-injecting should replace, not stack reflection blocks."""
        first = ["ATTEMPT 1 FAILED: first attempt"]
        _inject_attempt_reflections(db, 1, first)
        db.commit()

        second = ["ATTEMPT 1 FAILED: first attempt", "ATTEMPT 2 FAILED: second"]
        _inject_attempt_reflections(db, 1, second)
        db.commit()

        desc = db.execute(
            "SELECT description FROM tasks WHERE id = 1"
        ).fetchone()[0]
        # Should contain exactly ONE marker
        assert desc.count(_ATTEMPT_MARKER) == 1
        # Should have both attempts
        assert "ATTEMPT 2 FAILED" in desc

    def test_truncates_long_reflections(
        self, db: sqlite3.Connection
    ) -> None:
        """Reflection block must be capped at ~2000 chars."""
        huge = ["A" * 3000]
        _inject_attempt_reflections(db, 1, huge)
        db.commit()

        desc = db.execute(
            "SELECT description FROM tasks WHERE id = 1"
        ).fetchone()[0]
        # The reflection block (after marker) should be <= 2050 chars
        marker_pos = desc.index(_ATTEMPT_MARKER)
        block = desc[marker_pos + len(_ATTEMPT_MARKER):]
        assert len(block) < 2100
        assert "trimmed" in block

    def test_nonexistent_task(self, db: sqlite3.Connection) -> None:
        """Should not crash for a task that doesn't exist."""
        _inject_attempt_reflections(db, 999, ["reflection"])
        # No exception = pass

    def test_empty_reflections_list(self, db: sqlite3.Connection) -> None:
        """Empty reflections should still inject the marker."""
        _inject_attempt_reflections(db, 1, [])
        db.commit()

        desc = db.execute(
            "SELECT description FROM tasks WHERE id = 1"
        ).fetchone()[0]
        assert _ATTEMPT_MARKER in desc

    def test_preserves_original_description(
        self, db: sqlite3.Connection
    ) -> None:
        """Original description must remain intact before the marker."""
        reflections = ["ATTEMPT 1 FAILED: something"]
        _inject_attempt_reflections(db, 1, reflections)
        db.commit()

        desc = db.execute(
            "SELECT description FROM tasks WHERE id = 1"
        ).fetchone()[0]
        before_marker = desc[: desc.index(_ATTEMPT_MARKER)]
        assert before_marker == "Fix the login bug"


# ---------------------------------------------------------------------------
# cleanup_failed_attempt
# ---------------------------------------------------------------------------


class TestCleanupFailedAttempt:
    """Tests for the extracted cleanup_failed_attempt() helper.

    This helper unifies the branch-cleanup-and-reset logic that was
    previously duplicated between the parallel/auto path
    (``equipa/dispatch.py``) and the single-task ``--task`` CLI path
    (``equipa/cli.py``). The single-task path historically lacked the
    cross-attempt reflection injection — the regression test
    ``test_invokes_inject_attempt_reflections`` guards against that bug.
    """

    def test_invokes_inject_attempt_reflections(
        self, tmp_path, monkeypatch
    ) -> None:
        """Single-task path MUST now invoke _inject_attempt_reflections.

        This is the bug-fix coverage for D1: prior to extraction, the
        single-task path skipped reflection injection, so retried tasks
        lost memory of what previous attempts had tried.
        """
        from equipa import dispatch as dispatch_mod

        # Spy on _inject_attempt_reflections to confirm it was called
        calls: list[tuple[int, list[str]]] = []

        def fake_inject(conn, task_id, reflections):
            calls.append((task_id, list(reflections)))

        monkeypatch.setattr(
            dispatch_mod, "_inject_attempt_reflections", fake_inject
        )

        # Stub out git so we don't touch a real repo
        monkeypatch.setattr(
            dispatch_mod, "_is_git_repo", lambda _p: False
        )

        # Stub the DB connection to a real in-memory sqlite so the
        # UPDATE call inside cleanup_failed_attempt succeeds without
        # needing TheForge available in the test environment.
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tasks "
            "(id INTEGER PRIMARY KEY, status TEXT, description TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, status, description) "
            "VALUES (42, 'in_progress', 'Fix login')"
        )
        conn.commit()

        # Wrap the connection so .close() under test is a no-op and the
        # in-memory DB remains queryable after cleanup_failed_attempt runs.
        class _NoCloseConn:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def close(self):
                pass

        wrapped = _NoCloseConn(conn)
        monkeypatch.setattr(
            dispatch_mod, "get_db_connection", lambda write=False: wrapped
        )

        reflections = ["ATTEMPT 1 FAILED: tried X, did not work"]
        asyncio.run(cleanup_failed_attempt(
            task_id=42,
            project_dir=str(tmp_path),
            reflections=reflections,
        ))

        assert len(calls) == 1, (
            "cleanup_failed_attempt must invoke _inject_attempt_reflections "
            "exactly once when reflections are provided"
        )
        assert calls[0][0] == 42
        assert calls[0][1] == reflections

        # Status was reset to todo
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = 42"
        ).fetchone()[0]
        assert status == "todo"

    def test_skips_inject_when_reflections_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        """First attempt has no reflections — injection should be skipped."""
        from equipa import dispatch as dispatch_mod

        calls: list[tuple[int, list[str]]] = []

        def fake_inject(conn, task_id, reflections):
            calls.append((task_id, list(reflections)))

        monkeypatch.setattr(
            dispatch_mod, "_inject_attempt_reflections", fake_inject
        )
        monkeypatch.setattr(
            dispatch_mod, "_is_git_repo", lambda _p: False
        )

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tasks "
            "(id INTEGER PRIMARY KEY, status TEXT, description TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, status, description) "
            "VALUES (7, 'in_progress', 'desc')"
        )
        conn.commit()

        class _NoCloseConn:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def close(self):
                pass

        wrapped = _NoCloseConn(conn)
        monkeypatch.setattr(
            dispatch_mod, "get_db_connection", lambda write=False: wrapped
        )

        asyncio.run(cleanup_failed_attempt(
            task_id=7, project_dir=str(tmp_path), reflections=[]
        ))

        assert calls == [], (
            "Empty reflections list must NOT trigger injection"
        )

    def test_cli_imports_cleanup_helper(self) -> None:
        """The single-task CLI path must import the shared helper.

        Guards against the duplication regression by ensuring cli.py
        re-exports / imports the same function from dispatch.py rather
        than reimplementing it inline.
        """
        from equipa import cli as cli_mod
        from equipa.dispatch import cleanup_failed_attempt as dispatch_fn

        assert hasattr(cli_mod, "cleanup_failed_attempt")
        assert cli_mod.cleanup_failed_attempt is dispatch_fn
