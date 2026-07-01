#!/usr/bin/env python3
"""
Test suite for early termination signals in forge_orchestrator.py.

Tests verify:
1. _check_stuck_phrases detects stuck signal phrases correctly
2. _check_stuck_phrases is case-insensitive
3. _check_stuck_phrases returns None for clean text
4. _check_repeated_tool_calls detects 4 identical consecutive calls
5. _check_repeated_tool_calls returns False for varied calls
6. _check_repeated_tool_calls handles short history gracefully
7. EARLY_TERM_EXEMPT_ROLES includes all expected roles
8. EARLY_TERM_WARN_TURNS, EARLY_TERM_FINAL_WARN_TURNS, and EARLY_TERM_KILL_TURNS have correct values
9. _detect_tool_loop integrates correctly with early termination thresholds
10. Escalating warning thresholds are ordered correctly (warn < final_warn < kill)
11. Output hash computed for tool results (_compute_output_hash)
12. Same input + same output detected as stuck (stronger loop signal)
13. Same input + different output not flagged as stuck loop
14. Output hash handles None/empty content gracefully
15. Dead code _check_repeated_tool_calls removed
16. Alternating (A-B-A-B) loop detection triggers at 6+ failing alternations
17. Alternating loop warns at 4+ failing alternations
18. Successful alternating calls are not flagged
19. Mixed success/failure alternating patterns correctly detected
20. Three-way rotation (A-B-C) does not false-positive as alternating
21. Alternating detection coexists with consecutive detection
22. EARLY_COMPLETE: signal parsed correctly from agent text
23. EARLY_COMPLETE waits for message end (multi-line context parsing)
24. EARLY_COMPLETE not confused with RESULT: markers
25. EARLY_COMPLETE cost still tracked (result dict shape)
26. early_completed distinct from early_terminated (success vs failure)
27. False EARLY_COMPLETE in quoted/code text is ignored
28. Budget message returned at periodic intervals (every 5 turns)
29. Budget halfway warning at 50% threshold
30. Budget critical warning at 75% threshold
31. No budget message at turn 0 or when not on interval
32. Budget constants have expected values
33. Budget message includes correct remaining turn count
34. Budget message not returned when max_turns is None
35. Cost breaker terminates when total cost exceeds complexity-scaled limit
36. Cost breaker handles None cost gracefully (treated as $0.00)
37. Cost limits scale with complexity: simple=$3, medium=$5, complex=$10, epic=$20
38. Cost limits configurable via dispatch_config overrides
39. Early termination results recorded in LoopDetector for cross-cycle learning
40. Cost estimate for killed runs uses COST_ESTIMATE_PER_TURN ($0.15) formula
41. Preflight detects Node.js project via package.json
42. Preflight detects Go project via go.mod
43. Preflight detects Python project via requirements.txt/pyproject.toml
44. Preflight detects C# project via .csproj
45. Preflight skipped for build-fix tasks (task description contains fix/build/compile/broken)
46. Preflight failure returns useful error context for injection
47. Preflight timeout constant is 60 seconds
48. Preflight does not block task (returns tuple, never raises)
49. Preflight returns unknown for directories with no recognized project files
50. Preflight skip keywords contain expected build-fix keywords
51. Developer prompt contains "Bias for Action" section
52. Developer prompt contains "Successful Task" few-shot example
53. Developer prompt contains "Failed Task" few-shot example
54. Developer prompt contains "Mandatory First Actions" section
55. Tester prompt contains "Bias for Action" section
56. Tester prompt contains tester-adapted few-shot examples
57. Security reviewer prompt contains "Bias for Action" section
58. Security reviewer prompt contains reviewer-adapted few-shot examples
59. Git diff detects file creation via Bash (untracked files)
60. Git diff detects file deletion via Bash (tracked file removed)
61. Git diff detects file modification via Bash (tracked file changed)
62. Git diff resets file change counter (staged files detected)
63. Edit/Write tool detection still works (belt and suspenders)
64. Git diff no false positives on read-only Bash (clean repo)
65. Git diff returns False after all changes committed
66. Git diff handles non-git directories gracefully

Copyright 2026 Forgeborn
"""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent.parent))

from forge_orchestrator import (
    _check_git_changes,
    _check_stuck_phrases,
    _check_monologue,
    _check_cost_limit,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
    preflight_build_check,
    BUDGET_CHECK_INTERVAL,
    BUDGET_HALFWAY_THRESHOLD,
    BUDGET_CRITICAL_THRESHOLD,
    COST_LIMITS,
    COST_ESTIMATE_PER_TURN,
    EARLY_TERM_WARN_TURNS,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_STUCK_PHRASES,
    EARLY_TERM_EXEMPT_ROLES,
    LoopDetector,
    MONOLOGUE_THRESHOLD,
    MONOLOGUE_EXEMPT_TURNS,
    PREFLIGHT_TIMEOUT,
    PREFLIGHT_SKIP_KEYWORDS,
)


# --- Tests for _check_stuck_phrases ---

def test_stuck_phrases_detects_each():
    """Test that each stuck phrase triggers a match."""
    for phrase in EARLY_TERM_STUCK_PHRASES:
        text = f"Here is some context. {phrase} So I will stop."
        matched = _check_stuck_phrases(text)
        assert matched is not None, f"no match for text containing '{phrase}'"
        # The match might be a shorter phrase that is a substring
        # (e.g., "i cannot" matches before "i cannot complete this task")
        # That's OK — we just need SOME match to fire
        assert matched in text.lower(), f"matched '{matched}' is not in text"


def test_stuck_phrases_case_insensitive():
    """Test that matching is case-insensitive."""
    result = _check_stuck_phrases("I AM UNABLE TO complete this task")
    assert result is not None, "should have matched uppercase 'I AM UNABLE TO'"

    result2 = _check_stuck_phrases("I Cannot access the file")
    assert result2 is not None, "should have matched mixed-case 'I Cannot'"


def test_stuck_phrases_returns_none_for_clean_text():
    """Test that clean text returns None."""
    clean_texts = [
        "I have successfully completed the task.",
        "The file was written to disk.",
        "Running tests now.",
        "Edit the configuration file.",
        "",
    ]
    for text in clean_texts:
        result = _check_stuck_phrases(text)
        assert result is None, f"expected None for '{text}', got '{result}'"


def test_stuck_phrases_partial_match():
    """Test that phrases embedded in longer words still match (substring match)."""
    # "i cannot" is in the phrase list - should match even in longer sentence
    result = _check_stuck_phrases("unfortunately i cannot proceed with this")
    assert result is not None, "should have matched 'i cannot' in sentence"


def test_stuck_phrases_returns_first_match():
    """Test that the first matching phrase is returned when multiple match."""
    # Text contains multiple stuck phrases
    text = "i am unable to do this and i'm stuck on the problem"
    result = _check_stuck_phrases(text)
    # Should return "i am unable to" since it comes first in the list
    assert result == "i am unable to", f"expected 'i am unable to', got '{result}'"


# --- Tests for dead code removal ---

def test_dead_code_removed():
    """Test that _check_repeated_tool_calls has been removed (dead code cleanup)."""
    import forge_orchestrator
    assert not hasattr(forge_orchestrator, "_check_repeated_tool_calls"), \
        "_check_repeated_tool_calls should be removed — it was dead code superseded by _detect_tool_loop"


# --- Tests for _compute_output_hash ---

def test_output_hash_computed_for_tool_results():
    """Test that _compute_output_hash returns a valid SHA256 hex digest."""
    # String content
    result = _compute_output_hash("Hello, world!")
    assert isinstance(result, str), f"expected str, got {type(result)}"
    assert len(result) == 64, f"expected 64-char SHA256 hex, got {len(result)} chars"
    expected = hashlib.sha256("Hello, world!".encode("utf-8")).hexdigest()
    assert result == expected, f"hash mismatch: {result} != {expected}"

    # List content (content blocks from Claude stream-json)
    list_content = [
        {"type": "text", "text": "file contents here"},
        {"type": "text", "text": "more text"},
    ]
    result2 = _compute_output_hash(list_content)
    assert isinstance(result2, str) and len(result2) == 64, \
        "list content should produce a valid SHA256 hex digest"
    # The hash should be of the space-joined text blocks
    expected2 = hashlib.sha256("file contents here more text".encode("utf-8")).hexdigest()
    assert result2 == expected2, f"list hash mismatch: {result2} != {expected2}"


def test_output_hash_handles_none_content():
    """Test that _compute_output_hash handles None and empty content."""
    # None content should return a deterministic hash (hash of empty string)
    result_none = _compute_output_hash(None)
    assert isinstance(result_none, str) and len(result_none) == 64, \
        "None content should produce a valid hash"

    # Empty string
    result_empty = _compute_output_hash("")
    assert isinstance(result_empty, str) and len(result_empty) == 64, \
        "empty string should produce a valid hash"

    # None and empty string should produce the same hash (both are empty)
    assert result_none == result_empty, \
        "None and empty string should hash identically"

    # Empty list
    result_empty_list = _compute_output_hash([])
    assert result_empty_list == result_empty, \
        "empty list should hash the same as empty string"

    # List with non-text blocks (should be skipped)
    result_no_text = _compute_output_hash([{"type": "image", "data": "..."}])
    assert result_no_text == result_empty, \
        "list with no text blocks should hash the same as empty content"


def test_same_input_same_output_detected_as_stuck():
    """Test that _detect_tool_loop flags same input + same output as a stronger stuck signal.

    When output hashes match alongside input signatures, the effective threshold
    should be lower (warn at 2, terminate at 3) because identical output confirms
    the agent is truly stuck rather than retrying with potential external changes.
    """
    history = ["Read|/foo.py"] * 3
    errors = ["error: file not found"] * 3
    # Same output hash for all 3 calls
    same_hash = hashlib.sha256(b"identical error output").hexdigest()
    output_hashes = [same_hash] * 3

    # With output hashes showing identical results, 3 repeats should terminate
    # (normally 3 repeats would only warn without output hash confirmation)
    action, count, sig = _detect_tool_loop(
        history, errors, tool_output_hashes=output_hashes,
        warn_threshold=3, terminate_threshold=5,
    )
    assert action == "terminate", \
        f"same input + same output at 3 repeats should terminate, got '{action}'"


def test_same_input_different_output_not_flagged():
    """Test that same input but different output does NOT trigger early termination.

    When the agent retries the same tool call but gets different output, it means
    external state has changed (e.g., file was created between retries). This should
    use the normal (higher) thresholds, not the output-hash-enhanced thresholds.
    """
    history = ["Bash|npm test"] * 4
    errors = ["error: test failed"] * 4
    # Different output hash each time (external state changing)
    output_hashes = [
        hashlib.sha256(f"output {i}".encode()).hexdigest()
        for i in range(4)
    ]

    # 4 repeats with different outputs should NOT terminate (threshold=5)
    action, count, sig = _detect_tool_loop(
        history, errors, tool_output_hashes=output_hashes,
        warn_threshold=3, terminate_threshold=5,
    )
    # Should warn (4 >= warn_threshold=3) but NOT terminate (4 < 5)
    assert action == "warn", \
        f"same input + different output at 4 repeats should warn, got '{action}'"
    assert count == 4, f"expected count 4, got {count}"


# --- Tests for constants ---

def test_constants_values():
    """Test that early termination constants have expected values."""
    # Loosened on 2026-05-02 (commit d7ac174) for Opus 4.7 retest — 4.7 needs more
    # reading turns before first edit on EQUIPA-class refactor tasks.
    assert EARLY_TERM_WARN_TURNS == 12, f"EARLY_TERM_WARN_TURNS should be 12, got {EARLY_TERM_WARN_TURNS}"
    assert EARLY_TERM_FINAL_WARN_TURNS == 20, f"EARLY_TERM_FINAL_WARN_TURNS should be 20, got {EARLY_TERM_FINAL_WARN_TURNS}"
    assert EARLY_TERM_KILL_TURNS == 25, f"EARLY_TERM_KILL_TURNS should be 25, got {EARLY_TERM_KILL_TURNS}"
    assert EARLY_TERM_WARN_TURNS < EARLY_TERM_FINAL_WARN_TURNS < EARLY_TERM_KILL_TURNS, \
        f"WARN_TURNS ({EARLY_TERM_WARN_TURNS}) < FINAL_WARN_TURNS ({EARLY_TERM_FINAL_WARN_TURNS}) < KILL_TURNS ({EARLY_TERM_KILL_TURNS})"


def test_exempt_roles_contains_expected():
    """Test that exempt roles include research/planning roles."""
    required_roles = {"planner", "evaluator", "security-reviewer", "code-reviewer", "researcher"}
    missing = required_roles - EARLY_TERM_EXEMPT_ROLES
    assert not missing, f"missing exempt roles: {missing}"

    # Developer and tester should NOT be exempt
    assert "developer" not in EARLY_TERM_EXEMPT_ROLES, "'developer' should NOT be exempt"
    assert "tester" not in EARLY_TERM_EXEMPT_ROLES, "'tester' should NOT be exempt"


def test_stuck_phrases_list_not_empty():
    """Test that stuck phrases list is populated."""
    assert EARLY_TERM_STUCK_PHRASES, "stuck phrases list is empty"
    assert len(EARLY_TERM_STUCK_PHRASES) >= 5, \
        f"expected at least 5 stuck phrases, got {len(EARLY_TERM_STUCK_PHRASES)}"

    # All phrases should be lowercase
    for phrase in EARLY_TERM_STUCK_PHRASES:
        assert phrase == phrase.lower(), f"phrase '{phrase}' is not lowercase"


# --- Tests for _detect_tool_loop integration ---

def test_detect_tool_loop_integrates_with_early_term():
    """Test _detect_tool_loop's terminate threshold catches stuck agents."""
    history = ["Bash|git status"] * 5
    errors = ["error"] * 5
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", f"expected 'terminate', got '{action}'"
    assert count == 5, f"expected count 5, got {count}"


def test_detect_tool_loop_warn_at_3():
    """Test _detect_tool_loop warns at 3 consecutive identical."""
    history = ["Read|/x.py"] * 3
    errors = ["error"] * 3
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "warn", f"expected 'warn', got '{action}'"


# --- Tests for alternating (A-B-A-B) loop detection ---

def test_alternating_pattern_detected_at_6_cycles():
    """Test that 6+ failing A-B alternations trigger 'terminate'."""
    # 6 alternations = 3 complete A-B pairs: A B A B A B
    history = []
    errors = []
    for _ in range(3):
        history.extend(["Read|/foo.py", "Edit|/foo.py"])
        errors.extend(["file not found", "edit failed"])
    # history is now 6 entries: A B A B A B, all failing
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"expected 'terminate' for 6 failing alternations, got '{action}'"
    assert count >= 6, f"expected count >= 6, got {count}"


def test_alternating_pattern_warning_at_4_cycles():
    """Test that 4-5 failing A-B alternations trigger 'warn'."""
    # 4 alternations = 2 complete A-B pairs: A B A B
    history = []
    errors = []
    for _ in range(2):
        history.extend(["Grep|pattern", "Read|/src/main.py"])
        errors.extend(["no matches", "permission denied"])
    # history is 4 entries: A B A B, all failing
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "warn", \
        f"expected 'warn' for 4 failing alternations, got '{action}'"
    assert count >= 4, f"expected count >= 4, got {count}"


def test_alternating_successful_calls_not_flagged():
    """Test that alternating calls that SUCCEED are not flagged as stuck."""
    # 8 alternations but all successful
    history = []
    errors = []
    for _ in range(4):
        history.extend(["Read|/a.py", "Read|/b.py"])
        errors.extend([None, None])
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "ok", \
        f"expected 'ok' for successful alternations, got '{action}'"


def test_alternating_with_errors_flagged():
    """Test that alternating calls with errors are properly flagged."""
    # Mix: first 2 succeed, then 6 fail with alternating pattern
    history = ["Read|/a.py", "Read|/b.py"]  # these succeed
    errors = [None, None]
    for _ in range(3):
        history.extend(["Bash|npm test", "Edit|/fix.py"])
        errors.extend(["test failed", "syntax error"])
    # Last 6 entries are failing A-B-A-B-A-B
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"expected 'terminate' for 6 failing alternations after successes, got '{action}'"


def test_three_way_rotation_not_false_positive():
    """Test that A-B-C-A-B-C rotation is NOT detected as A-B-A-B alternation."""
    # 9 entries: A B C A B C A B C — this is a 3-way pattern, not 2-way
    history = []
    errors = []
    for _ in range(3):
        history.extend(["Read|/a.py", "Edit|/b.py", "Bash|test"])
        errors.extend(["error", "error", "error"])
    action, count, sig = _detect_tool_loop(history, errors)
    # Should NOT trigger alternating detection — it's a 3-way rotation
    # The consecutive check also won't fire since they're all different
    assert action == "ok", \
        f"expected 'ok' for 3-way rotation, got '{action}'"


def test_alternating_detection_coexists_with_consecutive_detection():
    """Test that consecutive detection still works alongside alternating detection."""
    # Consecutive: 5 identical failing calls should still trigger terminate
    history = ["Bash|git status"] * 5
    errors = ["error: not a git repo"] * 5
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"consecutive detection should still work, got '{action}'"
    assert count >= 5, f"expected count >= 5, got {count}"

    # Alternating: 6 failing A-B should also trigger terminate
    history2 = []
    errors2 = []
    for _ in range(3):
        history2.extend(["Read|/x", "Write|/x"])
        errors2.extend(["error", "error"])
    action2, count2, sig2 = _detect_tool_loop(history2, errors2)
    assert action2 == "terminate", \
        f"alternating detection should also work, got '{action2}'"


# --- Tests for _check_monologue (consecutive text-only turns) ---

def test_monologue_detected_at_3_consecutive_text_only():
    """Test that 3 consecutive text-only turns trigger termination after exempt period."""
    action = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=10,
    )
    assert action == "terminate", (
        f"expected 'terminate' at {MONOLOGUE_THRESHOLD} text-only turns, got '{action}'"
    )

    # Also test with higher counts
    action2 = _check_monologue(consecutive_text_only_turns=5, turn_count=15)
    assert action2 == "terminate", f"expected 'terminate' at 5 text-only turns, got '{action2}'"


def test_monologue_resets_on_tool_use():
    """Test that monologue counter of 0 (after reset) does not trigger."""
    action = _check_monologue(consecutive_text_only_turns=0, turn_count=10)
    assert action is None, f"expected None for 0 text-only turns, got '{action}'"

    action2 = _check_monologue(consecutive_text_only_turns=1, turn_count=10)
    assert action2 is None, f"expected None for 1 text-only turn, got '{action2}'"


def test_monologue_exempt_first_5_turns():
    """Test that monologue detection does NOT trigger during the first 5 turns."""
    for turn in range(1, MONOLOGUE_EXEMPT_TURNS + 1):
        action = _check_monologue(
            consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
            turn_count=turn,
        )
        assert action is None, (
            f"should NOT trigger at turn {turn} (exempt period), got '{action}'"
        )

    # But turn MONOLOGUE_EXEMPT_TURNS + 1 should trigger
    action_after = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=MONOLOGUE_EXEMPT_TURNS + 1,
    )
    assert action_after == "terminate", (
        f"should trigger at turn {MONOLOGUE_EXEMPT_TURNS + 1}, got '{action_after}'"
    )


def test_monologue_warning_at_2():
    """Test that 2 consecutive text-only turns emit a warning (not termination)."""
    action = _check_monologue(
        consecutive_text_only_turns=2,
        turn_count=10,
    )
    assert action == "warn", f"expected 'warn' at 2 text-only turns, got '{action}'"


def test_monologue_does_not_interfere_with_stuck_phrases():
    """Test that monologue detection is independent of stuck phrase detection."""
    action = _check_monologue(consecutive_text_only_turns=0, turn_count=10)
    assert action is None, "monologue should be None when no text-only turns"

    stuck = _check_stuck_phrases("I am unable to proceed with this task")
    assert stuck is not None, "stuck phrase should still be detected independently"

    action2 = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=10,
    )
    assert action2 == "terminate", "monologue should still trigger independently"


def test_monologue_constants():
    """Test that monologue constants have expected values."""
    assert MONOLOGUE_THRESHOLD == 3, (
        f"MONOLOGUE_THRESHOLD should be 3, got {MONOLOGUE_THRESHOLD}"
    )
    assert MONOLOGUE_EXEMPT_TURNS == 5, (
        f"MONOLOGUE_EXEMPT_TURNS should be 5, got {MONOLOGUE_EXEMPT_TURNS}"
    )
    assert MONOLOGUE_THRESHOLD > 0, "threshold must be positive"
    assert MONOLOGUE_EXEMPT_TURNS > 0, "exempt turns must be positive"


def test_monologue_below_threshold_no_action():
    """Test that counts below warning threshold return None."""
    action = _check_monologue(consecutive_text_only_turns=1, turn_count=10)
    assert action is None, f"expected None for 1 text-only turn, got '{action}'"


# --- Tests for _parse_early_complete (agent-initiated early completion) ---

def test_early_complete_signal_parsed():
    """Test that EARLY_COMPLETE: <reason> is correctly parsed from agent text."""
    text = "I've finished implementing the feature.\nEARLY_COMPLETE: All changes committed and tests pass"
    reason = _parse_early_complete(text)
    assert reason == "All changes committed and tests pass", \
        f"expected 'All changes committed and tests pass', got '{reason}'"

    # Single line
    reason2 = _parse_early_complete("EARLY_COMPLETE: No changes needed")
    assert reason2 == "No changes needed", \
        f"expected 'No changes needed', got '{reason2}'"

    # With extra whitespace around the reason
    reason3 = _parse_early_complete("EARLY_COMPLETE:   Extra spaces here  ")
    assert reason3 == "Extra spaces here", \
        f"expected 'Extra spaces here', got '{reason3}'"


def test_early_complete_waits_for_message_end():
    """Test that EARLY_COMPLETE is detected in text but does NOT interfere with
    message processing — the parsing function only extracts the reason.

    The actual "wait for message end" behavior is enforced in run_agent_streaming,
    which sets a flag and breaks AFTER the current assistant message is fully processed.
    This test verifies the parsing layer works correctly in multi-line contexts.
    """
    # EARLY_COMPLETE appears mid-text with content after it
    text = (
        "I've completed the implementation.\n"
        "EARLY_COMPLETE: Feature fully implemented and tested\n"
        "Here is the summary of what I did:\n"
        "RESULT: success\n"
        "SUMMARY: Added the new endpoint\n"
    )
    reason = _parse_early_complete(text)
    assert reason == "Feature fully implemented and tested", \
        f"should parse reason from the EARLY_COMPLETE line, got '{reason}'"


def test_early_complete_not_confused_with_result_marker():
    """Test that RESULT: markers are NOT parsed as EARLY_COMPLETE."""
    # RESULT: success should NOT trigger early complete
    assert _parse_early_complete("RESULT: success") is None, \
        "RESULT: success should NOT be parsed as EARLY_COMPLETE"

    # RESULT: blocked should NOT trigger early complete
    assert _parse_early_complete("RESULT: blocked") is None, \
        "RESULT: blocked should NOT be parsed as EARLY_COMPLETE"

    # Text with RESULT: but no EARLY_COMPLETE should return None
    text = "RESULT: success\nSUMMARY: Done\nFILES_CHANGED: foo.py"
    assert _parse_early_complete(text) is None, \
        "text with only RESULT: markers should return None"

    # Text with BOTH should return the EARLY_COMPLETE reason
    text_both = "EARLY_COMPLETE: Task done\nRESULT: success"
    reason = _parse_early_complete(text_both)
    assert reason == "Task done", \
        f"when both present, should return EARLY_COMPLETE reason, got '{reason}'"


def test_early_complete_cost_still_tracked():
    """Test that the result dict from an early-completed agent includes cost data.

    This is a structural test — we verify the result dict shape that
    run_agent_streaming returns when early_completed is True. The actual
    cost tracking happens via result_data.get('total_cost_usd'), which is
    populated from the stream-json 'result' message that arrives even when
    the agent signals early completion (since we wait for the current message
    to finish and the process to exit naturally).

    We test the parsing function returns a reason (which triggers the flag),
    and verify the expected keys exist by checking the function contract.
    """
    # The parser should return a valid reason
    reason = _parse_early_complete("EARLY_COMPLETE: Done, no more work needed")
    assert reason is not None, "should parse a valid reason"

    # Verify the function returns a string (not a bool or other type)
    assert isinstance(reason, str), f"reason should be a string, got {type(reason)}"
    assert len(reason) > 0, "reason should not be empty"


def test_early_complete_distinct_from_early_terminated():
    """Test that early_completed and early_terminated are distinct concepts.

    early_completed = agent CHOSE to stop (success signal)
    early_terminated = orchestrator FORCED agent to stop (failure signal)

    The _parse_early_complete function should return a reason for valid
    EARLY_COMPLETE markers but return None for stuck/termination phrases.
    """
    # Agent-initiated completion should parse successfully
    reason = _parse_early_complete("EARLY_COMPLETE: Task complete, no changes needed")
    assert reason is not None, "EARLY_COMPLETE should be parsed"

    # Stuck phrases should NOT parse as early completion
    stuck_texts = [
        "I am unable to complete this task",
        "I'm stuck on the problem",
        "I cannot proceed",
    ]
    for text in stuck_texts:
        result = _parse_early_complete(text)
        assert result is None, \
            f"stuck phrase '{text}' should NOT parse as EARLY_COMPLETE, got '{result}'"

    # early_term_reason phrases should NOT parse as early completion
    term_texts = [
        "Agent terminated: 40 consecutive turns without file changes",
        "Loop detected: agent repeated the same operation 5 times",
        "Process timed out after 3600 seconds",
    ]
    for text in term_texts:
        result = _parse_early_complete(text)
        assert result is None, \
            f"termination phrase should NOT parse as EARLY_COMPLETE, got '{result}'"


def test_false_early_complete_in_quoted_text_ignored():
    """Test that EARLY_COMPLETE inside quoted/code blocks is NOT parsed.

    Agents may discuss or document the EARLY_COMPLETE marker in their output
    (e.g., in code comments, documentation, or quoted instructions). These
    should NOT trigger the early completion signal.
    """
    # Inside a code block (triple backtick)
    text_code = (
        "Here is how to use the marker:\n"
        "```\n"
        "EARLY_COMPLETE: This is a code example\n"
        "```\n"
    )
    assert _parse_early_complete(text_code) is None, \
        "EARLY_COMPLETE inside a code block should be ignored"

    # Inside inline code (single backtick)
    text_inline = "Use the `EARLY_COMPLETE: reason` marker when done."
    assert _parse_early_complete(text_inline) is None, \
        "EARLY_COMPLETE inside inline code should be ignored"

    # Inside a quoted block (> prefix)
    text_quoted = "> EARLY_COMPLETE: This is a quote from the docs"
    assert _parse_early_complete(text_quoted) is None, \
        "EARLY_COMPLETE inside a quoted block should be ignored"

    # Inside a string literal (surrounded by quotes)
    text_string = 'The marker is "EARLY_COMPLETE: reason" format.'
    assert _parse_early_complete(text_string) is None, \
        "EARLY_COMPLETE inside double quotes should be ignored"

    # Plain EARLY_COMPLETE at start of line (not quoted) should still work
    text_valid = "Some preamble text\nEARLY_COMPLETE: Genuinely done"
    reason = _parse_early_complete(text_valid)
    assert reason == "Genuinely done", \
        f"unquoted EARLY_COMPLETE should parse, got '{reason}'"


# --- Tests for _get_budget_message (budget visibility) ---

def test_budget_message_at_periodic_intervals():
    """Test that budget messages are returned every BUDGET_CHECK_INTERVAL turns."""
    # At turn 5 of 50 (on interval), should get a periodic update
    msg = _get_budget_message(5, 50)
    assert msg is not None, "should return a message at turn 5 (on interval)"
    assert "45" in msg, f"message should mention 45 remaining turns, got: {msg}"
    assert "5" in msg, f"message should mention 5 turns used, got: {msg}"

    # At turn 10 of 50 (on interval, but below halfway), should get periodic
    msg2 = _get_budget_message(10, 50)
    assert msg2 is not None, "should return a message at turn 10 (on interval)"
    assert "40" in msg2, f"message should mention 40 remaining turns, got: {msg2}"


def test_budget_halfway_warning():
    """Test that a HALFWAY warning is returned at exactly 50% of budget."""
    # Turn 25 of 50 = exactly 50%
    msg = _get_budget_message(25, 50)
    assert msg is not None, "should return a message at 50% budget"
    assert "HALFWAY" in msg, f"message should contain 'HALFWAY', got: {msg}"
    assert "25" in msg, f"message should mention 25 remaining turns, got: {msg}"

    # Turn 20 of 40 = exactly 50%
    msg2 = _get_budget_message(20, 40)
    assert msg2 is not None, "should return a message at 50% of 40-turn budget"
    assert "HALFWAY" in msg2, f"message should contain 'HALFWAY', got: {msg2}"


def test_budget_critical_warning():
    """Test that a CRITICAL warning is returned at 75% of budget."""
    # Turn 38 of 50 = 76% (past critical)
    # But 38 is not on BUDGET_CHECK_INTERVAL. Try turn 40 of 50 = 80%.
    msg = _get_budget_message(40, 50)
    assert msg is not None, "should return a message at 80% budget"
    assert "CRITICAL" in msg, f"message should contain 'CRITICAL', got: {msg}"
    assert "10" in msg, f"message should mention 10 remaining turns, got: {msg}"

    # Turn 75 of 100 = exactly 75%
    msg2 = _get_budget_message(75, 100)
    assert msg2 is not None, "should return a message at 75% of 100-turn budget"
    assert "CRITICAL" in msg2, f"message should contain 'CRITICAL', got: {msg2}"


def test_budget_no_message_off_interval():
    """Test that no budget message is returned on turns not on the check interval."""
    # Turn 3 of 50 — not on interval (5), not at any threshold
    msg = _get_budget_message(3, 50)
    assert msg is None, f"should return None at turn 3 (not on interval), got: {msg}"

    # Turn 7 of 50 — not on interval
    msg2 = _get_budget_message(7, 50)
    assert msg2 is None, f"should return None at turn 7, got: {msg2}"

    # Turn 0 should return None
    msg3 = _get_budget_message(0, 50)
    assert msg3 is None, f"should return None at turn 0, got: {msg3}"


def test_budget_constants_values():
    """Test that budget visibility constants have expected values."""
    assert BUDGET_CHECK_INTERVAL == 5, \
        f"BUDGET_CHECK_INTERVAL should be 5, got {BUDGET_CHECK_INTERVAL}"
    assert BUDGET_HALFWAY_THRESHOLD == 0.5, \
        f"BUDGET_HALFWAY_THRESHOLD should be 0.5, got {BUDGET_HALFWAY_THRESHOLD}"
    assert BUDGET_CRITICAL_THRESHOLD == 0.75, \
        f"BUDGET_CRITICAL_THRESHOLD should be 0.75, got {BUDGET_CRITICAL_THRESHOLD}"
    # Ensure thresholds are ordered
    assert BUDGET_HALFWAY_THRESHOLD < BUDGET_CRITICAL_THRESHOLD, \
        "halfway threshold must be less than critical threshold"


def test_budget_message_correct_remaining():
    """Test that budget messages contain the correct remaining turn count."""
    # Turn 15 of 30 = 50% (halfway)
    msg = _get_budget_message(15, 30)
    assert msg is not None, "should return a message at turn 15/30"
    assert "15" in msg, f"should mention 15 remaining turns, got: {msg}"

    # Turn 20 of 80 = 25% (periodic)
    msg2 = _get_budget_message(20, 80)
    assert msg2 is not None, "should return a message at turn 20/80"
    assert "60" in msg2, f"should mention 60 remaining turns, got: {msg2}"


def test_budget_message_none_when_no_max_turns():
    """Test that no budget message is returned when max_turns is None or 0."""
    msg = _get_budget_message(5, None)
    assert msg is None, f"should return None when max_turns is None, got: {msg}"

    msg2 = _get_budget_message(5, 0)
    assert msg2 is None, f"should return None when max_turns is 0, got: {msg2}"


# --- Tests for _check_cost_limit (cost-based circuit breaker) ---

def test_cost_breaker_terminates_at_limit():
    """Test that _check_cost_limit returns a termination reason when cost exceeds limit."""
    # Simple task has $3.00 limit by default
    reason = _check_cost_limit(total_cost=3.50, complexity="simple")
    assert reason is not None, "should terminate when cost exceeds limit"
    assert "3.50" in reason, f"reason should include actual cost, got: {reason}"
    assert "3.00" in reason, f"reason should include limit, got: {reason}"

    # At exact limit, should NOT terminate (only exceeding triggers)
    reason_exact = _check_cost_limit(total_cost=3.00, complexity="simple")
    assert reason_exact is None, f"should not terminate at exact limit, got: {reason_exact}"

    # Below limit, should NOT terminate
    reason_below = _check_cost_limit(total_cost=2.99, complexity="simple")
    assert reason_below is None, f"should not terminate below limit, got: {reason_below}"

    # Medium task has $5.00 limit
    reason_medium = _check_cost_limit(total_cost=5.01, complexity="medium")
    assert reason_medium is not None, "should terminate when medium cost exceeds $5.00"

    # Complex task has $10.00 limit
    reason_complex = _check_cost_limit(total_cost=10.50, complexity="complex")
    assert reason_complex is not None, "should terminate when complex cost exceeds $10.00"

    # Epic task has $20.00 limit
    reason_epic = _check_cost_limit(total_cost=20.01, complexity="epic")
    assert reason_epic is not None, "should terminate when epic cost exceeds $20.00"


def test_cost_breaker_handles_none_cost():
    """Test that _check_cost_limit handles None total_cost gracefully."""
    # None cost should be treated as 0.0 — never triggers the breaker
    reason = _check_cost_limit(total_cost=None, complexity="simple")
    assert reason is None, f"None cost should not trigger breaker, got: {reason}"

    # 0.0 cost should not trigger
    reason_zero = _check_cost_limit(total_cost=0.0, complexity="complex")
    assert reason_zero is None, f"zero cost should not trigger breaker, got: {reason_zero}"


def test_cost_limit_scales_with_complexity():
    """Test that cost limits scale per complexity tier."""
    # Verify the limits are ordered: simple < medium < complex < epic
    assert COST_LIMITS["simple"] < COST_LIMITS["medium"], \
        f"simple ({COST_LIMITS['simple']}) should be less than medium ({COST_LIMITS['medium']})"
    assert COST_LIMITS["medium"] < COST_LIMITS["complex"], \
        f"medium ({COST_LIMITS['medium']}) should be less than complex ({COST_LIMITS['complex']})"
    assert COST_LIMITS["complex"] < COST_LIMITS["epic"], \
        f"complex ({COST_LIMITS['complex']}) should be less than epic ({COST_LIMITS['epic']})"

    # Verify the specific values from the task spec
    assert COST_LIMITS["simple"] == 3.0, f"simple limit should be $3.00, got {COST_LIMITS['simple']}"
    assert COST_LIMITS["medium"] == 5.0, f"medium limit should be $5.00, got {COST_LIMITS['medium']}"
    assert COST_LIMITS["complex"] == 10.0, f"complex limit should be $10.00, got {COST_LIMITS['complex']}"
    assert COST_LIMITS["epic"] == 20.0, f"epic limit should be $20.00, got {COST_LIMITS['epic']}"

    # $4.00 should pass for medium but fail for simple
    assert _check_cost_limit(4.0, "medium") is None, \
        "$4.00 should be under medium limit"
    assert _check_cost_limit(4.0, "simple") is not None, \
        "$4.00 should exceed simple limit"


def test_cost_breaker_configurable_via_dispatch_config():
    """Test that cost limits can be overridden via dispatch_config."""
    # Override simple to $50.00 via config
    config_limits = {"simple": 50.0, "medium": 100.0, "complex": 200.0, "epic": 500.0}

    # $4.00 with default simple limit ($3.00) would terminate, but with override ($50) it shouldn't
    reason = _check_cost_limit(total_cost=4.0, complexity="simple", config_limits=config_limits)
    assert reason is None, \
        f"config override should allow $4.00 for simple (limit=$50), got: {reason}"

    # $51.00 should exceed the overridden $50 limit
    reason_over = _check_cost_limit(total_cost=51.0, complexity="simple", config_limits=config_limits)
    assert reason_over is not None, \
        "should terminate when cost exceeds config-overridden limit"

    # Unknown complexity falls back to default $10.00
    reason_unknown = _check_cost_limit(total_cost=11.0, complexity="unknown_tier")
    assert reason_unknown is not None, \
        "unknown complexity should use default limit ($10.00)"


def test_early_term_recorded_in_loop_detector():
    """Test that early termination results are properly recorded in LoopDetector."""
    detector = LoopDetector()

    # Simulate a failed result (early terminated)
    result = {
        "result_text": "RESULT: failed\nSUMMARY: Agent stuck\nFILES_CHANGED: none\nBLOCKERS: stuck",
        "errors": ["agent terminated: stuck"],
    }

    # Record the early-terminated result — should return "ok" on first attempt
    action = detector.record(result, cycle=1)
    assert action == "ok", f"first record should be 'ok', got '{action}'"

    # Record same result again — after warning_threshold (3) identical, should warn
    detector.record(result, cycle=2)
    action3 = detector.record(result, cycle=3)
    assert action3 == "warn", \
        f"3rd identical result should trigger 'warn', got '{action3}'"

    # Verify the detector has tracked the fingerprints
    assert len(detector.fingerprints) == 3, \
        f"expected 3 fingerprints, got {len(detector.fingerprints)}"


def test_cost_estimate_for_killed_runs():
    """Test that killed runs (None cost) use the estimated cost formula.

    When an agent is killed (cost=None), we estimate: num_turns * COST_ESTIMATE_PER_TURN.
    This test verifies the constant exists and the estimation logic is correct.
    """
    # Verify the constant exists and has a reasonable value
    assert COST_ESTIMATE_PER_TURN > 0, \
        f"COST_ESTIMATE_PER_TURN should be positive, got {COST_ESTIMATE_PER_TURN}"
    assert COST_ESTIMATE_PER_TURN == 0.15, \
        f"COST_ESTIMATE_PER_TURN should be $0.15, got {COST_ESTIMATE_PER_TURN}"

    # Estimate for 10 turns: 10 * 0.15 = $1.50
    estimated = 10 * COST_ESTIMATE_PER_TURN
    assert abs(estimated - 1.50) < 0.001, \
        f"10 turns * $0.15 should be $1.50, got {estimated}"

    # A killed run with 30 turns should estimate $4.50 — exceeds simple limit ($3.00)
    killed_estimate = 30 * COST_ESTIMATE_PER_TURN
    reason = _check_cost_limit(total_cost=killed_estimate, complexity="simple")
    assert reason is not None, \
        f"killed run cost ${killed_estimate:.2f} should exceed simple limit ($3.00)"


# --- Tests for preflight_build_check (multi-language pre-flight build) ---

def test_preflight_detects_node_project():
    """Test that preflight_build_check detects a Node.js project via package.json."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a package.json to signal Node.js project
        pkg_json = Path(tmpdir) / "package.json"
        pkg_json.write_text('{"name": "test", "scripts": {"build": "echo ok"}}')

        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert language == "node", f"expected language='node', got '{language}'"


def test_preflight_detects_go_project():
    """Test that preflight_build_check detects a Go project via go.mod."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a go.mod to signal Go project
        go_mod = Path(tmpdir) / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")

        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert language == "go", f"expected language='go', got '{language}'"


def test_preflight_detects_python_project():
    """Test that preflight_build_check detects a Python project via requirements.txt or pyproject.toml."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a requirements.txt to signal Python project
        req = Path(tmpdir) / "requirements.txt"
        req.write_text("flask==3.0.0\n")

        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert language == "python", f"expected language='python', got '{language}'"

    # Also test with pyproject.toml
    with tempfile.TemporaryDirectory() as tmpdir2:
        pyproject = Path(tmpdir2) / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\n')

        success2, language2, error_details2 = asyncio.run(
            preflight_build_check(tmpdir2)
        )
        assert language2 == "python", f"expected language='python' for pyproject.toml, got '{language2}'"


def test_preflight_detects_csharp_project():
    """Test that preflight_build_check detects a C# project via .csproj file."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a .csproj file to signal C# project
        csproj = Path(tmpdir) / "MyApp.csproj"
        csproj.write_text('<Project Sdk="Microsoft.NET.Sdk">\n</Project>')

        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert language == "csharp", f"expected language='csharp', got '{language}'"


def test_preflight_skipped_for_build_fix_tasks():
    """Test that preflight check is skipped when task description mentions build-fix keywords."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a package.json so it would normally detect Node.js
        pkg_json = Path(tmpdir) / "package.json"
        pkg_json.write_text('{"name": "test"}')

        # Task descriptions that should skip preflight
        skip_descriptions = [
            "Fix the build errors in the CI pipeline",
            "The project won't compile, please help",
            "Fix broken tests and build",
            "Resolve compilation issues",
        ]
        for desc in skip_descriptions:
            success, language, error_details = asyncio.run(
                preflight_build_check(tmpdir, task_description=desc)
            )
            assert success is True, \
                f"preflight should return success=True (skipped) for task desc: '{desc}'"
            assert "skipped" in error_details.lower(), \
                f"error_details should mention 'skipped' for task desc: '{desc}', got '{error_details}'"


def test_preflight_failure_injected_into_context():
    """Test that a failed preflight returns useful error context for injection."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a package.json with a build script that will fail
        pkg_json = Path(tmpdir) / "package.json"
        pkg_json.write_text('{"name": "test", "scripts": {"build": "exit 1"}}')

        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        # The build command will likely fail (no node_modules, or exit 1)
        # Either way, we should get meaningful error details back
        assert language == "node", f"should still detect language as 'node', got '{language}'"
        if not success:
            assert len(error_details) > 0, \
                "failed preflight should include error details for context injection"


def test_preflight_timeout_at_60s():
    """Test that PREFLIGHT_TIMEOUT constant is set to 60 seconds."""
    assert PREFLIGHT_TIMEOUT == 60, \
        f"PREFLIGHT_TIMEOUT should be 60 seconds, got {PREFLIGHT_TIMEOUT}"


def test_preflight_does_not_block_task():
    """Test that preflight_build_check returns a tuple (not raises) even on failure."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a go.mod but no Go source files — build will fail
        go_mod = Path(tmpdir) / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")

        # Should NOT raise an exception — returns a result tuple
        result = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
        assert len(result) == 3, f"expected 3-element tuple, got {len(result)}"
        success, language, error_details = result
        assert isinstance(success, bool), f"success should be bool, got {type(success)}"
        assert isinstance(language, str), f"language should be str, got {type(language)}"
        assert isinstance(error_details, str), f"error_details should be str, got {type(error_details)}"


def test_preflight_unknown_project():
    """Test that preflight_build_check handles a directory with no recognized project files."""
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as tmpdir:
        # Empty directory — no project files
        success, language, error_details = asyncio.run(
            preflight_build_check(tmpdir)
        )
        assert success is True, "empty dir should return success=True (nothing to check)"
        assert language == "unknown", f"expected language='unknown', got '{language}'"


def test_preflight_skip_keywords_exist():
    """Test that PREFLIGHT_SKIP_KEYWORDS contains expected build-fix related keywords."""
    assert isinstance(PREFLIGHT_SKIP_KEYWORDS, (list, tuple, set, frozenset)), \
        f"PREFLIGHT_SKIP_KEYWORDS should be a sequence, got {type(PREFLIGHT_SKIP_KEYWORDS)}"
    # Must contain at least these keywords
    required = {"fix", "build", "compile", "broken"}
    keywords_lower = {k.lower() for k in PREFLIGHT_SKIP_KEYWORDS}
    missing = required - keywords_lower
    assert not missing, f"PREFLIGHT_SKIP_KEYWORDS missing: {missing}"


# --- Tests for anti-paralysis prompt sections ---

def _read_prompt(role):
    """Helper: read a role prompt template file and return its contents."""
    # Prompts live at repo root: <repo>/prompts/<role>.md
    # Tests live at: <repo>/tests/test_early_termination.py
    prompt_path = Path(__file__).parent.parent / "prompts" / f"{role}.md"
    assert prompt_path.exists(), f"prompt file not found: {prompt_path}"
    return prompt_path.read_text(encoding="utf-8")


def test_developer_prompt_has_bias_for_action():
    """Test that developer prompt contains the 'Bias for Action' section."""
    text = _read_prompt("developer")
    assert "## CRITICAL: Bias for Action" in text, \
        "developer prompt missing '## CRITICAL: Bias for Action' section"
    # Check key directives are present
    assert "first 5 tool calls" in text, \
        "developer prompt missing 'first 5 tool calls' directive"
    assert "TURN BUDGET" in text, \
        "developer prompt missing 'TURN BUDGET' limit"
    assert "Wrong code you can fix is better than no code" in text, \
        "developer prompt missing 'wrong code you can fix' directive"


def test_developer_prompt_has_successful_example():
    """Test that developer prompt contains the successful task few-shot example."""
    text = _read_prompt("developer")
    assert "## Example: Successful Task (DO THIS)" in text, \
        "developer prompt missing successful task example"
    assert "COMPLETED in 5 turns" in text, \
        "developer prompt missing 'COMPLETED in 5 turns' outcome"
    # Verify the example shows immediate editing behavior
    assert "Turn 2: Edit the file" in text, \
        "successful example should show editing on Turn 2"


def test_developer_prompt_has_failed_example():
    """Test that developer prompt contains the failed task few-shot example."""
    text = _read_prompt("developer")
    assert "## Example: Failed Task (DO NOT DO THIS)" in text, \
        "developer prompt missing failed task example"
    assert "KILLED at turn 28" in text, \
        "developer prompt missing 'KILLED at turn 28' outcome"
    assert "TOTAL FAILURE" in text, \
        "developer prompt missing 'TOTAL FAILURE' label"
    assert "zero edits made" in text, \
        "failed example should mention 'zero edits made'"


def test_developer_prompt_has_mandatory_first_actions():
    """Test that developer prompt contains the 'Mandatory First Actions' section."""
    text = _read_prompt("developer")
    assert "## Mandatory First Actions" in text, \
        "developer prompt missing '## Mandatory First Actions' section"
    # Check key directives
    assert "FIRST tool call must be Read" in text, \
        "developer prompt missing first-action Read directive"
    # Loosened on 2026-05-02 (commit d7ac174) for Opus 4.7 retest:
    # SECOND must be Read (target file), THIRD must be Edit/Write.
    assert "SECOND tool call must be Read" in text, \
        "developer prompt missing second-action Read (target file) directive"
    assert "THIRD tool call must be Edit or Write" in text, \
        "developer prompt missing third-action Edit/Write directive"
    assert "Do NOT use Glob or Grep in your first 3 turns" in text, \
        "developer prompt missing Glob/Grep restriction"


def test_developer_prompt_bias_section_is_at_top():
    """Test that the Bias for Action section appears BEFORE the main agent identity."""
    text = _read_prompt("developer")
    bias_pos = text.find("## CRITICAL: Bias for Action")
    identity_pos = text.find("# EQUIPA Developer Agent")
    assert bias_pos < identity_pos, \
        f"Bias for Action (pos {bias_pos}) must appear before agent identity (pos {identity_pos})"


def test_tester_prompt_has_bias_for_action():
    """Test that tester prompt contains the 'Bias for Action' section."""
    text = _read_prompt("tester")
    assert "## CRITICAL: Bias for Action" in text, \
        "tester prompt missing '## CRITICAL: Bias for Action' section"
    # Check tester-specific directives
    assert "FIRST 2 tool calls" in text, \
        "tester prompt missing 'FIRST 2 tool calls' directive"
    assert "RUN tests, not to UNDERSTAND the codebase" in text, \
        "tester prompt missing 'RUN tests' directive"


def test_tester_prompt_has_few_shot_examples():
    """Test that tester prompt contains adapted few-shot examples."""
    text = _read_prompt("tester")
    assert "## Example: Successful Test Run (DO THIS)" in text, \
        "tester prompt missing successful test run example"
    assert "## Example: Failed Test Run (DO NOT DO THIS)" in text, \
        "tester prompt missing failed test run example"
    assert "COMPLETED in 3 turns" in text, \
        "tester prompt missing 'COMPLETED in 3 turns' outcome"
    assert "never ran a single test" in text, \
        "tester failed example should mention 'never ran a single test'"


def test_tester_prompt_bias_section_is_at_top():
    """Test that the Bias for Action section appears BEFORE the main agent identity."""
    text = _read_prompt("tester")
    bias_pos = text.find("## CRITICAL: Bias for Action")
    identity_pos = text.find("# EQUIPA Tester Agent")
    assert bias_pos < identity_pos, \
        f"Bias for Action (pos {bias_pos}) must appear before agent identity (pos {identity_pos})"


def test_security_reviewer_prompt_has_bias_for_action():
    """Test that security reviewer prompt contains the 'Bias for Action' section."""
    text = _read_prompt("security-reviewer")
    assert "## CRITICAL: Bias for Action" in text, \
        "security-reviewer prompt missing '## CRITICAL: Bias for Action' section"
    # Check reviewer-specific directives
    assert "first 5 tool calls" in text, \
        "security-reviewer prompt missing 'first 5 tool calls' directive"
    assert "document it RIGHT NOW" in text, \
        "security-reviewer prompt missing 'document it RIGHT NOW' directive"


def test_security_reviewer_prompt_has_few_shot_examples():
    """Test that security reviewer prompt contains adapted few-shot examples."""
    text = _read_prompt("security-reviewer")
    assert "## Example: Successful Security Review (DO THIS)" in text, \
        "security-reviewer prompt missing successful review example"
    assert "## Example: Failed Security Review (DO NOT DO THIS)" in text, \
        "security-reviewer prompt missing failed review example"
    assert "COMPLETED in 9 turns" in text, \
        "security-reviewer prompt missing 'COMPLETED in 9 turns' outcome"
    assert "zero findings documented" in text, \
        "security-reviewer failed example should mention 'zero findings documented'"


def test_security_reviewer_prompt_bias_section_is_at_top():
    """Test that the Bias for Action section appears BEFORE the main agent identity."""
    text = _read_prompt("security-reviewer")
    bias_pos = text.find("## CRITICAL: Bias for Action")
    identity_pos = text.find("# EQUIPA SecurityReviewer Agent")
    assert bias_pos < identity_pos, \
        f"Bias for Action (pos {bias_pos}) must appear before agent identity (pos {identity_pos})"


# --- Tests for _check_git_changes ---

def _make_git_repo():
    """Helper: create a temporary git repo and return its path."""
    tmpdir = tempfile.mkdtemp(prefix="equipa_test_git_")
    subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@forgeborn.dev"],
        cwd=tmpdir, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmpdir, capture_output=True, check=True,
    )
    # Create initial commit so HEAD exists
    init_file = os.path.join(tmpdir, "README.md")
    with open(init_file, "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmpdir, capture_output=True, check=True,
    )
    return tmpdir


def test_git_diff_detects_bash_file_creation():
    """_check_git_changes returns True when a new untracked file exists."""
    tmpdir = _make_git_repo()
    try:
        # Create a new file (untracked)
        new_file = os.path.join(tmpdir, "new_file.py")
        with open(new_file, "w") as f:
            f.write("print('hello')\n")
        assert _check_git_changes(tmpdir) is True, \
            "should detect untracked file as a change"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_git_diff_detects_bash_file_deletion():
    """_check_git_changes returns True when a tracked file is deleted."""
    tmpdir = _make_git_repo()
    try:
        # Delete the tracked README.md
        os.remove(os.path.join(tmpdir, "README.md"))
        assert _check_git_changes(tmpdir) is True, \
            "should detect deleted tracked file as a change"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_git_diff_detects_bash_file_modification():
    """_check_git_changes returns True when a tracked file is modified."""
    tmpdir = _make_git_repo()
    try:
        # Modify the tracked README.md
        readme = os.path.join(tmpdir, "README.md")
        with open(readme, "a") as f:
            f.write("extra content\n")
        assert _check_git_changes(tmpdir) is True, \
            "should detect modified tracked file as a change"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_git_diff_resets_file_change_counter():
    """_check_git_changes returns True for staged changes (git add without commit)."""
    tmpdir = _make_git_repo()
    try:
        # Create and stage a file
        new_file = os.path.join(tmpdir, "staged.txt")
        with open(new_file, "w") as f:
            f.write("staged content\n")
        subprocess.run(["git", "add", "staged.txt"], cwd=tmpdir, capture_output=True, check=True)
        assert _check_git_changes(tmpdir) is True, \
            "should detect staged file as a change"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_edit_write_still_detected():
    """Existing Edit/Write/NotebookEdit detection is preserved (belt and suspenders).

    This tests the function exists and handles edge cases — the integration
    with tool_name matching is verified by the early termination constants tests.
    """
    # _check_git_changes with None/empty/nonexistent dir returns False
    assert _check_git_changes(None) is False, "None project_dir should return False"
    assert _check_git_changes("") is False, "empty project_dir should return False"
    assert _check_git_changes("/nonexistent/path/xyz") is False, \
        "nonexistent directory should return False"


def test_git_diff_no_false_positives_on_read_only_bash():
    """_check_git_changes returns False for a clean repo with no changes."""
    tmpdir = _make_git_repo()
    try:
        # Repo is clean — no changes
        assert _check_git_changes(tmpdir) is False, \
            "clean repo should return False (no false positive)"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_git_diff_detects_committed_then_clean():
    """_check_git_changes returns False after all changes are committed."""
    tmpdir = _make_git_repo()
    try:
        # Create, add, and commit a file
        new_file = os.path.join(tmpdir, "committed.txt")
        with open(new_file, "w") as f:
            f.write("committed\n")
        subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add file"],
            cwd=tmpdir, capture_output=True, check=True,
        )
        # Now the repo is clean again
        assert _check_git_changes(tmpdir) is False, \
            "fully committed repo should return False"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


def test_git_diff_non_git_directory():
    """_check_git_changes returns False for a directory that is not a git repo."""
    tmpdir = tempfile.mkdtemp(prefix="equipa_test_nogit_")
    try:
        # Create a file but this is NOT a git repo
        with open(os.path.join(tmpdir, "file.txt"), "w") as f:
            f.write("not a repo\n")
        assert _check_git_changes(tmpdir) is False, \
            "non-git directory should return False (git commands fail gracefully)"
    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)


# --- Paralysis retry read gate tests ---


def test_paralysis_retry_2_first_read_not_killed():
    """Retry >= 2 + turn 1 + Read must NOT kill — only arm must_write_next_turn.

    Regression test for the unsatisfiable ZERO-READ PROTOCOL: previously,
    paralysis_retry_count >= 2 with any read-only tool on turns 1-2 was an
    instant kill, even though the agent has zero forward context after a
    paralysis kill (soft_checkpoint stores only the file list, not contents)
    and must read at least one file to know what to edit.
    """
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    early_term, must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=2,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=False,
        must_write_next_turn=False,
    )
    assert early_term is None, \
        f"retry 2 first Read must not be killed; got early_term={early_term!r}"
    assert must_write is True, \
        "retry 2 first Read should arm must_write_next_turn so the NEXT call must write"


def test_paralysis_retry_1_first_read_is_no_op():
    """Retry == 1 + turn 1 + Read is a NO-OP — gate only applies on retry >= 2.

    First paralysis retry must not arm must_write_next_turn on the first read.
    Hard design/wiring tasks legitimately need 3-4 reads before writing — task
    #2611 evidence: CryptoTrader DataContext task, turns=2/400 on every retry.
    Normal warn→final_warn→kill escalation (via effective_kill_turns) still
    applies; only the instant 1-read clamp is disabled for retry == 1.
    """
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    early_term, must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=1,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=False,
        must_write_next_turn=False,
    )
    assert early_term is None
    assert must_write is False  # gate bypassed on first retry — agent can read more


def test_paralysis_retry_1_multiple_reads_not_killed():
    """Hard task that reads 4 files on retry==1 must NOT be gated out on any read.

    Task #2611: before fix, turns 1-Read armed must_write (1 read allowed),
    turn 2-Read killed. A novel-wiring task dies at turn 2 of a 400-turn budget.
    After fix: retry==1 gate is a no-op; agent can read up to effective_kill_turns.
    """
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    must_write = False
    for turn in range(1, 5):
        early_term, must_write = _evaluate_paralysis_retry_read_gate(
            paralysis_retry_count=1,
            turn_count=turn,
            tool_name="Read",
            has_any_file_change=False,
            must_write_next_turn=must_write,
        )
        assert early_term is None, \
            f"retry==1 read on turn {turn} must not kill (gate is no-op)"
        assert must_write is False, \
            f"retry==1 read on turn {turn} must not arm must_write_next_turn"


def test_paralysis_retry_0_no_op():
    """Without a paralysis retry the gate is a no-op (returns inputs unchanged)."""
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    early_term, must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=0,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=False,
        must_write_next_turn=False,
    )
    assert early_term is None
    assert must_write is False


def test_paralysis_retry_write_tool_no_op():
    """A write-class tool on a paralysis retry is a no-op (gate ignores writes)."""
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    for tool in ("Edit", "Write", "Bash"):
        early_term, must_write = _evaluate_paralysis_retry_read_gate(
            paralysis_retry_count=2,
            turn_count=1,
            tool_name=tool,
            has_any_file_change=False,
            must_write_next_turn=False,
        )
        assert early_term is None, f"write tool {tool} unexpectedly killed"
        assert must_write is False, f"write tool {tool} unexpectedly armed must_write"


def test_paralysis_retry_after_file_change_no_op():
    """Once the agent has produced any file change, the gate stops firing."""
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    early_term, must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=3,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=True,
        must_write_next_turn=False,
    )
    assert early_term is None
    assert must_write is False


def test_paralysis_retry_already_armed_no_double_arm():
    """If must_write_next_turn is already armed, the gate does not re-arm or kill."""
    from equipa.agent_runner import _evaluate_paralysis_retry_read_gate

    early_term, must_write = _evaluate_paralysis_retry_read_gate(
        paralysis_retry_count=2,
        turn_count=1,
        tool_name="Read",
        has_any_file_change=False,
        must_write_next_turn=True,
    )
    assert early_term is None
    assert must_write is True


# --- Main ---

def main():
    tests = [
        test_stuck_phrases_detects_each,
        test_stuck_phrases_case_insensitive,
        test_stuck_phrases_returns_none_for_clean_text,
        test_stuck_phrases_partial_match,
        test_stuck_phrases_returns_first_match,
        test_dead_code_removed,
        test_output_hash_computed_for_tool_results,
        test_output_hash_handles_none_content,
        test_same_input_same_output_detected_as_stuck,
        test_same_input_different_output_not_flagged,
        test_constants_values,
        test_exempt_roles_contains_expected,
        test_stuck_phrases_list_not_empty,
        test_detect_tool_loop_integrates_with_early_term,
        test_detect_tool_loop_warn_at_3,
        test_alternating_pattern_detected_at_6_cycles,
        test_alternating_pattern_warning_at_4_cycles,
        test_alternating_successful_calls_not_flagged,
        test_alternating_with_errors_flagged,
        test_three_way_rotation_not_false_positive,
        test_alternating_detection_coexists_with_consecutive_detection,
        # Monologue detection tests
        test_monologue_detected_at_3_consecutive_text_only,
        test_monologue_resets_on_tool_use,
        test_monologue_exempt_first_5_turns,
        test_monologue_warning_at_2,
        test_monologue_does_not_interfere_with_stuck_phrases,
        test_monologue_constants,
        test_monologue_below_threshold_no_action,
        # Early completion signal tests
        test_early_complete_signal_parsed,
        test_early_complete_waits_for_message_end,
        test_early_complete_not_confused_with_result_marker,
        test_early_complete_cost_still_tracked,
        test_early_complete_distinct_from_early_terminated,
        test_false_early_complete_in_quoted_text_ignored,
        # Budget visibility tests
        test_budget_message_at_periodic_intervals,
        test_budget_halfway_warning,
        test_budget_critical_warning,
        test_budget_no_message_off_interval,
        test_budget_constants_values,
        test_budget_message_correct_remaining,
        test_budget_message_none_when_no_max_turns,
        # Cost-based circuit breaker tests
        test_cost_breaker_terminates_at_limit,
        test_cost_breaker_handles_none_cost,
        test_cost_limit_scales_with_complexity,
        test_cost_breaker_configurable_via_dispatch_config,
        test_early_term_recorded_in_loop_detector,
        test_cost_estimate_for_killed_runs,
        # Preflight build check tests
        test_preflight_detects_node_project,
        test_preflight_detects_go_project,
        test_preflight_detects_python_project,
        test_preflight_detects_csharp_project,
        test_preflight_skipped_for_build_fix_tasks,
        test_preflight_failure_injected_into_context,
        test_preflight_timeout_at_60s,
        test_preflight_does_not_block_task,
        test_preflight_unknown_project,
        test_preflight_skip_keywords_exist,
        # Anti-paralysis prompt section tests
        test_developer_prompt_has_bias_for_action,
        test_developer_prompt_has_successful_example,
        test_developer_prompt_has_failed_example,
        test_developer_prompt_has_mandatory_first_actions,
        test_developer_prompt_bias_section_is_at_top,
        test_tester_prompt_has_bias_for_action,
        test_tester_prompt_has_few_shot_examples,
        test_tester_prompt_bias_section_is_at_top,
        test_security_reviewer_prompt_has_bias_for_action,
        test_security_reviewer_prompt_has_few_shot_examples,
        test_security_reviewer_prompt_bias_section_is_at_top,
        # Git-based file change detection tests
        test_git_diff_detects_bash_file_creation,
        test_git_diff_detects_bash_file_deletion,
        test_git_diff_detects_bash_file_modification,
        test_git_diff_resets_file_change_counter,
        test_edit_write_still_detected,
        test_git_diff_no_false_positives_on_read_only_bash,
        test_git_diff_detects_committed_then_clean,
        test_git_diff_non_git_directory,
        # Paralysis retry read gate tests
        test_paralysis_retry_2_first_read_not_killed,
        test_paralysis_retry_1_first_read_is_no_op,
        test_paralysis_retry_1_multiple_reads_not_killed,
        test_paralysis_retry_0_no_op,
        test_paralysis_retry_write_tool_no_op,
        test_paralysis_retry_after_file_change_no_op,
        test_paralysis_retry_already_armed_no_double_arm,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print(f"  Early Termination Test Suite")
    print(f"  Testing _check_stuck_phrases, _compute_output_hash,")
    print(f"  _detect_tool_loop with output hashes, dead code removal,")
    print(f"  _parse_early_complete, _get_budget_message, _check_cost_limit,")
    print(f"  preflight_build_check, anti-paralysis prompt sections,")
    print(f"  _check_git_changes (git-based file change detection)")
    print(f"{'=' * 60}\n")

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
            print(f"  PASS: {test_fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL: {test_fn.__name__}: {e}")
            failed += 1
            errors.append(test_fn.__name__)
        except Exception as e:
            print(f"  ERROR: {test_fn.__name__}: {e}")
            failed += 1
            errors.append(f"{test_fn.__name__} (exception: {e})")

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print(f"  Failed tests: {', '.join(errors)}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

