"""Regression fixture for task #2451.

Verifies that the unified `_gated_merge_task` helper blocks merges to master
whenever SECURITY-REVIEW-N.md reports a HIGH or CRITICAL finding, regardless
of dispatch mode (single-task or parallel).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd)


@pytest.fixture
def gated_repo(tmp_path: Path) -> dict:
    """Create a repo with master + forge-task-9999 and a SECURITY-REVIEW-9999.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@forgeborn.local")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    master_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "forge-task-9999")
    (repo / "feature.py").write_text("# feature\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feat: feature")
    _git(repo, "checkout", "-q", "master")
    return {"repo": repo, "master_head": master_head}


def _write_review(repo: Path, task_id: int, severity: str) -> None:
    review = repo / f"SECURITY-REVIEW-{task_id}.md"
    review.write_text(
        f"""# Security Review

## Findings

### [F-01] {severity} — Example finding
Some issue.

## Summary
Total: 1 finding ({severity}: 1)
"""
    )


def test_single_task_path_blocks_high(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head
    branches = _git(repo, "branch", "--list", "forge-task-9999")
    assert "forge-task-9999" in branches


def test_parallel_path_blocks_critical(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "CRITICAL")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_defensive_invariant_raises(gated_repo, monkeypatch):
    from equipa import dispatch
    from equipa.security_gate import SecurityGateBypassError

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    with pytest.raises(SecurityGateBypassError):
        asyncio.run(
            dispatch._merge_task_branch(str(repo), 9999, "forge-task-9999")
        )

    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_happy_path_merges_when_clean(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    # Clean review — zero CRITICAL/HIGH
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        "# Security Review\n\nNo CRITICAL or HIGH findings.\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "merged"
    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head != master_head


def test_outcome_not_eligible_skipped(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_failed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "skipped"
    assert _git(repo, "rev-parse", "HEAD") == master_head
