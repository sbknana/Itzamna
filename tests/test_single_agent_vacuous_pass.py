"""Regression tests for task #2371: vacuous-pass guard on single-agent role modes.

Background
----------
Prior to this fix the vacuous-pass guard (which downgrades SUCCESS -> blocked
when an agent run produces no on-disk output) only ran inside the Dev+Test
loop code path. Single-agent role dispatches (``--task X --role Y`` without
``--dev-test``) bypassed the loop entirely, so a no-write planner / reviewer
run could be marked SUCCESS and the DB row set to DONE -- exactly what
happened for task #2361 on 2026-05-14.

These tests pin the "no output -> blocked" contract on the single-agent
dispatch path so the regression cannot return silently.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from equipa import dispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo so git-diff calls have something to talk to."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


def _make_run_result(
    *,
    stdout: str = "",
    files_changed: list[str] | None = None,
    raw_files_changed: list[str] | None = None,
) -> dict[str, Any]:
    """Shape mirrors what the Claude CLI wrapper returns."""
    return {
        "stdout": stdout,
        "stderr": "",
        "returncode": 0,
        "files_changed": files_changed or [],
        "raw_files_changed": raw_files_changed or [],
    }


# ---------------------------------------------------------------------------
# File-producing roles: planner / developer / frontend-designer / world-builder
# ---------------------------------------------------------------------------

def test_single_agent_planner_no_files_is_blocked(fake_repo: Path) -> None:
    """Planner role: zero git changes -> outcome no_output, task NOT marked done."""
    run_result = _make_run_result(
        stdout="RESULT: success\nSUMMARY: planned things\nFILES_CHANGED: none\n",
        files_changed=[],
    )

    outcome = dispatch.evaluate_single_agent_outcome(
        role="planner",
        task_id=2361,
        run_result=run_result,
        repo_path=fake_repo,
    )

    assert outcome.status == "no_output"
    assert outcome.is_blocked is True
    assert "no file" in outcome.reason.lower() or "zero" in outcome.reason.lower()


def test_single_agent_planner_with_files_passes(fake_repo: Path) -> None:
    """Planner role: writes a file -> success."""
    deliverable = fake_repo / "PLAN.md"
    deliverable.write_text("# Plan\n")
    subprocess.run(["git", "add", "PLAN.md"], cwd=fake_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "plan"],
        cwd=fake_repo, check=True,
    )

    run_result = _make_run_result(
        stdout="RESULT: success\nFILES_CHANGED: PLAN.md\n",
        files_changed=["PLAN.md"],
    )

    outcome = dispatch.evaluate_single_agent_outcome(
        role="planner",
        task_id=2361,
        run_result=run_result,
        repo_path=fake_repo,
    )

    assert outcome.status == "success"
    assert outcome.is_blocked is False


def test_single_agent_developer_no_files_is_blocked(fake_repo: Path) -> None:
    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])
    outcome = dispatch.evaluate_single_agent_outcome(
        role="developer",
        task_id=99,
        run_result=run_result,
        repo_path=fake_repo,
    )
    assert outcome.status == "no_output"
    assert outcome.is_blocked is True


# ---------------------------------------------------------------------------
# Review roles: code-reviewer / security-reviewer / evaluator
# ---------------------------------------------------------------------------

def test_single_agent_reviewer_no_artifact_is_blocked(fake_repo: Path) -> None:
    """code-reviewer run with no {REVIEW}-{ID}.md on disk -> blocked."""
    run_result = _make_run_result(
        stdout="RESULT: success\nSUMMARY: looks fine\n",
        files_changed=[],
    )

    outcome = dispatch.evaluate_single_agent_outcome(
        role="code-reviewer",
        task_id=2361,
        run_result=run_result,
        repo_path=fake_repo,
    )

    assert outcome.status == "no_output"
    assert outcome.is_blocked is True
    assert "artifact" in outcome.reason.lower() or "review" in outcome.reason.lower()


def test_single_agent_reviewer_with_artifact_passes(fake_repo: Path) -> None:
    """code-reviewer with CODE-REVIEW-2361.md on disk -> success."""
    artifact = fake_repo / "CODE-REVIEW-2361.md"
    artifact.write_text("# Code Review\nLGTM\n")

    run_result = _make_run_result(
        stdout="RESULT: success\n",
        files_changed=["CODE-REVIEW-2361.md"],
    )

    outcome = dispatch.evaluate_single_agent_outcome(
        role="code-reviewer",
        task_id=2361,
        run_result=run_result,
        repo_path=fake_repo,
    )

    assert outcome.status == "success"
    assert outcome.is_blocked is False


def test_single_agent_security_reviewer_with_artifact_passes(fake_repo: Path) -> None:
    artifact = fake_repo / "SECURITY-REVIEW-2361.md"
    artifact.write_text("# Security Review\n")

    run_result = _make_run_result(
        stdout="RESULT: success\n",
        files_changed=["SECURITY-REVIEW-2361.md"],
    )

    outcome = dispatch.evaluate_single_agent_outcome(
        role="security-reviewer",
        task_id=2361,
        run_result=run_result,
        repo_path=fake_repo,
    )

    assert outcome.status == "success"


def test_single_agent_evaluator_no_artifact_is_blocked(fake_repo: Path) -> None:
    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])
    outcome = dispatch.evaluate_single_agent_outcome(
        role="evaluator",
        task_id=42,
        run_result=run_result,
        repo_path=fake_repo,
    )
    assert outcome.status == "no_output"
    assert outcome.is_blocked is True


# ---------------------------------------------------------------------------
# Hallucinated TASKS_CREATED rejection
# ---------------------------------------------------------------------------

def test_hallucinated_tasks_created_rejected(fake_repo: Path) -> None:
    """Agent emits TASKS_CREATED with IDs that predate the run -> failure."""
    db = MagicMock()
    # All four IDs exist BUT were created before run_started_at -> hallucination.
    db.fetch_tasks_by_ids.return_value = [
        {"id": 78, "project_id": 23, "created_at": "2026-01-10"},
        {"id": 79, "project_id": 23, "created_at": "2026-01-10"},
        {"id": 80, "project_id": 23, "created_at": "2026-01-10"},
        {"id": 81, "project_id": 99, "created_at": "2026-01-11"},
    ]

    stdout = (
        "RESULT: success\n"
        "FILES_CHANGED: none\n"
        "TASKS_CREATED: 78,79,80,81\n"
    )

    result = dispatch.validate_tasks_created_claim(
        stdout=stdout,
        run_started_at="2026-05-14T10:00:00",
        expected_project_id=23,
        db=db,
    )

    assert result.is_valid is False
    assert result.reason  # non-empty explanation
    # Should flag both the pre-existing-IDs problem and the project_id mismatch
    assert any("pre-existing" in r.lower() or "before" in r.lower()
               or "stale" in r.lower() for r in [result.reason])


def test_valid_tasks_created_accepted(fake_repo: Path) -> None:
    db = MagicMock()
    db.fetch_tasks_by_ids.return_value = [
        {"id": 500, "project_id": 23, "created_at": "2026-05-14T10:05:00"},
        {"id": 501, "project_id": 23, "created_at": "2026-05-14T10:05:01"},
    ]

    stdout = "RESULT: success\nTASKS_CREATED: 500,501\n"

    result = dispatch.validate_tasks_created_claim(
        stdout=stdout,
        run_started_at="2026-05-14T10:00:00",
        expected_project_id=23,
        db=db,
    )

    assert result.is_valid is True


def test_tasks_created_missing_ids_rejected(fake_repo: Path) -> None:
    """Some claimed IDs don't exist at all -> rejection."""
    db = MagicMock()
    db.fetch_tasks_by_ids.return_value = [
        {"id": 500, "project_id": 23, "created_at": "2026-05-14T10:05:00"},
    ]
    stdout = "TASKS_CREATED: 500,999\n"

    result = dispatch.validate_tasks_created_claim(
        stdout=stdout,
        run_started_at="2026-05-14T10:00:00",
        expected_project_id=23,
        db=db,
    )

    assert result.is_valid is False
