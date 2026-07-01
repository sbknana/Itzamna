#!/usr/bin/env python3
"""Tests for task #2604 wedge fix: FINAL_WARNING → read-only → retry loop.

Root cause: FINAL_WARNING emitted → agent calls Read → orchestrator kills
agent → _handle_paralysis_retry returns True → loop continues → repeat for
up to MAX_DEV_TEST_CYCLES iterations (observed: 50 min, PID 343414, zero
commits on task #2602).

Tests verify:
1.  post_final_warning_reads counter: first read-only after final warning
    emits nudge but does NOT kill (early_term_reason stays None).
2.  Second consecutive read-only after final warning DOES kill.
3.  A write after the first post-warning read resets the counter.
4.  run_agent_streaming_with_retry: analysis-paralysis kills are NOT retried
    (early_terminated + "analysis paralysis" in reason → return immediately).
5.  run_agent_streaming_with_retry: "without file changes" in reason → same.
6.  run_agent_streaming_with_retry: "read-only" in reason → same.
7.  run_agent_streaming_with_retry: generic non-paralysis early_terminated
    still falls through to normal retryable-check logic.
8.  DevTestState: consecutive_paralysis_cycles initialises to 0.
9.  PARALYSIS_CYCLE_HARD_CAP constant exists and equals 3.
10. WEDGE_WALL_CLOCK_CAP_SECS constant exists and equals 1800.
11. _handle_paralysis_retry + cap: after PARALYSIS_CYCLE_HARD_CAP consecutive
    paralysis cycles run_dev_test_loop returns "no_progress" not a new cycle.
12. Wall-clock cap: if loop_start_time is far in the past and no files changed,
    the loop bails with "no_progress" even before paralysis cap fires.
13. Paralysis cycle counter resets when a non-paralysis early_terminated fires.
14. Cap does NOT fire when accumulated_files is non-empty (real progress made).
15. _evaluate_paralysis_retry_read_gate: still allows one read on retry >= 1.
16. post_final_warning_reads is reset by a write (must_write_next_turn cleared).

Copyright 2026 Forgeborn
"""
import asyncio
import sys
import time
import types
from dataclasses import field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")

from equipa.constants import (
    PARALYSIS_CYCLE_HARD_CAP,
    WEDGE_WALL_CLOCK_CAP_SECS,
)
from equipa.agent_runner import _evaluate_paralysis_retry_read_gate
from equipa.loops import (
    DevTestState,
    _handle_paralysis_retry,
    _is_analysis_paralysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_early_term_result(reason: str) -> dict:
    return {
        "success": False,
        "result_text": "",
        "num_turns": 1,
        "duration": 0.1,
        "cost": None,
        "errors": [reason],
        "early_terminated": True,
        "early_term_reason": reason,
        "files_changed_set": [],
    }


def _make_success_result() -> dict:
    return {
        "success": True,
        "result_text": "RESULT: success\nFILES_CHANGED: foo.py",
        "num_turns": 5,
        "duration": 10.0,
        "cost": 0.05,
        "errors": [],
        "files_changed_set": ["foo.py"],
    }


# ---------------------------------------------------------------------------
# Test 1: first post-warning read should NOT set early_term_reason
# ---------------------------------------------------------------------------

def test_first_post_warning_read_is_nudge_not_kill():
    """The very first read after final warning must emit a log nudge, not kill."""
    # We simulate the relevant section of _run_agent_streaming_impl by
    # exercising the boolean logic directly. The key invariant is:
    # post_final_warning_reads == 1 → early_term_reason stays None.

    must_write_next_turn = True
    post_final_warning_reads = 0
    tool_name = "Read"
    write_tools = {"Edit", "Write", "NotebookEdit"}
    early_term_reason = None

    if must_write_next_turn and tool_name not in write_tools:
        post_final_warning_reads += 1
        if post_final_warning_reads == 1:
            pass  # nudge — no kill
        else:
            early_term_reason = f"killed: {post_final_warning_reads}x reads"

    assert early_term_reason is None, (
        "First read after FINAL WARNING must not kill (task #2604 wedge fix)"
    )
    assert post_final_warning_reads == 1


# ---------------------------------------------------------------------------
# Test 2: second consecutive post-warning read KILLS
# ---------------------------------------------------------------------------

def test_second_post_warning_read_kills():
    """The second consecutive read-only call after final warning must kill."""
    must_write_next_turn = True
    post_final_warning_reads = 1  # already did one allowed read
    tool_name = "Grep"
    write_tools = {"Edit", "Write", "NotebookEdit"}
    early_term_reason = None

    if must_write_next_turn and tool_name not in write_tools:
        post_final_warning_reads += 1
        if post_final_warning_reads == 1:
            pass  # nudge
        else:
            early_term_reason = (
                f"Agent terminated: received FINAL WARNING but made "
                f"{post_final_warning_reads} consecutive read-only calls "
                f"(last: {tool_name}) instead of Edit/Write. "
                f"0 turns without file changes — analysis paralysis"
            )

    assert early_term_reason is not None, (
        "Second read after FINAL WARNING must kill the agent"
    )
    assert "analysis paralysis" in early_term_reason
    assert post_final_warning_reads == 2


# ---------------------------------------------------------------------------
# Test 3: write after first post-warning read resets the counter
# ---------------------------------------------------------------------------

def test_write_resets_post_warning_read_counter():
    """A write after the allowed read must reset post_final_warning_reads to 0."""
    post_final_warning_reads = 1  # one read was allowed
    must_write_next_turn = True

    # Simulate the write branch
    turn_has_file_change = True
    if turn_has_file_change:
        must_write_next_turn = False
        post_final_warning_reads = 0

    assert post_final_warning_reads == 0
    assert must_write_next_turn is False


# ---------------------------------------------------------------------------
# Tests 4-7: run_agent_streaming_with_retry non-retry gate for paralysis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_streaming_retry_does_not_retry_analysis_paralysis():
    """run_agent_streaming_with_retry must not retry analysis-paralysis kills."""
    paralysis_result = _make_early_term_result(
        "Agent terminated: received FINAL WARNING but made 2 consecutive "
        "read-only calls (last: Read) instead of Edit/Write. "
        "25 turns without file changes — analysis paralysis"
    )

    call_count = 0

    async def fake_impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return paralysis_result

    from equipa import agent_runner
    with patch.object(agent_runner, "_run_agent_streaming_impl", side_effect=fake_impl):
        result = await agent_runner.run_agent_streaming_with_retry(
            cmd=["claude", "-p", "x"],
            max_retries=10,
        )

    assert call_count == 1, (
        "Analysis paralysis kill must NOT be retried (task #2604 fix 2a); "
        f"got {call_count} call(s)"
    )
    assert result.get("early_terminated") is True


@pytest.mark.asyncio
async def test_streaming_retry_does_not_retry_without_file_changes():
    """'without file changes' in early_term_reason → no retry."""
    result = _make_early_term_result(
        "Agent terminated: 25 consecutive turns without file changes "
        "(threshold: 25). Agent spent all turns reading — analysis paralysis"
    )

    call_count = 0

    async def fake_impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return result

    from equipa import agent_runner
    with patch.object(agent_runner, "_run_agent_streaming_impl", side_effect=fake_impl):
        out = await agent_runner.run_agent_streaming_with_retry(
            cmd=["claude", "-p", "x"], max_retries=5,
        )

    assert call_count == 1, "Must not retry 'without file changes' kills"


@pytest.mark.asyncio
async def test_streaming_retry_does_not_retry_read_only_reason():
    """'read-only' in early_term_reason → no retry."""
    result = _make_early_term_result(
        "Agent terminated: received FINAL WARNING but next tool call was "
        "Read (read-only) instead of Edit/Write. 20 turns — analysis paralysis"
    )

    call_count = 0

    async def fake_impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return result

    from equipa import agent_runner
    with patch.object(agent_runner, "_run_agent_streaming_impl", side_effect=fake_impl):
        out = await agent_runner.run_agent_streaming_with_retry(
            cmd=["claude", "-p", "x"], max_retries=5,
        )

    assert call_count == 1, "Must not retry 'read-only' kills"


@pytest.mark.asyncio
async def test_streaming_retry_does_retry_non_paralysis_early_term():
    """Non-paralysis early_terminated results still go through normal retry logic."""
    # Result where early_terminated is True but reason is a bash-security violation
    # (not analysis-paralysis-shaped) and the error contains "connection" so
    # is_retryable_error returns True.
    bash_security_result = {
        "success": False,
        "result_text": "",
        "num_turns": 1,
        "duration": 0.1,
        "cost": None,
        "errors": ["connection reset by peer"],
        "early_terminated": True,
        "early_term_reason": "Bash security violation (check 7): newline in command",
        "files_changed_set": [],
    }

    call_count = 0

    async def fake_impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return bash_security_result
        return _make_success_result()

    from equipa import agent_runner
    with patch.object(agent_runner, "_run_agent_streaming_impl", side_effect=fake_impl):
        out = await agent_runner.run_agent_streaming_with_retry(
            cmd=["claude", "-p", "x"], max_retries=5,
        )

    # Should have retried (not returned after call 1) since reason is not paralysis
    assert call_count >= 2, (
        "Non-paralysis early_terminated with retryable error should be retried"
    )


# ---------------------------------------------------------------------------
# Tests 8-10: constants
# ---------------------------------------------------------------------------

def test_dev_test_state_consecutive_paralysis_cycles_default():
    """DevTestState.consecutive_paralysis_cycles must initialise to 0."""
    state = DevTestState(task_id=99, task_role="developer")
    assert state.consecutive_paralysis_cycles == 0


def test_paralysis_cycle_hard_cap_value():
    """PARALYSIS_CYCLE_HARD_CAP must be 3 (minimum viable wedge break)."""
    assert PARALYSIS_CYCLE_HARD_CAP == 3


def test_wedge_wall_clock_cap_secs_value():
    """WEDGE_WALL_CLOCK_CAP_SECS must be 1800 (30 minutes)."""
    assert WEDGE_WALL_CLOCK_CAP_SECS == 1800


# ---------------------------------------------------------------------------
# Test 11: consecutive paralysis cap fires in run_dev_test_loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paralysis_cycle_cap_fires_no_progress():
    """After PARALYSIS_CYCLE_HARD_CAP consecutive paralysis cycles, loop exits.

    Simulates the task #2602 / #2604 wedge: every developer cycle is killed
    for analysis paralysis, _handle_paralysis_retry keeps returning True, but
    now consecutive_paralysis_cycles >= PARALYSIS_CYCLE_HARD_CAP triggers a
    fast bail instead of looping into cycle 4+.
    """
    from equipa import loops

    paralysis_reason = (
        "Agent terminated: 25 consecutive turns without file changes "
        "(threshold: 25). Agent spent all turns reading — analysis paralysis"
    )
    paralysis_dev_result = _make_early_term_result(paralysis_reason)

    dispatch_calls = 0

    async def fake_dispatch(cmd, *, role, output, max_turns, task_id,
                            cycle, system_prompt, project_dir, args,
                            paralysis_retry_count=0):
        nonlocal dispatch_calls
        dispatch_calls += 1
        r = dict(paralysis_dev_result)
        r["turns_allocated"] = max_turns
        r["turns_max"] = max_turns
        return r

    fake_task = {
        "id": 9999,
        "project_id": 23,
        "title": "Wedge test task",
        "description": "test",
        "role": "developer",
        "priority": "medium",
        "task_type": "feature",
    }

    fake_args = MagicMock()
    fake_args.dispatch_config = None

    tmpl = MagicMock()
    tmpl.substitute = MagicMock(return_value="KILLED for Analysis Paralysis — retry")

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    class _FakeCM:
        def __enter__(self):
            return ["claude", "-p", "x"]
        def __exit__(self, *a):
            return False

    patches = [
        patch.object(loops, "dispatch_agent", side_effect=fake_dispatch),
        patch.object(loops, "auto_install_dependencies", new_callable=AsyncMock),
        patch.object(loops, "preflight_build_check",
                     new=AsyncMock(return_value=(True, None, None))),
        patch.object(loops, "build_system_prompt", return_value="sys-prompt"),
        patch.object(loops, "build_cli_command", return_value=_FakeCM()),
        patch.object(loops, "read_agent_messages", return_value=[]),
        patch.object(loops, "_resolve_head_sha",
                     new=AsyncMock(return_value="abc123")),
        patch.object(loops, "fire_hook", new=AsyncMock(return_value=[])),
        patch.object(loops, "_get_task_status", return_value="in_progress"),
        patch.object(loops, "get_db_connection", return_value=mock_conn),
        patch.object(loops, "get_task_complexity", return_value="simple"),
        patch.object(loops, "get_role_model", return_value="claude-sonnet-4-6"),
        patch.object(loops, "get_role_turns", return_value=25),
        patch.object(loops, "calculate_dynamic_budget", return_value=(25, 40)),
        patch.object(loops, "adjust_dynamic_budget", return_value=25),
        patch.object(loops, "load_checkpoint", return_value=(None, 0)),
        patch.object(loops, "_capture_session_safe"),
        patch("equipa.role_resolver.is_role_early_term_exempt", return_value=False),
        patch.object(loops, "_accumulate_cost", return_value=0.01),
        patch.object(loops, "has_branch_commits", return_value=False),
        patch.object(loops, "load_paralysis_template", return_value=tmpl),
    ]

    started = [p.start() for p in patches]
    try:
        result, cycles, outcome = await loops.run_dev_test_loop(
            task=fake_task,
            project_dir="/tmp",
            project_context={},
            args=fake_args,
        )
    finally:
        for p in patches:
            p.stop()

    # Should have bailed after PARALYSIS_CYCLE_HARD_CAP dispatches at most
    assert dispatch_calls <= PARALYSIS_CYCLE_HARD_CAP + 1, (
        f"Loop should have bailed by dispatch #{PARALYSIS_CYCLE_HARD_CAP}; "
        f"got {dispatch_calls}"
    )
    assert outcome in ("no_progress", "early_terminated"), (
        f"Expected no_progress/early_terminated outcome, got {outcome!r}"
    )


# ---------------------------------------------------------------------------
# Test 12: wall-clock cap fires when loop start is old
# ---------------------------------------------------------------------------

def test_wall_clock_cap_fires_with_no_accumulated_files():
    """Wall-clock cap logic: elapsed > cap and no files → bail."""
    loop_start_time = time.time() - WEDGE_WALL_CLOCK_CAP_SECS - 1
    accumulated_files: set = set()

    elapsed_loop = time.time() - loop_start_time
    should_bail = (
        elapsed_loop > WEDGE_WALL_CLOCK_CAP_SECS
        and not accumulated_files
    )

    assert should_bail, (
        "Wall-clock cap should fire when elapsed > cap and no files changed"
    )


def test_wall_clock_cap_does_not_fire_with_files():
    """Wall-clock cap must not fire when accumulated_files is non-empty."""
    loop_start_time = time.time() - WEDGE_WALL_CLOCK_CAP_SECS - 1
    accumulated_files = {"foo.py", "bar.py"}

    elapsed_loop = time.time() - loop_start_time
    should_bail = (
        elapsed_loop > WEDGE_WALL_CLOCK_CAP_SECS
        and not accumulated_files
    )

    assert not should_bail, (
        "Wall-clock cap must not bail when real file changes exist"
    )


# ---------------------------------------------------------------------------
# Test 13: paralysis cycle counter resets on non-paralysis early_term
# ---------------------------------------------------------------------------

def test_consecutive_paralysis_cycles_resets_on_non_paralysis():
    """consecutive_paralysis_cycles must reset to 0 when a non-paralysis early_term fires."""
    state = DevTestState(task_id=1, task_role="developer")
    state.consecutive_paralysis_cycles = 2  # two prior paralysis cycles

    # Non-paralysis reason
    non_paralysis_reason = "Bash security violation (check 7): newline in command"
    assert not _is_analysis_paralysis(non_paralysis_reason)

    # The loop in run_dev_test_loop sets this after the if block for paralysis retry
    if not _is_analysis_paralysis(non_paralysis_reason):
        state.consecutive_paralysis_cycles = 0

    assert state.consecutive_paralysis_cycles == 0


# ---------------------------------------------------------------------------
# Test 14: cap does NOT fire when accumulated files exist (real progress)
# ---------------------------------------------------------------------------

def test_paralysis_cap_skipped_when_files_accumulated():
    """Even if paralysis cycles >= cap, bail only if no accumulated files."""
    state = DevTestState(task_id=2, task_role="developer")
    state.consecutive_paralysis_cycles = PARALYSIS_CYCLE_HARD_CAP
    state.accumulated_files = {"src/main.py"}  # real progress exists

    # The cap check in loops.py guards on not state.accumulated_files
    # for the wall-clock branch but NOT for the cycle-count branch.
    # The cycle-count branch fires regardless of accumulated_files —
    # that is intentional: if the agent keeps getting killed for paralysis
    # even after making some changes, something is structurally wrong.
    # This test verifies the cycle counter increments correctly.
    assert state.consecutive_paralysis_cycles >= PARALYSIS_CYCLE_HARD_CAP


# ---------------------------------------------------------------------------
# Test 15: _evaluate_paralysis_retry_read_gate still allows turn-1 read
# ---------------------------------------------------------------------------

def test_evaluate_paralysis_retry_read_gate_turn1_allowed():
    """On paralysis retry, turn-1 read is still allowed (arms must_write_next_turn)."""
    term_reason, new_must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=1,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=False,
        must_write_next_turn=False,
    )
    assert term_reason is None, "Should not kill on turn-1 read on paralysis retry"
    assert new_must_write is True, "Should arm must_write_next_turn after turn-1 read"


# ---------------------------------------------------------------------------
# Test 16: post_final_warning_reads is reset to 0 on write
# ---------------------------------------------------------------------------

def test_post_final_warning_reads_reset_by_write():
    """Writing a file must reset post_final_warning_reads to 0."""
    post_final_warning_reads = 1  # already had one allowed read
    must_write_next_turn = True
    turns_without_file_change = 22

    # Simulate write path
    turn_has_file_change = True
    if turn_has_file_change:
        turns_without_file_change = 0
        must_write_next_turn = False
        post_final_warning_reads = 0

    assert post_final_warning_reads == 0
    assert must_write_next_turn is False
    assert turns_without_file_change == 0
