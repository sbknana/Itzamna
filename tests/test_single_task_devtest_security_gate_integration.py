"""End-to-end integration tests for the single-task --dev-test security gate.

Task 2449 / SECURITY-REVIEW-2448 S2 (HIGH):
The existing unit tests in test_single_task_devtest_security_gate.py call
dispatch._security_review_blocks_merge(...) directly. That helper PRE-DATES
the task 2448 patch (parallel mode already used it). The actual fix is the
orchestration glue in equipa/cli.py run_mode_task (~lines 1064-1171):
  * runs run_security_review BEFORE _post_task_telemetry,
  * computes review_blocks_merge,
  * demotes `outcome` to "security_review_blocked",
  * fail-closed on reviewer crash.

Without an integration test driving run_mode_task end-to-end, the suite
would still report all-pass even if the cli.py call site were deleted
(re-introducing the exact 2448 bug). This module closes that gap.

The load-bearing assertion is: when run_security_review returns >=1 HIGH,
the task branch is NOT merged to master.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.integration


def _init_repo(path: Path) -> None:
    """Initialize a git repo with a master branch and a feature branch."""
    subprocess.run(["git", "init", "-b", "master"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forgeborn.dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def _make_feature_branch(path: Path, branch: str, files: dict[str, str]) -> str:
    """Create a feature branch with the given files. Returns the branch SHA."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=path, check=True, capture_output=True)
    for rel, content in files.items():
        fp = path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"feat: {branch}"], cwd=path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "master"], cwd=path, check=True, capture_output=True)
    return sha


def _master_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "master"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _branch_merged_to_master(path: Path, branch: str) -> bool:
    """True iff `branch` has been merged into master."""
    result = subprocess.run(
        ["git", "branch", "--merged", "master"], cwd=path, check=True, capture_output=True, text=True
    )
    return any(line.strip().lstrip("* ").strip() == branch for line in result.stdout.splitlines())


# Stub: real run_mode_task driver is wired below once signature is confirmed.
def _drive_run_mode_task(*, repo: Path, task_id: int, review_high: int, review_critical: int, review_crash: bool, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Drive equipa.cli.run_mode_task with monkeypatched dependencies. Returns the recorded outcome."""
    raise NotImplementedError("wired in follow-up edit")


class TestSingleTaskDevtestSecurityGateIntegration:
    """End-to-end coverage of the run_mode_task security-gate orchestration."""

    def test_high_severity_finding_blocks_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When run_security_review reports >=1 HIGH, branch must NOT be merged to master."""
        _init_repo(tmp_path)
        _make_feature_branch(tmp_path, "forge-task-99", {"src/foo.py": "def f():\n    return 1\n"})
        master_before = _master_sha(tmp_path)

        outcome = _drive_run_mode_task(
            repo=tmp_path, task_id=99, review_high=1, review_critical=0,
            review_crash=False, monkeypatch=monkeypatch,
        )

        assert outcome["outcome"] == "security_review_blocked"
        assert _master_sha(tmp_path) == master_before, "master must not advance when HIGH findings block merge"
        assert not _branch_merged_to_master(tmp_path, "forge-task-99"), "feature branch must not be merged"

    def test_critical_severity_finding_blocks_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CRITICAL findings must block the merge identically to HIGH."""
        _init_repo(tmp_path)
        _make_feature_branch(tmp_path, "forge-task-100", {"src/bar.py": "def g():\n    return 2\n"})
        master_before = _master_sha(tmp_path)

        outcome = _drive_run_mode_task(
            repo=tmp_path, task_id=100, review_high=0, review_critical=1,
            review_crash=False, monkeypatch=monkeypatch,
        )

        assert outcome["outcome"] == "security_review_blocked"
        assert _master_sha(tmp_path) == master_before
        assert not _branch_merged_to_master(tmp_path, "forge-task-100")

    def test_clean_review_allows_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A clean review (0 CRITICAL, 0 HIGH) must NOT block the merge."""
        _init_repo(tmp_path)
        _make_feature_branch(tmp_path, "forge-task-101", {"src/baz.py": "def h():\n    return 3\n"})
        master_before = _master_sha(tmp_path)

        outcome = _drive_run_mode_task(
            repo=tmp_path, task_id=101, review_high=0, review_critical=0,
            review_crash=False, monkeypatch=monkeypatch,
        )

        assert outcome["outcome"] != "security_review_blocked"
        # Either outcome is tests_passed/done OR the merge happened — both are acceptable.
        # The load-bearing negative assertion is that we did NOT demote.

    def test_reviewer_crash_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If run_security_review raises, the gate must fail-closed (block merge)."""
        _init_repo(tmp_path)
        _make_feature_branch(tmp_path, "forge-task-102", {"src/qux.py": "def i():\n    return 4\n"})
        master_before = _master_sha(tmp_path)

        outcome = _drive_run_mode_task(
            repo=tmp_path, task_id=102, review_high=0, review_critical=0,
            review_crash=True, monkeypatch=monkeypatch,
        )

        assert outcome["outcome"] == "security_review_blocked"
        assert _master_sha(tmp_path) == master_before
        assert not _branch_merged_to_master(tmp_path, "forge-task-102")
