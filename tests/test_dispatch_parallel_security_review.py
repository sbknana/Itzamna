"""Regression tests for bug 2321 — parallel-mode security review gate.

Bug 2321: parallel-isolation mode (--tasks N,M,O with worktrees) auto-
merged task branches to local master without ever running the security
reviewer agent, while single-task mode (--task N) did run it. Five
commits across two repos landed un-reviewed in production before the
operator caught the divergence.

These tests cover the helpers that gate the merge and the integrated
parallel-mode dispatch path:

* ``_is_security_review_enabled`` reads the same precedence chain as
  single-task mode (CLI flag, then dispatch_config top-level, then
  features.security_review).
* ``_security_review_blocks_merge`` reads the SECURITY-REVIEW-NNNN.md
  artifact (NOT raw agent stdout) so the gate uses the same plumbing as
  task 2315 fixed for the single-task path.
* ``run_parallel_tasks`` calls ``run_security_review`` once per task that
  completed dev-test successfully, demotes the outcome on CRITICAL/HIGH
  findings so the task ends up blocked (not done), and removes the
  branch from the merge candidate list.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from equipa.dispatch import (
    _is_security_review_enabled,
    _security_review_blocks_merge,
    run_parallel_tasks,
)


# ---------------------------------------------------------------------------
# _is_security_review_enabled — enablement precedence
# ---------------------------------------------------------------------------


def _make_args(
    security_review=None, dispatch_config=None, yes=True,
) -> MagicMock:
    args = MagicMock()
    args.security_review = security_review
    args.dispatch_config = dispatch_config or {}
    args.yes = yes
    args.max_concurrent = 4
    args.use_flow = False
    return args


def test_enabled_when_cli_flag_true():
    args = _make_args(security_review=True, dispatch_config={})
    assert _is_security_review_enabled(args) is True


def test_disabled_when_cli_flag_false():
    """CLI flag --no-security-review (False) wins over dispatch config."""
    args = _make_args(
        security_review=False,
        dispatch_config={"security_review": True},
    )
    assert _is_security_review_enabled(args) is False


def test_falls_back_to_dispatch_config_top_level():
    args = _make_args(
        security_review=None,
        dispatch_config={"security_review": True},
    )
    assert _is_security_review_enabled(args) is True


def test_feature_flag_can_disable_top_level_key():
    args = _make_args(
        security_review=None,
        dispatch_config={
            "security_review": True,
            "features": {"security_review": False},
        },
    )
    assert _is_security_review_enabled(args) is False


def test_disabled_when_no_flag_and_no_config():
    args = _make_args()
    assert _is_security_review_enabled(args) is False


# ---------------------------------------------------------------------------
# _security_review_blocks_merge — read from artifact, not stdout
# ---------------------------------------------------------------------------


def _write_review(path: Path, *, critical: int = 0, high: int = 0,
                  medium: int = 0, low: int = 0, info: int = 0) -> None:
    """Build a synthetic SECURITY-REVIEW-NNNN.md with N findings per severity.

    Uses the canonical ``### [TAG-NN] SEVERITY — desc`` header format
    that ``_count_findings_in_review_file`` matches.
    """
    sections = ["# Security Review", ""]
    for label, n in (
        ("CRITICAL", critical), ("HIGH", high), ("MEDIUM", medium),
        ("LOW", low), ("INFO", info),
    ):
        for i in range(n):
            sections.append(f"### [{label[0]}{i + 1}] {label} — finding {i + 1}")
            sections.append("Some prose describing the finding.")
            sections.append("")
    path.write_text("\n".join(sections), encoding="utf-8")


def test_blocks_when_critical_present(tmp_path):
    _write_review(tmp_path / "SECURITY-REVIEW-42.md", critical=1)
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 42)
    assert blocks is True
    assert counts == {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}


def test_blocks_when_high_present(tmp_path):
    _write_review(tmp_path / "SECURITY-REVIEW-42.md", high=2)
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 42)
    assert blocks is True
    assert counts["HIGH"] == 2


def test_does_not_block_when_only_medium_low_info(tmp_path):
    _write_review(
        tmp_path / "SECURITY-REVIEW-42.md", medium=3, low=5, info=2,
    )
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 42)
    assert blocks is False
    assert counts["MEDIUM"] == 3
    assert counts["LOW"] == 5
    assert counts["INFO"] == 2


def test_missing_artifact_blocks_by_default(tmp_path):
    """Task 2341 S1: missing artifact is fail-closed by default.

    The pre-2341 contract returned (False, None) — a crashed reviewer
    that never wrote the artifact would silently authorise the merge.
    The default is now (True, None): operators must opt out via
    features.security_review_block_on_missing_artifact=False.
    """
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 99)
    assert blocks is True
    assert counts is None


def test_missing_artifact_does_not_block_when_flag_disabled(tmp_path):
    """Operator opt-out via block_on_missing=False restores fail-open."""
    blocks, counts = _security_review_blocks_merge(
        str(tmp_path), 99, block_on_missing=False,
    )
    assert blocks is False
    assert counts is None


def test_artifact_count_ignores_severity_words_in_prose(tmp_path):
    """The gate must count finding headers, not substring 'CRITICAL' in prose.

    This is the same defence that task 2315 added to the single-task
    path. The gate has to use the helper that reads the artifact, not
    the raw agent stdout, or it will fire on rejected-finding prose
    like '[S1] LOW — this is NOT a CRITICAL because…'.
    """
    review = tmp_path / "SECURITY-REVIEW-42.md"
    review.write_text(
        "# Review\n\n"
        "### [S1] LOW — this is NOT a CRITICAL vulnerability\n"
        "The reviewer considered HIGH severity but downgraded after analysis.\n"
        "\n"
        "### [S2] INFO — discussion of HIGH-impact edge cases\n",
        encoding="utf-8",
    )
    blocks, counts = _security_review_blocks_merge(str(tmp_path), 42)
    assert blocks is False, "prose mentions must not trigger the gate"
    assert counts["CRITICAL"] == 0
    assert counts["HIGH"] == 0
    assert counts["LOW"] == 1
    assert counts["INFO"] == 1


# ---------------------------------------------------------------------------
# Integration: run_parallel_tasks calls security review and gates merge
# ---------------------------------------------------------------------------


def _patch_parallel_mode(
    tmp_project: Path,
    *,
    review_writer,
    dev_outcome: str = "tests_passed",
):
    """Return a list of patches that stub the parallel-mode dependencies.

    Each test invokes ``run_parallel_tasks`` against the same skeleton
    project so the patches can stay shared. ``review_writer(task_dir,
    task_id)`` is the hook each test uses to drop (or omit) the
    SECURITY-REVIEW-NNNN.md artifact.
    """
    async def fake_dev_test(task, project_dir, project_context, args,
                            config, output=None):
        return (
            {"cost": 0.0, "duration": 0.0},
            1,
            dev_outcome,
            0.0,
            0.0,
            task,
        )

    async def fake_security_review(task, project_dir, project_context, args,
                                   output=None):
        review_writer(Path(project_dir), task["id"])
        return {"success": True, "duration": 0.0, "result_text": ""}

    async def fake_create_worktrees(tasks, project_dir, worktree_base):
        out = {}
        for t in tasks:
            d = tmp_project / f"wt-{t['id']}"
            d.mkdir(parents=True, exist_ok=True)
            out[t["id"]] = str(d)
        return out

    merge_calls: list[tuple[str, int, str]] = []

    async def fake_merge(project_dir, task_id, branch_name):
        merge_calls.append((project_dir, task_id, branch_name))
        return True

    async def fake_cleanup(project_dir, worktree_dirs, merged, base):
        return None

    patches = [
        patch("equipa.dispatch.fetch_tasks_by_ids",
              return_value=[{
                  "id": 100, "project_id": 1, "title": "t",
                  "description": "d", "role": "developer",
              }]),
        patch("equipa.dispatch.resolve_project_dir",
              return_value=str(tmp_project)),
        patch("equipa.dispatch.fetch_project_context", return_value={}),
        patch("equipa.dispatch._is_git_repo", return_value=True),
        patch(
            "equipa.dispatch.run_dev_test_loop_with_autoresearch",
            side_effect=fake_dev_test,
        ),
        patch(
            "equipa.dispatch.run_security_review",
            side_effect=fake_security_review,
        ),
        patch(
            "equipa.dispatch._create_isolation_worktrees",
            side_effect=fake_create_worktrees,
        ),
        patch(
            "equipa.dispatch._merge_task_branch",
            side_effect=fake_merge,
        ),
        patch(
            "equipa.dispatch._cleanup_worktrees",
            side_effect=fake_cleanup,
        ),
        patch("equipa.dispatch.update_task_status"),
        patch("equipa.dispatch.record_agent_run"),
        patch("equipa.dispatch.get_role_model", return_value="opus"),
        patch("equipa.dispatch.get_role_turns", return_value=10),
    ]
    return patches, merge_calls


@pytest.mark.asyncio
async def test_parallel_mode_runs_security_review(tmp_path):
    """A successful parallel-mode task triggers run_security_review and
    persists SECURITY-REVIEW-NNNN.md in the worktree."""
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        _write_review(task_dir / f"SECURITY-REVIEW-{task_id}.md")

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    # We also need two tasks to force use_worktrees=True (>1 task).
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 100, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 101, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5] as mock_review, patches[6], patches[7], patches[8], \
         patches[9], patches[10], patches[11], patches[12]:
        await run_parallel_tasks([100, 101], args)

    # Review was invoked once per task.
    assert mock_review.call_count == 2
    # And the artifact landed in each worktree.
    assert (tmp_path / "wt-100" / "SECURITY-REVIEW-100.md").is_file()
    assert (tmp_path / "wt-101" / "SECURITY-REVIEW-101.md").is_file()
    # Both branches merged (clean reviews).
    merged_ids = {tid for _, tid, _ in merge_calls}
    assert merged_ids == {100, 101}


@pytest.mark.asyncio
async def test_parallel_mode_blocks_merge_on_critical(tmp_path):
    """A CRITICAL finding leaves the branch unmerged."""
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        _write_review(
            task_dir / f"SECURITY-REVIEW-{task_id}.md", critical=1,
        )

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 200, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 201, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], \
         patches[9] as mock_status, patches[10], patches[11], patches[12]:
        await run_parallel_tasks([200, 201], args)

    # Critical findings => NO merge.
    assert merge_calls == []
    # update_task_status(task_id, outcome, output=...) — outcome arg
    # is "security_review_blocked", which maps to 'blocked' (not 'done').
    assert len(mock_status.call_args_list) == 2
    for c in mock_status.call_args_list:
        assert c[0][1] == "security_review_blocked", c


@pytest.mark.asyncio
async def test_parallel_mode_blocks_merge_on_high(tmp_path):
    """A HIGH finding (without CRITICAL) also blocks merge."""
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        _write_review(task_dir / f"SECURITY-REVIEW-{task_id}.md", high=2)

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 300, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 301, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], \
         patches[10], patches[11], patches[12]:
        await run_parallel_tasks([300, 301], args)

    assert merge_calls == [], "HIGH findings must also gate the merge"


@pytest.mark.asyncio
async def test_parallel_mode_review_uses_count_from_artifact(tmp_path):
    """The gate reads the artifact (task 2315 plumbing), not raw stdout.

    Even when the review agent's stdout contains the words 'CRITICAL'
    and 'HIGH' all over the place, only counts derived from the
    SECURITY-REVIEW-NNNN.md file may trigger the gate.
    """
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        # Artifact reports MEDIUM only — should NOT block.
        review = task_dir / f"SECURITY-REVIEW-{task_id}.md"
        review.write_text(
            "### [M1] MEDIUM — minor issue\nDescribed.\n", encoding="utf-8",
        )

    async def chatty_review(task, project_dir, project_context, args,
                            output=None):
        writer(Path(project_dir), task["id"])
        # The stdout text mentions CRITICAL/HIGH but those are prose.
        return {
            "success": True,
            "duration": 0.0,
            "result_text": (
                "[S1] LOW — this is NOT a CRITICAL vulnerability "
                "and would not be HIGH either."
            ),
        }

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 400, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 401, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )
    # Replace the review patch with the chatty one.
    patches[5] = patch(
        "equipa.dispatch.run_security_review", side_effect=chatty_review,
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], \
         patches[10], patches[11], patches[12]:
        await run_parallel_tasks([400, 401], args)

    # Artifact is MEDIUM-only — both branches must merge despite
    # 'CRITICAL'/'HIGH' substrings in the agent's prose.
    merged_ids = {tid for _, tid, _ in merge_calls}
    assert merged_ids == {400, 401}


@pytest.mark.asyncio
async def test_parallel_mode_skips_review_when_disabled(tmp_path):
    """When security_review is disabled, the helper is never called and
    every successful task is still merged (pre-2321 behaviour intact)."""
    args = _make_args(
        security_review=False, dispatch_config={"security_review": False},
    )

    def writer(task_dir: Path, task_id: int):  # pragma: no cover
        raise AssertionError("review must not run when disabled")

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 500, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 501, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5] as mock_review, patches[6], patches[7], patches[8], \
         patches[9], patches[10], patches[11], patches[12]:
        await run_parallel_tasks([500, 501], args)

    mock_review.assert_not_called()
    merged_ids = {tid for _, tid, _ in merge_calls}
    assert merged_ids == {500, 501}


# ---------------------------------------------------------------------------
# Task 2341 — fail-closed regressions for missing artifact + reviewer crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_artifact_blocks_merge_by_default(tmp_path):
    """S1 regression: review agent that does not write the artifact
    must NOT silently authorise the merge (pre-2341 vulnerability).
    """
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        # Reviewer runs but produces no artifact (e.g. crashed silently,
        # was steered off-task by description content, or wrote to the
        # wrong path).
        return None

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 600, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 601, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], \
         patches[9] as mock_status, patches[10], patches[11], patches[12]:
        await run_parallel_tasks([600, 601], args)

    assert merge_calls == [], "missing artifact must block merge by default"
    assert len(mock_status.call_args_list) == 2
    for c in mock_status.call_args_list:
        assert c[0][1] == "security_review_blocked", c


@pytest.mark.asyncio
async def test_review_crash_blocks_merge(tmp_path):
    """S2 regression: run_security_review raising must gate the merge
    even if a stale SECURITY-REVIEW-NNNN.md from a prior run exists.
    """
    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        # Simulate a stale clean review on disk from a previous run.
        # If the gate trusted the artifact alone, the crash would be
        # invisible and the merge would proceed.
        _write_review(task_dir / f"SECURITY-REVIEW-{task_id}.md")

    async def crashing_review(task, project_dir, project_context, args,
                              output=None):
        writer(Path(project_dir), task["id"])
        raise RuntimeError("network outage during review")

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 700, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 701, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )
    patches[5] = patch(
        "equipa.dispatch.run_security_review", side_effect=crashing_review,
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], \
         patches[9] as mock_status, patches[10], patches[11], patches[12]:
        await run_parallel_tasks([700, 701], args)

    assert merge_calls == [], (
        "reviewer crash must block merge regardless of stale artifact"
    )
    for c in mock_status.call_args_list:
        assert c[0][1] == "security_review_blocked", c


@pytest.mark.asyncio
async def test_missing_artifact_unblocks_with_flag(tmp_path):
    """Operator opt-out: setting features."
    "security_review_block_on_missing_artifact=False restores the
    pre-2341 fail-open behaviour for shops whose workflow needs it.
    """
    args = _make_args(
        security_review=True,
        dispatch_config={
            "security_review": True,
            "features": {
                "security_review": True,
                "security_review_block_on_missing_artifact": False,
            },
        },
    )

    def writer(task_dir: Path, task_id: int):
        return None  # no artifact

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 800, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
            {"id": 801, "project_id": 1, "title": "t2",
             "description": "d", "role": "developer"},
        ],
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], \
         patches[10], patches[11], patches[12]:
        await run_parallel_tasks([800, 801], args)

    merged_ids = {tid for _, tid, _ in merge_calls}
    assert merged_ids == {800, 801}, (
        "fail-open opt-out must allow merges with missing artifact"
    )


@pytest.mark.asyncio
async def test_review_crash_logs_traceback(tmp_path, caplog):
    """The exception handler still records logger.exception() output so
    operators can diagnose reviewer failures even though the gate now
    blocks rather than falling through.
    """
    import logging

    args = _make_args(
        security_review=True,
        dispatch_config={"security_review": True},
    )

    def writer(task_dir: Path, task_id: int):
        return None

    async def crashing_review(task, project_dir, project_context, args,
                              output=None):
        raise RuntimeError("boom")

    patches, merge_calls = _patch_parallel_mode(tmp_path, review_writer=writer)
    patches[0] = patch(
        "equipa.dispatch.fetch_tasks_by_ids",
        return_value=[
            {"id": 900, "project_id": 1, "title": "t1",
             "description": "d", "role": "developer"},
        ],
    )
    patches[5] = patch(
        "equipa.dispatch.run_security_review", side_effect=crashing_review,
    )

    with caplog.at_level(logging.ERROR, logger="equipa.dispatch"):
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11], patches[12]:
            await run_parallel_tasks([900], args)

    # logger.exception() records at ERROR level with the traceback;
    # capture confirms the diagnostic survived the new gating path.
    crash_records = [
        r for r in caplog.records
        if "security review crashed" in r.getMessage()
    ]
    assert crash_records, (
        "logger.exception must still fire so operators see the traceback"
    )
    assert crash_records[0].exc_info is not None
    # And the gate still blocked the merge.
    assert merge_calls == []
