"""Regression tests for task 2447 — security-review artifact must be
persisted to a STABLE path that survives worktree teardown.

Bug recap: parallel-mode dispatches run security review inside a
per-task git worktree (``task_dir``). The reviewer agent writes
``SECURITY-REVIEW-{task_id}.md`` into the worktree, the merge gate
reads it, and then the worktree is torn down — taking the artifact
with it. Operators see "Found N HIGH" in the dispatch log but cannot
audit the actual findings.

Task 2412 was supposed to fix this and is marked done, but the bug
recurred (tasks 2379, 2382). Root cause: 2412's fallback writer
targeted the worktree path, not the stable project root.

The fix (task 2447): the orchestrator now persists the artifact to
``stable_project_dir`` (the project's real ``local_path``) on EVERY
security-review outcome — pass, block, missing, crash. If the reviewer
saved a structured artifact, copy it; if not, synthesize a fallback
from the captured agent ``result_text``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    SECURITY_REVIEW_FALLBACK_MARKER,
    _persist_security_review_artifact,
    run_security_review,
)


def _stable_artifact(stable: Path, task_id: int) -> Path:
    """Per task 2476, persisted artifacts live under .equipa-artifacts/."""
    return stable / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{task_id}.md"


# ---------------------------------------------------------------------------
# _persist_security_review_artifact — unit tests
# ---------------------------------------------------------------------------


def _structured_review(critical: int = 0, high: int = 0) -> str:
    lines = ["# Security Review", ""]
    for label, n in (("CRITICAL", critical), ("HIGH", high)):
        for i in range(n):
            lines.append(f"### [{label[0]}{i + 1}] {label} — finding")
            lines.append("Body.")
            lines.append("")
    return "\n".join(lines)


def test_persist_copies_structured_artifact_from_worktree(tmp_path):
    """If the agent wrote a structured artifact in the worktree, copy
    it verbatim to the stable path."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    src = worktree / "SECURITY-REVIEW-2447.md"
    src.write_text(_structured_review(critical=0, high=1), encoding="utf-8")

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text="ignored — agent wrote real file",
        agent_succeeded=True,
    )

    dst = _stable_artifact(stable, 2447)
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    # Must NOT be a fallback dump — it is the agent's real artifact.
    assert SECURITY_REVIEW_FALLBACK_MARKER not in dst.read_text(encoding="utf-8")
    # Task 2476: artifact must NOT appear at the legacy repo-root path.
    assert not (stable / "SECURITY-REVIEW-2447.md").exists()


def test_persist_synthesizes_fallback_when_agent_wrote_nothing(tmp_path):
    """If the agent did NOT save the artifact, synthesize a fallback
    dump from result_text at the stable path."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text=(
            "FOUND 1 CRITICAL and 2 HIGH severity findings.\n"
            "[S1] CRITICAL: SQL injection in foo.py\n"
        ),
        agent_succeeded=True,
    )

    dst = _stable_artifact(stable, 2447)
    assert dst.is_file(), "fallback must be written at stable path"
    body = dst.read_text(encoding="utf-8")
    assert SECURITY_REVIEW_FALLBACK_MARKER in body
    assert "SQL injection in foo.py" in body, (
        "captured agent output must be preserved"
    )


def test_persist_does_not_clobber_existing_structured_artifact(tmp_path):
    """If the stable path already has a structured artifact (e.g. from
    a prior run), don't overwrite it with a synthesized fallback when
    the agent wrote nothing this time."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    existing = _stable_artifact(stable, 2447)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(_structured_review(high=1), encoding="utf-8")
    original_text = existing.read_text(encoding="utf-8")

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text="some new agent output",
        agent_succeeded=True,
    )

    assert existing.read_text(encoding="utf-8") == original_text
    assert SECURITY_REVIEW_FALLBACK_MARKER not in existing.read_text(
        encoding="utf-8"
    )


def test_persist_overwrites_with_fresh_worktree_artifact(tmp_path):
    """A fresh worktree artifact for THIS run supersedes any stale file
    at the stable path."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    src = worktree / "SECURITY-REVIEW-2447.md"
    src.write_text(_structured_review(critical=1), encoding="utf-8")
    stale = _stable_artifact(stable, 2447)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# stale from previous run\n", encoding="utf-8")

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text="",
        agent_succeeded=True,
    )

    body = stale.read_text(encoding="utf-8")
    assert "[C1] CRITICAL" in body
    assert "stale from previous run" not in body


def test_persist_noop_without_task_id(tmp_path):
    """If task_id is missing or 0, the helper is a no-op (no file
    operations performed)."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=None,
        result_text="x",
        agent_succeeded=False,
    )

    # No artifact directory created, no files written.
    assert list(stable.iterdir()) == []
    assert not (stable / ARTIFACTS_DIR_NAME).exists()


def test_persist_synthesizes_fallback_when_agent_crashed(tmp_path):
    """Even if the agent crashed (agent_succeeded=False, empty
    result_text), the helper still creates a fallback so the
    block-without-artifact path is auditable."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text="",
        agent_succeeded=False,
    )

    dst = _stable_artifact(stable, 2447)
    assert dst.is_file()
    body = dst.read_text(encoding="utf-8")
    assert SECURITY_REVIEW_FALLBACK_MARKER in body


# ---------------------------------------------------------------------------
# Artifact survives worktree teardown
# ---------------------------------------------------------------------------


def test_artifact_survives_worktree_removal(tmp_path):
    """The single sharpest invariant of task 2447: after the worktree
    directory is removed, the artifact must still be readable at the
    stable path."""
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    src = worktree / "SECURITY-REVIEW-2447.md"
    src.write_text(_structured_review(high=2), encoding="utf-8")

    _persist_security_review_artifact(
        worktree_dir=str(worktree),
        stable_dir=str(stable),
        task_id=2447,
        result_text="",
        agent_succeeded=True,
    )

    # Simulate worktree teardown.
    shutil.rmtree(worktree)
    assert not worktree.exists()

    dst = _stable_artifact(stable, 2447)
    assert dst.is_file(), (
        "artifact at stable path must survive worktree removal"
    )
    body = dst.read_text(encoding="utf-8")
    assert "[H1] HIGH" in body
    assert "[H2] HIGH" in body


# ---------------------------------------------------------------------------
# run_security_review integration: stable_project_dir kwarg wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def _patched_runner():
    """Patch the heavy bits of run_security_review (agent invocation,
    prompt building) so the test exercises only the artifact-persistence
    wiring at the bottom of the function."""

    sec_result_holder = {
        "result": {
            "success": True,
            "duration": 0.1,
            "result_text": (
                "I reviewed the code. Found 1 HIGH severity issue.\n"
                "[S1] HIGH — example finding for the captured-output path."
            ),
            "errors": [],
        }
    }

    async def fake_run_agent(cmd, timeout=None):
        return sec_result_holder["result"]

    class _Ctx:
        def __init__(self):
            self.cmd = ["fake-cmd"]

        def __enter__(self):
            return self.cmd

        def __exit__(self, *a):
            return False

    with patch("equipa.loops.run_agent", side_effect=fake_run_agent), \
         patch("equipa.loops.build_cli_command", return_value=_Ctx()), \
         patch("equipa.loops.build_system_prompt", return_value="prompt"), \
         patch("equipa.loops.get_role_turns", return_value=10), \
         patch("equipa.loops.get_role_model", return_value="opus"), \
         patch(
             "equipa.loops.load_dispatch_config",
             return_value={"security_review_timeout": 30},
         ), \
         patch("equipa.loops._extract_security_findings", return_value=[]), \
         patch("equipa.loops._create_security_lessons", return_value=0):
        yield sec_result_holder


@pytest.mark.asyncio
async def test_security_review_artifact_persisted_on_pass(
    tmp_path, _patched_runner
):
    """test_security_review_artifact_persisted_on_pass — task spec
    regression #1: a passing review still writes the artifact to the
    stable path."""
    worktree = tmp_path / "wt-3001"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()

    # Agent writes a clean structured artifact in the worktree at the
    # new artifacts-dir location (task 2476).
    artifact_dir = worktree / ARTIFACTS_DIR_NAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "SECURITY-REVIEW-3001.md"
    artifact.write_text(
        "# Review\n\n### [I1] INFO — no real issues\n", encoding="utf-8",
    )

    task = {"id": 3001, "title": "t", "description": "d", "project_id": 1}
    await run_security_review(
        task, str(worktree), {}, MagicMock(),
        stable_project_dir=str(stable),
    )

    dst = _stable_artifact(stable, 3001)
    assert dst.is_file()
    assert "[I1] INFO" in dst.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_security_review_artifact_persisted_on_block(
    tmp_path, _patched_runner
):
    """test_security_review_artifact_persisted_on_block — task spec
    regression #2: a HIGH-finding blocked review still writes the
    artifact to the stable path."""
    worktree = tmp_path / "wt-3002"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()

    artifact_dir = worktree / ARTIFACTS_DIR_NAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "SECURITY-REVIEW-3002.md").write_text(
        _structured_review(high=2), encoding="utf-8",
    )

    task = {"id": 3002, "title": "t", "description": "d", "project_id": 1}
    await run_security_review(
        task, str(worktree), {}, MagicMock(),
        stable_project_dir=str(stable),
    )

    dst = _stable_artifact(stable, 3002)
    assert dst.is_file()
    body = dst.read_text(encoding="utf-8")
    assert body.count("HIGH") >= 2

    # Simulate worktree teardown — artifact must still survive.
    shutil.rmtree(worktree)
    assert dst.is_file()


@pytest.mark.asyncio
async def test_gating_block_without_artifact_escalates(
    tmp_path, _patched_runner
):
    """test_gating_block_without_artifact_escalates — task spec
    regression #3: reviewer returns "block" but writes nothing. The
    orchestrator must synthesize the artifact from the captured agent
    output so it is never silently lost. The synthesized file is
    marked as a fallback so the fail-closed merge gate still fires."""
    worktree = tmp_path / "wt-3003"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    # Note: NO artifact written by the agent in the worktree.

    task = {"id": 3003, "title": "t", "description": "d", "project_id": 1}
    await run_security_review(
        task, str(worktree), {}, MagicMock(),
        stable_project_dir=str(stable),
    )

    dst = _stable_artifact(stable, 3003)
    assert dst.is_file(), (
        "block without saved artifact must still produce a stable file"
    )
    body = dst.read_text(encoding="utf-8")
    assert SECURITY_REVIEW_FALLBACK_MARKER in body
    # Captured raw agent output is in the dump (preserves findings).
    assert "example finding for the captured-output path" in body
