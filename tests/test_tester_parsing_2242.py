"""Regression tests for the Task #2242 parsing additions.

Covers two new pieces of contract enforcement in ``equipa.parsing``:

    * ``parse_tester_output`` records whether the TESTS_SKIPPED line was
      actually present, so callers can fail closed on absence rather than
      defaulting the field to 0 (Phase A).
    * ``grep_framework_skip_counts`` cross-checks the tester's reported
      skip count against framework-emitted skip lines in raw stdout
      (Phase B). vitest/jest/pytest/playwright/go are covered.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import pytest

from equipa.parsing import grep_framework_skip_counts, parse_tester_output


# ---- parse_tester_output: tests_skipped_present sentinel -------------------


def test_tests_skipped_present_true_when_line_emitted() -> None:
    """Well-formed tester output sets the sentinel to True."""
    raw = (
        "RESULT: pass\n"
        "TEST_FRAMEWORK: pytest\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 5\n"
        "TESTS_FAILED: 0\n"
        "TESTS_SKIPPED: 0\n"
        "SUMMARY: all good\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped_present"] is True
    assert parsed["tests_skipped"] == 0


def test_tests_skipped_present_false_when_line_omitted() -> None:
    """Omission of TESTS_SKIPPED is recorded, not silently defaulted to 0."""
    raw = (
        "RESULT: pass\n"
        "TEST_FRAMEWORK: pytest\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 5\n"
        "TESTS_FAILED: 0\n"
        # NOTE: no TESTS_SKIPPED line
        "SUMMARY: missing field\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped_present"] is False
    # The numeric field still defaults to 0 (back-compat for callers that
    # don't yet read the presence sentinel), but tests_skipped_present is
    # the source of truth for the contract check.
    assert parsed["tests_skipped"] == 0


def test_tests_skipped_present_false_on_empty_text() -> None:
    """Empty result_text -> presence sentinel is False (fail closed)."""
    parsed = parse_tester_output("")
    assert parsed["tests_skipped_present"] is False


def test_tests_skipped_present_true_with_nonzero_count() -> None:
    """TESTS_SKIPPED: N>0 also marks the field as present."""
    raw = (
        "RESULT: pass\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 0\n"
        "TESTS_FAILED: 0\n"
        "TESTS_SKIPPED: 5\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped_present"] is True
    assert parsed["tests_skipped"] == 5


# ---- grep_framework_skip_counts: framework stdout detection ----------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # vitest summary
        ("Test Files  1 passed (1)\nTests  0 passed, 5 skipped (5)", 5),
        # pytest footer (with surrounding equals signs)
        ("== 3 skipped, 7 passed in 0.42s ==", 3),
        # pytest footer with the bare "= 3 skipped =" shape
        ("============== = 12 skipped = ==============", 12),
        # playwright-style
        ("Running 10 tests using 1 worker\n  10 passed (3s)\n  4 skipped", 4),
        # jest todo
        ("Tests:       2 todo, 8 passed, 10 total", 2),
        # go test SKIP lines (counted by occurrence, not integer)
        (
            "--- SKIP: TestA (0.00s)\n"
            "--- SKIP: TestB (0.00s)\n"
            "--- SKIP: TestC (0.00s)\n"
            "PASS\n",
            3,
        ),
    ],
)
def test_grep_finds_framework_skip_count(raw: str, expected: int) -> None:
    assert grep_framework_skip_counts(raw) == expected


def test_grep_returns_max_across_patterns() -> None:
    """Multiple disjoint skip signals -> return the max."""
    raw = "2 todo\n=== 5 skipped ===\n--- SKIP: T1\n--- SKIP: T2"
    # vitest "5 skipped" wins (5 > 2 todo, > 2 go SKIPs)
    assert grep_framework_skip_counts(raw) == 5


def test_grep_returns_zero_on_clean_output() -> None:
    """No skip evidence -> 0 (no false positive)."""
    raw = "RESULT: pass\nTESTS_RUN: 5\nTESTS_PASSED: 5\nAll tests pass."
    assert grep_framework_skip_counts(raw) == 0


def test_grep_returns_zero_on_empty_text() -> None:
    assert grep_framework_skip_counts("") == 0
    assert grep_framework_skip_counts(None) == 0  # type: ignore[arg-type]


def test_grep_case_insensitive() -> None:
    """Framework output may be in any case."""
    assert grep_framework_skip_counts("Tests:  3 SKIPPED") == 3
    assert grep_framework_skip_counts("4 Skipped") == 4
