"""Regression tests for the Task #2242 parsing additions (attempt 3).

Covers contract enforcement in ``equipa.parsing``:

    * ``parse_tester_output`` makes TESTS_SKIPPED REQUIRED and emits
      ``None`` (not 0) when the line is absent OR its value is unparseable
      (Phase C3 + F3). The parser is the single source of truth — callers
      MUST treat ``None`` as a contract violation. The legacy
      ``tests_skipped_present`` sidechannel is removed.
    * ``grep_framework_skip_counts`` cross-checks tester claims against
      framework-emitted skip lines (Phase B / D3). Patterns are anchored
      to start-of-line framework footers so tester prose like
      "2 tests were skipped earlier in dev" does NOT inflate the count.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import pytest

from equipa.parsing import grep_framework_skip_counts, parse_tester_output


# ---- parse_tester_output: tests_skipped sentinel (Phase C3 / F3) -----------


def test_tests_skipped_is_int_when_line_emitted_cleanly() -> None:
    """Well-formed tester output -> tests_skipped is the parsed int."""
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
    assert parsed["tests_skipped"] == 0
    # Legacy sidechannel must NOT be set — parser is the source of truth.
    assert "tests_skipped_present" not in parsed


def test_tests_skipped_is_none_when_line_omitted() -> None:
    """Omission of TESTS_SKIPPED -> None sentinel, NOT default 0 (F3)."""
    raw = (
        "RESULT: pass\n"
        "TEST_FRAMEWORK: pytest\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 5\n"
        "TESTS_FAILED: 0\n"
        # NOTE: no TESTS_SKIPPED line — contract violation.
        "SUMMARY: missing field\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped"] is None


def test_tests_skipped_is_none_on_empty_text() -> None:
    """Empty result_text -> None (fail closed)."""
    parsed = parse_tester_output("")
    assert parsed["tests_skipped"] is None


def test_tests_skipped_is_none_when_value_unparseable() -> None:
    """Phase F3: presence is not enough — value must be a parseable int.

    A line like ``TESTS_SKIPPED: (see comment)`` previously satisfied a
    presence check while ``_parse_structured_output`` silently kept the
    default 0. Now the parser returns ``None`` so the call site fails
    closed exactly like an absent line.
    """
    raw = (
        "RESULT: pass\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 5\n"
        "TESTS_FAILED: 0\n"
        "TESTS_SKIPPED: (see comment)\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped"] is None


def test_tests_skipped_int_with_nonzero_count() -> None:
    """TESTS_SKIPPED: N -> the int N."""
    raw = (
        "RESULT: pass\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 0\n"
        "TESTS_FAILED: 0\n"
        "TESTS_SKIPPED: 5\n"
    )
    parsed = parse_tester_output(raw)
    assert parsed["tests_skipped"] == 5


# ---- grep_framework_skip_counts: anchored framework detection (D3) ---------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # vitest "Tests" header line — anchored at start-of-line.
        ("Test Files  1 passed (1)\nTests  0 passed, 5 skipped (5)", 5),
        # pytest summary footer line — requires >=3 leading '=' signs.
        (
            "============================== 3 skipped in 0.12s "
            "==============================",
            3,
        ),
        # pytest footer with mixed totals.
        (
            "===== 3 passed, 12 skipped in 0.05s =====",
            12,
        ),
        # playwright-style: separate "N skipped" line at line start.
        (
            "Running 10 tests using 1 worker\n"
            "  10 passed (3s)\n"
            "  4 skipped",
            4,
        ),
        # jest todo on a Tests: header line.
        ("Tests:       2 todo, 8 passed, 10 total", 2),
        # go test SKIP lines (counted by occurrence).
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
    """Multiple disjoint anchored skip signals -> return the max."""
    raw = (
        "Tests:  2 todo, 8 passed\n"
        "===== 5 skipped in 0.05s =====\n"
        "--- SKIP: T1\n"
        "--- SKIP: T2"
    )
    # pytest footer "5 skipped" wins (5 > 2 todo, > 2 go SKIPs).
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
    # Bare "4 Skipped" with no leading int/Tests/= anchor still matches the
    # generic playwright-style line: starts with optional ws + int + skipped.
    assert grep_framework_skip_counts("4 Skipped") == 4


# ---- Phase D3 anti-prose-false-positive --------------------------------------


def test_grep_ignores_prose_mentioning_skipped() -> None:
    """Phase D3: tester prose containing 'N skipped' must NOT match.

    Before D3 the patterns were unanchored, so a SUMMARY: line such as
    "2 tests were skipped earlier in dev" pushed the count to 2 spuriously
    and caused false-positive ``tests_inconclusive`` routings.
    """
    raw = (
        "RESULT: pass\n"
        "TEST_FRAMEWORK: pytest\n"
        "TESTS_RUN: 5\n"
        "TESTS_PASSED: 5\n"
        "TESTS_FAILED: 0\n"
        "TESTS_SKIPPED: 0\n"
        "SUMMARY: 2 tests were skipped earlier in dev but all currently pass\n"
    )
    assert grep_framework_skip_counts(raw) == 0


def test_grep_ignores_embedded_skipped_in_log_line() -> None:
    """Mid-line prose like "...the 7 skipped runs..." must NOT match."""
    raw = "INFO: noted that the 7 skipped runs were due to env vars\n"
    assert grep_framework_skip_counts(raw) == 0
