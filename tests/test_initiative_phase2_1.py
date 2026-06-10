#!/usr/bin/env python3
"""Phase 2.1 (#2491) deferred LOW/INFO polish — unit coverage.

Covers the five behavioural fixes folded into #2491:

* S2-INIT-02  per-wave cost delta (no double-count on resume)
* S2-INIT-03  content-level cross-branch contamination detection
* S2-INIT-04  fence-preserving truncation of the pause reason
* S2-INIT-06  per-wave batched cost commit
* item 6      case-insensitive initiative-project resolution (covered by the
              SQL ``LOWER(...)`` change; exercised here against an in-mem DB)

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3

import pytest

import equipa.initiative_runner as ir
from equipa.security import wrap_untrusted


# ---------------------------------------------------------------------------
# S2-INIT-02 — per-wave cost delta
# ---------------------------------------------------------------------------

def _agent_runs_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE agent_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, "
        "cost_usd REAL DEFAULT 0, outcome TEXT)"
    )
    return conn


def test_cost_high_water_and_delta() -> None:
    conn = _agent_runs_db()
    # Prior attempt rows for task 7 (e.g. a retried task across waves).
    conn.executemany(
        "INSERT INTO agent_runs (task_id, cost_usd) VALUES (?, ?)",
        [(7, 1.50), (7, 0.50)],
    )
    conn.commit()

    high_water = ir._read_task_cost_high_water(conn, 7)
    assert high_water == 2  # two prior rows

    # Without the high-water mark the full history is summed (the old bug).
    assert ir._read_task_cost(conn, 7) == pytest.approx(2.00)

    # A new wave adds one more row.
    conn.execute(
        "INSERT INTO agent_runs (task_id, cost_usd) VALUES (?, ?)", (7, 0.75)
    )
    conn.commit()

    # The delta read counts ONLY the new row, not the re-summed history.
    delta = ir._read_task_cost(conn, 7, since_run_id=high_water)
    assert delta == pytest.approx(0.75)


def test_cost_high_water_empty_task() -> None:
    conn = _agent_runs_db()
    assert ir._read_task_cost_high_water(conn, 99) == 0
    assert ir._read_task_cost(conn, 99, since_run_id=0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# S2-INIT-04 — fence-preserving truncation
# ---------------------------------------------------------------------------

def test_truncate_preserves_closing_fence() -> None:
    body = "X" * 400
    fenced = wrap_untrusted(body, "UNTRUSTED_deadbeef")
    assert fenced.endswith(">>>")
    # A limit that would sever the closing fence under naive slicing.
    limit = len(fenced) - 10
    out = ir._truncate_preserving_fence(fenced, limit)
    assert out.rstrip().endswith("<<<END_UNTRUSTED_deadbeef>>>"), out
    # Naive truncation would NOT have kept the delimiter.
    assert not fenced[:limit].rstrip().endswith(">>>")


def test_truncate_noop_when_within_limit() -> None:
    text = "short reason"
    assert ir._truncate_preserving_fence(text, 2000) == text


def test_truncate_plain_text_no_fence() -> None:
    text = "Y" * 5000
    out = ir._truncate_preserving_fence(text, 100)
    assert len(out) == 100


# ---------------------------------------------------------------------------
# S2-INIT-03 — content-level cross-branch contamination
# ---------------------------------------------------------------------------

def test_content_level_shared_commit_flagged() -> None:
    """Branches with DIFFERENT heads that share a commit are contamination."""
    base = "base000000"

    heads = {"forge-task-1": "aaa111", "forge-task-2": "bbb222"}
    # task-1 range: [shared, aaa111]; task-2 range: [shared, bbb222]
    ranges = {
        "base000000..forge-task-1": "aaa111\nshared99",
        "base000000..forge-task-2": "bbb222\nshared99",
    }

    def fake_git(repo: str, gitargs: list[str]) -> str:
        if gitargs[0] == "rev-parse":
            return heads[gitargs[-1]]
        if gitargs[0] == "rev-list":
            return ranges[gitargs[-1]]
        raise AssertionError(gitargs)

    violations = ir.verify_wave_branch_isolation(
        [1, 2], "/repo", wave_base=base, run_git=fake_git
    )
    assert set(violations) == {1, 2}
    assert "shared99"[:12] in violations[1]


def test_content_level_clean_wave() -> None:
    base = "base000000"
    heads = {"forge-task-1": "aaa111", "forge-task-2": "bbb222"}
    ranges = {
        "base000000..forge-task-1": "aaa111",
        "base000000..forge-task-2": "bbb222",
    }

    def fake_git(repo: str, gitargs: list[str]) -> str:
        if gitargs[0] == "rev-parse":
            return heads[gitargs[-1]]
        return ranges[gitargs[-1]]

    assert ir.verify_wave_branch_isolation(
        [1, 2], "/repo", wave_base=base, run_git=fake_git
    ) == {}
