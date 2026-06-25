"""Regression tests: agent runs must stamp a usable ``started_at`` marker.

Background
----------
``single_agent_guard.validate_tasks_created_claim`` rejects a ``TASKS_CREATED:``
line whose ids predate the run (the task #2361 hallucination: an agent claimed
to have created ids 78-82 that were really pre-existing January tickets). The
check is ``created_at < run_started``. The orchestrator wired that call up as
``run_started_at=result.get("started_at")`` -- but no runner ever set
``started_at`` on the result dict, so the value was always ``None`` and the
date-check was silently skipped. A same-project pre-existing id therefore
slipped straight through.

These tests pin two things:
* the top-level runners now stamp ``started_at`` onto the result, and
* the stamped value is **naive UTC**, so it compares correctly against
  ``tasks.created_at`` (SQLite ``CURRENT_TIMESTAMP`` == naive UTC) -- neither
  raising (aware vs naive) nor false-flagging in-run tasks (local-vs-UTC skew).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from equipa import agent_runner, dispatch
from equipa.single_agent_guard import _parse_iso_timestamp


def test_run_started_at_is_naive_utc() -> None:
    """The marker parses to a naive (tz-free) datetime within seconds of now-UTC."""
    raw = agent_runner._run_started_at_utc()
    parsed = _parse_iso_timestamp(raw)
    assert parsed is not None
    assert parsed.tzinfo is None  # naive -> safe to compare with DB timestamps
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc - parsed).total_seconds()) < 60


def test_marker_catches_same_project_preexisting_id() -> None:
    """With a real stamped marker, a same-project id created before the run is
    rejected -- the exact gap a ``None`` marker left open."""
    run_started = agent_runner._run_started_at_utc()
    db = MagicMock()
    db.fetch_tasks_by_ids.return_value = [
        # Same project (23), but created months before the run -> pre-existing.
        {"id": 78, "project_id": 23, "created_at": "2026-01-10 00:00:00"},
    ]

    verdict = dispatch.validate_tasks_created_claim(
        stdout="TASKS_CREATED: 78\n",
        run_started_at=run_started,
        expected_project_id=23,
        db=db,
    )

    assert verdict.is_valid is False
    assert 78 in verdict.invalid_ids


def test_marker_does_not_flag_in_run_task() -> None:
    """A task created *during* the run (UTC, just after start) stays valid --
    guards against a local-time marker false-flagging by the UTC offset."""
    run_started = agent_runner._run_started_at_utc()
    created_during = (
        _parse_iso_timestamp(run_started) + timedelta(seconds=60)
    ).isoformat()
    db = MagicMock()
    db.fetch_tasks_by_ids.return_value = [
        {"id": 500, "project_id": 23, "created_at": created_during},
    ]

    verdict = dispatch.validate_tasks_created_claim(
        stdout="TASKS_CREATED: 500\n",
        run_started_at=run_started,
        expected_project_id=23,
        db=db,
    )

    assert verdict.is_valid is True


def test_run_agent_with_retries_stamps_started_at(monkeypatch) -> None:
    """The top-level retry runner stamps started_at onto the returned result."""
    async def fake_run_agent(cmd: list[str]) -> dict[str, Any]:
        return {
            "success": True,
            "result_text": "ok",
            "num_turns": 1,
            "duration": 0.1,
            "cost": None,
            "errors": [],
        }

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_runner, "validate_output", lambda result: (True, ""))

    result, attempts = asyncio.run(
        agent_runner.run_agent_with_retries(["claude"], {"id": 1}, max_retries=3)
    )

    assert "started_at" in result
    assert _parse_iso_timestamp(result["started_at"]) is not None
