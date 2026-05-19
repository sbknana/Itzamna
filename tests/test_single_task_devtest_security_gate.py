"""Regression tests for task 2448.

Single-task --dev-test mode MUST enforce the security-review merge gate the
same way parallel mode does. Previously the gate was absent in single-task
mode: the review ran, persisted SECURITY-REVIEW-{id}.md, but the merge
proceeded regardless of CRITICAL/HIGH findings (see task #2382 incident
2026-05-19).

These tests pin the corrected behavior:
- HIGH/CRITICAL findings block the worktree merge and demote outcome.
- A clean review allows the merge.
- A doc-only diff skips the gate (task 2360 parity).
- A missing artifact, with the flag on, fails closed (task 2341 parity).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa.dispatch import _security_review_blocks_merge
from equipa.loops import SECURITY_REVIEW_FALLBACK_MARKER
from equipa.security_gate import is_doc_only_diff


def _write_review(project_dir: Path, task_id: int, body: str) -> Path:
    artifact = project_dir / f"SECURITY-REVIEW-{task_id}.md"
    artifact.write_text(body, encoding="utf-8")
    return artifact


def test_single_task_devtest_blocks_merge_on_high(tmp_path: Path) -> None:
    """A HIGH finding in the artifact must block the merge."""
    task_id = 99001
    _write_review(
        tmp_path,
        task_id,
        "# Security Review\n\n"
        "## Findings\n\n"
        "### [S1] HIGH — Trust boundary bypass\n\nDetails...\n",
    )
    blocks, counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=True
    )
    assert blocks is True
    assert counts.get("HIGH", 0) >= 1


def test_single_task_devtest_blocks_merge_on_critical(tmp_path: Path) -> None:
    """A CRITICAL finding in the artifact must block the merge."""
    task_id = 99002
    _write_review(
        tmp_path,
        task_id,
        "# Security Review\n\n"
        "## Findings\n\n"
        "### [C1] CRITICAL — Remote code execution\n\nDetails...\n",
    )
    blocks, counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=True
    )
    assert blocks is True
    assert counts.get("CRITICAL", 0) >= 1


def test_single_task_devtest_merges_on_clean_review(tmp_path: Path) -> None:
    """A clean review (0 CRITICAL, 0 HIGH) must NOT block the merge."""
    task_id = 99003
    _write_review(
        tmp_path,
        task_id,
        "# Security Review\n\n"
        "## Findings\n\n"
        "### [M1] MEDIUM — Minor input validation gap\n\nDetails...\n"
        "### [L1] LOW — Cosmetic\n",
    )
    blocks, counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=True
    )
    assert blocks is False


def test_single_task_devtest_missing_artifact_fail_closed(tmp_path: Path) -> None:
    """A missing artifact with block_on_missing=True must block the merge."""
    task_id = 99004
    blocks, _counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=True
    )
    assert blocks is True


def test_single_task_devtest_missing_artifact_flag_off_allows_merge(
    tmp_path: Path,
) -> None:
    """A missing artifact with block_on_missing=False must NOT block the merge."""
    task_id = 99005
    blocks, _counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=False
    )
    assert blocks is False


def test_single_task_devtest_doc_only_skips_gate(tmp_path: Path) -> None:
    """A doc-only diff (only .md/.txt/.rst etc. changed) must skip the
    security review and merge normally (task 2360 parity).

    Task 2448 / SECURITY-REVIEW-2448 S1 (HIGH): the 2448 spec enumerated
    5 tests; the file shipped 4. This is the 5th. Without it the doc-only
    short-circuit in cli.py:1088-1117 is unverified — a refactor could
    turn every doc-only commit into a blocked merge (or, worse, the
    opposite: a refactor that drops the short-circuit would block every
    doc-only spec commit, breaking the task 2358 use case the
    short-circuit was added for in the first place).

    The cli.py:1088-1117 wiring is:

        review_skipped_doc_only = is_doc_only_diff(changed_files)
        if review_skipped_doc_only:
            ...  # do NOT call run_security_review, do NOT block
        else:
            ...  # run review, evaluate gate

    So the unit-level contract is: ``is_doc_only_diff`` returns True for
    any diff that contains only documentation extensions, and False the
    moment a code file is present. The integration test in
    test_single_task_devtest_security_gate_integration.py
    (test_doc_only_diff_skips_gate_end_to_end) covers the wiring end-to-end.
    """
    # All-docs diff: gate must skip.
    assert is_doc_only_diff(["README.md", "docs/spec.md", "notes.txt", "x.rst"]) is True

    # A single code file in an otherwise-doc diff: gate must run.
    assert is_doc_only_diff(["README.md", "src/foo.py"]) is False

    # Empty diff: explicitly NOT treated as doc-only (task 2321 silent-skip
    # bug class — empty result usually means the diff couldn't be computed).
    assert is_doc_only_diff([]) is False

    # Unknown extensions (Makefile, .toml, .yaml, .json) must be treated
    # as code so we never silently skip a gate the operator expected to run.
    assert is_doc_only_diff(["README.md", "Makefile"]) is False
    assert is_doc_only_diff(["docs/x.md", "pyproject.toml"]) is False


def test_single_task_devtest_fallback_dump_treated_as_missing(
    tmp_path: Path,
) -> None:
    """An orchestrator-saved fallback dump must be treated as a missing artifact
    so the fail-closed gate still fires (parity with parallel mode / task 2412).
    """
    task_id = 99006
    _write_review(
        tmp_path,
        task_id,
        f"# SECURITY-REVIEW fallback\n{SECURITY_REVIEW_FALLBACK_MARKER}\n\n"
        "raw agent output\n",
    )
    blocks, counts = _security_review_blocks_merge(
        tmp_path, task_id, block_on_missing=True,
    )
    assert blocks is True
    assert counts is None
