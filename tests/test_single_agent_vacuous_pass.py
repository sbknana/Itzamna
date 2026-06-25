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

import os
import subprocess
from datetime import datetime, timedelta
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
# Bug 5: non-git project dirs + project-overlay report roles
#
# The guard proved "real work" via git only for non-review roles. In a non-git
# project dir (a CAD/docs/product folder) git detection returns nothing, so a
# report-writer overlay role's real on-disk deliverable was falsely BLOCKED.
# These pin the mtime-based filesystem fallback + resolver-aware classification.
# ---------------------------------------------------------------------------

def _run_start_marker() -> datetime:
    """A run-start timestamp safely in the past relative to files we create."""
    return datetime.now() - timedelta(seconds=30)


def test_nongit_overlay_role_with_deliverable_passes(tmp_path: Path) -> None:
    """Non-git dir + unrecognized role: a deliverable written after run start
    is seen by the filesystem fallback -> success (the Bug 5 repro)."""
    assert not (tmp_path / ".git").exists()  # genuinely not a git repo
    run_started_at = _run_start_marker()

    deliverable = tmp_path / "docs" / "ip"
    deliverable.mkdir(parents=True)
    report = deliverable / "FTO-fact-canfd.md"
    report.write_text("# FTO\nReal cited patents.\n", encoding="utf-8")

    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])

    outcome = dispatch.evaluate_single_agent_outcome(
        role="ip-analyst",
        task_id=100036,
        run_result=run_result,
        repo_path=tmp_path,
        run_started_at=run_started_at,
    )

    assert outcome.status == "success"
    assert outcome.is_blocked is False
    assert any("FTO-fact-canfd.md" in f for f in outcome.files_observed)


def test_nongit_empty_run_still_blocked(tmp_path: Path) -> None:
    """Non-git dir, nothing written after run start -> still no_output.

    The fix must stop treating "no git repo" as "no output" WITHOUT going soft
    on a genuinely empty run."""
    # A pre-existing file whose mtime predates the run must NOT count.
    stale = tmp_path / "README.md"
    stale.write_text("pre-existing\n", encoding="utf-8")
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(stale, (old, old))

    run_started_at = _run_start_marker()
    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])

    outcome = dispatch.evaluate_single_agent_outcome(
        role="ip-analyst",
        task_id=100036,
        run_result=run_result,
        repo_path=tmp_path,
        run_started_at=run_started_at,
    )

    assert outcome.status == "no_output"
    assert outcome.is_blocked is True


def test_nongit_project_overlay_report_role_classified(tmp_path: Path) -> None:
    """A project-overlay role marked ``early_term_exempt: true`` is classified
    as a report role (resolver-aware) and credited on its filesystem artifact."""
    roles_dir = tmp_path / ".equipa" / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "ip-analyst.md").write_text(
        "---\nearly_term_exempt: true\n---\nYou are an FTO analyst.\n",
        encoding="utf-8",
    )

    run_started_at = _run_start_marker()
    report = tmp_path / "FTO-fact-canfd.md"
    report.write_text("# FTO\n", encoding="utf-8")

    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])

    outcome = dispatch.evaluate_single_agent_outcome(
        role="ip-analyst",
        task_id=100036,
        run_result=run_result,
        repo_path=tmp_path,
        run_started_at=run_started_at,
        project_dir=str(tmp_path),
    )

    assert outcome.status == "success"
    assert "report role" in outcome.reason


def test_nongit_no_run_marker_keeps_legacy_behaviour(tmp_path: Path) -> None:
    """Without a run_started_at marker the filesystem fallback is disabled, so
    behaviour is unchanged (git + self-report only) -> no_output here."""
    (tmp_path / "out.md").write_text("# out\n", encoding="utf-8")

    run_result = _make_run_result(stdout="RESULT: success\n", files_changed=[])

    outcome = dispatch.evaluate_single_agent_outcome(
        role="ip-analyst",
        task_id=100036,
        run_result=run_result,
        repo_path=tmp_path,
        # run_started_at omitted on purpose
    )

    assert outcome.status == "no_output"


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
