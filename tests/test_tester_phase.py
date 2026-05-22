"""Regression tests for tester-phase helpers extracted from run_dev_test_loop.

Covers _capture_git_diff_context and _dispatch_tester_outcome — the two
helpers carved out as part of the S3 decomposition (task #2097).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from equipa.loops import _capture_git_diff_context, _dispatch_tester_outcome


# ---- _capture_git_diff_context ---------------------------------------------

class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _run(coro):
    """Drive an async helper synchronously inside a test."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestCaptureGitDiffContext:
    def test_returns_empty_when_diff_is_empty(self):
        with patch("equipa.loops.git_run_async", new=AsyncMock(return_value=_FakeProc(0, ""))):
            assert _run(_capture_git_diff_context("/tmp", cycle=1)) == ""

    def test_returns_empty_on_nonzero_returncode(self):
        with patch("equipa.loops.git_run_async", new=AsyncMock(return_value=_FakeProc(128, ""))):
            assert _run(_capture_git_diff_context("/tmp", cycle=1)) == ""

    def test_formats_non_empty_diff(self):
        diff = "diff --git a/x.py b/x.py\n+ added line"
        with patch("equipa.loops.git_run_async", new=AsyncMock(return_value=_FakeProc(0, diff))):
            ctx = _run(_capture_git_diff_context("/tmp", cycle=2))
        assert "## Developer Changes (git diff)" in ctx
        assert "```diff\n" in ctx
        assert "added line" in ctx
        assert "Write tests that verify these specific changes" in ctx

    def test_truncates_at_8000_chars(self):
        # Build a diff that exceeds the 8000-char cap
        long_diff = "x" * 9000
        with patch("equipa.loops.git_run_async", new=AsyncMock(return_value=_FakeProc(0, long_diff))):
            ctx = _run(_capture_git_diff_context("/tmp", cycle=1))
        assert "[... diff truncated, 1000 chars omitted ...]" in ctx

    def test_swallows_subprocess_errors(self):
        import subprocess
        with patch("equipa.loops.git_run_async", new=AsyncMock(side_effect=subprocess.TimeoutExpired("git", 10))):
            assert _run(_capture_git_diff_context("/tmp", cycle=1)) == ""

    def test_swallows_filenotfound(self):
        with patch("equipa.loops.git_run_async", new=AsyncMock(side_effect=FileNotFoundError("git"))):
            assert _run(_capture_git_diff_context("/tmp", cycle=1)) == ""

    def test_uses_provided_base_ref(self):
        """Per-cycle SHA mode (task #2145): diff runs against base_ref, not HEAD."""
        captured: dict[str, Any] = {}

        async def fake_async(args, project_dir, timeout=None):
            captured["args"] = args
            return _FakeProc(0, "")

        with patch("equipa.loops.git_run_async", side_effect=fake_async):
            _run(_capture_git_diff_context("/tmp", cycle=2, base_ref="abc1234"))

        # git_run_async receives the args list without the "git" prefix
        assert "abc1234" in captured["args"]
        assert "HEAD" not in captured["args"]


# ---- _dispatch_tester_outcome ----------------------------------------------

@pytest.fixture
def patched_msg_helpers(monkeypatch):
    """Stub out side-effecting helpers so dispatch logic stays isolated."""
    posted: list[tuple] = []
    monkeypatch.setattr(
        "equipa.loops.post_agent_message",
        lambda *a, **k: posted.append((a, k)),
    )
    monkeypatch.setattr("equipa.loops.clear_checkpoints", lambda task_id: None)
    monkeypatch.setattr(
        "equipa.loops._apply_cost_totals",
        lambda result, cost, dur: result.update({"cost": cost, "duration": dur}),
    )
    monkeypatch.setattr(
        "equipa.loops.build_test_failure_context",
        lambda results, cycle: f"failure ctx cycle {cycle}",
    )
    return posted


def _results(outcome: str, run: int = 0, passed: int = 0, failed: int = 0,
             skipped: int = 0, details: list | None = None,
             tests_skipped_present: bool = True) -> dict:
    """Build a parsed-tester payload.

    Task #2242 Phase A: ``tests_skipped_present`` defaults to True so the
    common case (well-formed tester output) is the default. Tests that
    exercise the contract-violation path pass ``tests_skipped_present=False``.
    """
    payload: dict[str, Any] = {
        "result": outcome,
        "tests_run": run,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_skipped": skipped,
        "failure_details": details or [],
    }
    if tests_skipped_present:
        payload["tests_skipped_present"] = True
    return payload


class TestDispatchTesterOutcome:
    def test_pass_returns_exit_with_tester_result(self, patched_msg_helpers):
        tester = {"some": "tester"}
        dev = {"some": "dev"}
        history: list[str] = []
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=3, passed=3),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=1.0, total_duration=2.0,
            compaction_history=history,
        )
        assert action == "exit"
        assert ret is tester
        assert outcome == "tests_passed"
        assert tester["cost"] == 1.0
        assert len(patched_msg_helpers) == 1

    def test_no_tests_returns_dev_result(self, patched_msg_helpers):
        tester = {"some": "tester"}
        dev = {"some": "dev"}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("no-tests"), tester, dev, cycle=1, task_id=1,
            task_role="developer", total_cost=0.5, total_duration=1.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert ret is dev
        assert outcome == "no_tests"

    def test_blocked_returns_exit_with_tester_blocked(self, patched_msg_helpers):
        tester = {}
        dev = {}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("blocked", details=["dep missing"]),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert ret is tester
        assert outcome == "tester_blocked"

    def test_unknown_with_zero_tests_treated_as_no_tests(self, patched_msg_helpers):
        tester = {}
        dev = {}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("unknown", run=0, failed=0),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert ret is dev
        assert outcome == "no_tests"

    def test_fail_appends_failure_context_and_continues(self, patched_msg_helpers):
        tester = {}
        dev = {}
        history: list[str] = []
        action, ret, outcome = _dispatch_tester_outcome(
            _results("fail", run=3, passed=1, failed=2,
                     details=["t1 failed", "t2 failed"]),
            tester, dev, cycle=2, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=history,
        )
        assert action == "continue_loop"
        assert ret is None
        assert outcome is None
        assert history == ["failure ctx cycle 2"]

    # Task #2242: all-tests-skipped should NOT be mapped to tests_passed —
    # it routes to tests_inconclusive so the orchestrator does not mark the
    # task done. Repro of the GutenForge Stage D #2236 loophole: 1 test,
    # tests_passed=0, tests_skipped=1, tester reports "pass".
    def test_all_skipped_pass_returns_tests_inconclusive(
        self, patched_msg_helpers
    ):
        tester = {"some": "tester"}
        dev = {"some": "dev"}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=1, passed=0, skipped=1),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.5, total_duration=1.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert ret is tester
        assert outcome == "tests_inconclusive"
        # Cost totals still applied so we can audit the wasted run.
        assert tester["cost"] == 0.5
        # A tests_inconclusive message must be posted (not test_passed).
        assert len(patched_msg_helpers) == 1
        posted_args = patched_msg_helpers[0][0]
        assert "tests_inconclusive" in posted_args

    def test_partial_skip_with_real_passes_still_counts_as_pass(
        self, patched_msg_helpers
    ):
        # tests_passed > 0 means at least one test actually ran and asserted
        # — this is real validation, not the vacuous all-skipped pattern.
        tester = {"some": "tester"}
        dev = {"some": "dev"}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=10, passed=7, skipped=3),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert outcome == "tests_passed"

    def test_unknown_with_failed_tests_falls_through_to_fail(
        self, patched_msg_helpers
    ):
        # 'unknown' but tests_failed > 0 is NOT the no-tests shortcut,
        # so it falls through to the fail branch.
        tester = {}
        dev = {}
        history: list[str] = []
        action, ret, outcome = _dispatch_tester_outcome(
            _results("unknown", run=2, failed=2),
            tester, dev, cycle=3, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=history,
        )
        assert action == "continue_loop"
        assert history == ["failure ctx cycle 3"]

    # --- Task #2242 re-dispatch: defense-in-depth fixtures -------------------

    def test_missing_tests_skipped_line_routes_inconclusive(
        self, patched_msg_helpers
    ):
        """Phase A: tester output without TESTS_SKIPPED line -> inconclusive.

        Even when tests_run > 0 and tests_passed == tests_run (looks like a
        clean pass), the absence of TESTS_SKIPPED is a contract violation —
        the tester may have drifted/truncated and the skip count cannot be
        trusted. Fail closed.
        """
        tester = {"some": "tester", "result_text": "RESULT: pass\nTESTS_RUN: 5\nTESTS_PASSED: 5"}
        dev = {"some": "dev"}
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=5, passed=5, tests_skipped_present=False),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert outcome == "tests_inconclusive"
        # The posted message must carry the Phase A reason.
        posted_args = patched_msg_helpers[0][0]
        assert "tests_inconclusive" in posted_args
        # Last positional arg is the JSON payload — must include the reason.
        import json
        payload = json.loads(posted_args[-1])
        assert payload["reason"] == "missing_tests_skipped_field"

    def test_framework_skip_disagreement_routes_inconclusive(
        self, patched_msg_helpers
    ):
        """Phase B: raw stdout says '5 skipped' but tester reported 0 -> inconclusive.

        The hardest variant of the loophole: tester reports TESTS_RUN: 0,
        TESTS_PASSED: 0, TESTS_SKIPPED: 0 (all zeros, internally consistent,
        skip-line present). But the raw framework stdout shows '5 skipped'.
        Phase B cross-checks and flags the disagreement.
        """
        raw = """
        Running tests...
        Test Files  1 passed, 1 skipped (2)
             Tests  0 passed, 5 skipped (5)
        """
        tester = {"some": "tester", "result_text": raw}
        dev = {"some": "dev"}
        # Note: we use run=1 so the Phase A guard doesn't fire; the disagreement
        # is what Phase B catches.
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=1, passed=1, skipped=0),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert outcome == "tests_inconclusive"
        import json
        payload = json.loads(patched_msg_helpers[0][0][-1])
        assert payload["reason"] == "framework_skip_count_disagrees"
        assert payload["framework_skips"] >= 5

    def test_counts_inconsistent_routes_inconclusive(self, patched_msg_helpers):
        """Phase C: passed + failed + skipped != tests_run -> inconclusive.

        Catches both adversarial misreport and benign tester bugs without
        any agent-protocol change.
        """
        tester = {"some": "tester", "result_text": "RESULT: pass\n"}
        dev = {"some": "dev"}
        # 10 run, 0 passed, 0 failed, 3 skipped — 7 unaccounted for
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=10, passed=0, failed=0, skipped=3),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert outcome == "tests_inconclusive"
        import json
        payload = json.loads(patched_msg_helpers[0][0][-1])
        assert payload["reason"] == "counts_inconsistent"
        assert payload["expected_sum"] == 10
        assert payload["actual_sum"] == 3

    def test_consistent_counts_with_passes_is_pass(self, patched_msg_helpers):
        """Phase C: consistent partial-skip with real passes is tests_passed."""
        tester = {"some": "tester", "result_text": "RESULT: pass\n"}
        dev = {"some": "dev"}
        # 10 run, 7 passed, 0 failed, 3 skipped — sums to 10
        action, ret, outcome = _dispatch_tester_outcome(
            _results("pass", run=10, passed=7, failed=0, skipped=3),
            tester, dev, cycle=1, task_id=1, task_role="developer",
            total_cost=0.0, total_duration=0.0,
            compaction_history=[],
        )
        assert action == "exit"
        assert outcome == "tests_passed"
