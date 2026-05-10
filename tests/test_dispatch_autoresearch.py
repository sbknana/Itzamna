"""Regression tests for bug 2282 — run_dev_test_loop_with_autoresearch.

Bug 2282: parallel-mode --tasks N,M,O lost the autoresearch retry wrapper
that single-task mode had. Fixed by extracting the retry pattern into a
shared helper. These tests verify the helper retries on failure outcomes
and stops on success outcomes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from equipa.dispatch import run_dev_test_loop_with_autoresearch


def _make_args() -> MagicMock:
    args = MagicMock()
    args.dispatch_config = {}
    return args


@pytest.mark.asyncio
async def test_returns_immediately_on_tests_passed():
    """Success outcome breaks the retry loop on the first attempt."""
    task = {"id": 1}
    args = _make_args()
    config = {"features": {"autoresearch": True}, "autoresearch_max_retries": 5}

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(return_value=({"cost": 1.0, "duration": 10.0}, 1, "tests_passed")),
    ) as mock_loop:
        result, cycles, outcome, cost, duration, returned_task = (
            await run_dev_test_loop_with_autoresearch(
                task, "/tmp", {}, args, config,
            )
        )

    assert outcome == "tests_passed"
    assert cycles == 1
    assert cost == 1.0
    assert duration == 10.0
    assert returned_task is task
    mock_loop.assert_called_once()


@pytest.mark.asyncio
async def test_returns_immediately_on_no_tests():
    """no_tests outcome is also success — no retry."""
    task = {"id": 2}
    args = _make_args()
    config = {"features": {"autoresearch": True}, "autoresearch_max_retries": 5}

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(return_value=({"cost": 0.5, "duration": 5.0}, 1, "no_tests")),
    ) as mock_loop:
        _, _, outcome, _, _, _ = await run_dev_test_loop_with_autoresearch(
            task, "/tmp", {}, args, config,
        )

    assert outcome == "no_tests"
    mock_loop.assert_called_once()


@pytest.mark.asyncio
async def test_no_retry_when_autoresearch_disabled():
    """If the feature flag is off, failure is final on the first attempt."""
    task = {"id": 3}
    args = _make_args()
    config = {"features": {"autoresearch": False}, "autoresearch_max_retries": 5}

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(return_value=({"cost": 0.0, "duration": 1.0}, 1, "early_terminated")),
    ) as mock_loop:
        _, _, outcome, _, _, _ = await run_dev_test_loop_with_autoresearch(
            task, "/tmp", {}, args, config,
        )

    assert outcome == "early_terminated"
    mock_loop.assert_called_once()


@pytest.mark.asyncio
async def test_retries_until_success():
    """A failure followed by a success should result in 2 calls."""
    task = {"id": 4}
    args = _make_args()
    config = {"features": {"autoresearch": True}, "autoresearch_max_retries": 5}

    side_effect = [
        ({"cost": 1.0, "duration": 5.0}, 1, "early_terminated"),
        ({"cost": 1.0, "duration": 5.0}, 1, "tests_passed"),
    ]

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(side_effect=side_effect),
    ) as mock_loop, patch(
        "equipa.dispatch.cleanup_failed_attempt", AsyncMock()
    ), patch(
        "equipa.dispatch.fetch_task", return_value={"id": 4, "title": "x"}
    ):
        _, _, outcome, cost, duration, _ = await run_dev_test_loop_with_autoresearch(
            task, "/tmp", {}, args, config,
        )

    assert outcome == "tests_passed"
    # Costs accumulate across attempts
    assert cost == 2.0
    assert duration == 10.0
    assert mock_loop.call_count == 2


@pytest.mark.asyncio
async def test_retries_exhausted():
    """If every retry fails, the helper returns the final failed outcome."""
    task = {"id": 5}
    args = _make_args()
    config = {"features": {"autoresearch": True}, "autoresearch_max_retries": 2}

    side_effect = [
        ({"cost": 1.0, "duration": 5.0}, 1, "early_terminated"),
        ({"cost": 1.0, "duration": 5.0}, 1, "early_terminated"),
        ({"cost": 1.0, "duration": 5.0}, 1, "early_terminated"),
    ]

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(side_effect=side_effect),
    ) as mock_loop, patch(
        "equipa.dispatch.cleanup_failed_attempt", AsyncMock()
    ), patch(
        "equipa.dispatch.fetch_task", return_value={"id": 5, "title": "x"}
    ):
        _, _, outcome, _, _, _ = await run_dev_test_loop_with_autoresearch(
            task, "/tmp", {}, args, config,
        )

    assert outcome == "early_terminated"
    # max_retries=2 means 3 total attempts (initial + 2 retries)
    assert mock_loop.call_count == 3


@pytest.mark.asyncio
async def test_aborts_if_task_disappears():
    """If fetch_task returns None mid-retry, abort cleanly."""
    task = {"id": 6}
    args = _make_args()
    config = {"features": {"autoresearch": True}, "autoresearch_max_retries": 5}

    side_effect = [
        ({"cost": 1.0, "duration": 5.0}, 1, "early_terminated"),
        # fetch_task returning None should abort before this is reached
        ({"cost": 1.0, "duration": 5.0}, 1, "tests_passed"),
    ]

    with patch(
        "equipa.dispatch.run_dev_test_loop",
        AsyncMock(side_effect=side_effect),
    ) as mock_loop, patch(
        "equipa.dispatch.cleanup_failed_attempt", AsyncMock()
    ), patch(
        "equipa.dispatch.fetch_task", return_value=None
    ):
        _, _, outcome, _, _, _ = await run_dev_test_loop_with_autoresearch(
            task, "/tmp", {}, args, config,
        )

    # First call returned early_terminated; abort prevents second call
    assert outcome == "early_terminated"
    assert mock_loop.call_count == 1
