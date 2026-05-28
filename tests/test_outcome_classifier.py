"""Regression tests for the outcome classifier.

Bug context (task #2481):
In task #2476 the developer agent wrote a new test file
(tests/test_review_artifact_paths.py) along with two other files, but the
orchestrator classified the run as outcome=no_tests instead of
outcome=tests_passed. This module pins down the expected classifier
behavior so the bug cannot regress.
"""
from __future__ import annotations

import pytest

from equipa.classifier import classify_outcome


def _make_turn(files_changed=None, tests_passed=None, tests_failed=None):
    """Build a minimal turn-summary dict matching what the orchestrator records."""
    return {
        "files_changed": list(files_changed or []),
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
    }


class TestOutcomeClassifier:
    def test_developer_and_tester_both_write_tests_all_pass(self):
        """Developer writes tests/test_foo.py AND tester writes tests/test_bar.py
        AND all tests pass -> tests_passed (NOT no_tests).

        This is the exact scenario from task #2476 (commit ff5f804) that
        regressed to outcome=no_tests.
        """
        turns = [
            _make_turn(
                files_changed=[
                    "src/foo.py",
                    "tests/test_foo.py",
                ]
            ),
            _make_turn(
                files_changed=["tests/test_bar.py"],
                tests_passed=4,
                tests_failed=0,
            ),
        ]
        assert classify_outcome(turns) == "tests_passed"

    def test_no_test_files_and_no_existing_tests_yields_no_tests(self):
        """Developer writes only source files AND no tests exist in repo
        -> no_tests.
        """
        turns = [
            _make_turn(
                files_changed=["src/foo.py", "src/bar.py"],
                tests_passed=None,
                tests_failed=None,
            )
        ]
        assert classify_outcome(turns, repo_has_tests=False) == "no_tests"

    def test_tests_written_but_failing_yields_tests_failed(self):
        """Developer writes tests/test_foo.py AND tests fail
        -> tests_failed (NOT tests_passed, NOT no_tests).
        """
        turns = [
            _make_turn(
                files_changed=["src/foo.py", "tests/test_foo.py"],
                tests_passed=1,
                tests_failed=2,
            )
        ]
        assert classify_outcome(turns) == "tests_failed"

    def test_test_file_under_alternate_naming_recognized(self):
        """Files like foo_test.py (Go-style) and tests at nested paths still count."""
        turns = [
            _make_turn(
                files_changed=[
                    "pkg/foo_test.py",
                    "src/sub/tests/test_inner.py",
                ],
                tests_passed=2,
                tests_failed=0,
            )
        ]
        assert classify_outcome(turns) == "tests_passed"

    def test_vacuous_pass_guard_not_weakened(self):
        """Sanity: when zero files were written, even a 'pass' signal must
        NOT yield tests_passed. The pre-existing vacuous-pass guard
        (feedback_equipa_vacuous_pass.md, 2026-04-17) must remain intact.
        """
        turns = [_make_turn(files_changed=[], tests_passed=3, tests_failed=0)]
        outcome = classify_outcome(turns)
        assert outcome != "tests_passed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
