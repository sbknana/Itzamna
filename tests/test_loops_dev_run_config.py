"""Regression test for TheForge task #2095 / architecture review #2093 finding S1.

Bug:
    ``equipa/loops.py::run_dev_test_loop`` referenced the local
    ``dev_run_config`` dict at two sites (read at ~line 707, write at
    ~line 829) without ever initialising it. Every dev-test cycle that
    entered the analysis-paralysis retry path therefore raised
    ``NameError: name 'dev_run_config' is not defined`` at module import
    of ``equipa.loops`` was fine, but at the FIRST call to
    ``dispatch_agent`` the lookup of ``dev_run_config.get(...)`` blew up.
    The orchestrator could not even dispatch a fix for itself because
    its own startup hit the same NameError path.

Bootstrap fix (commit 1a9d945, 2026-05-02):
    Inserted ``dev_run_config: dict[str, Any] = {}`` near line 540,
    BEFORE the cycle loop, so both reference sites work.

This test guards the fix two ways:

1. **Static check** — inspect the source of ``run_dev_test_loop`` and
   assert that an assignment to ``dev_run_config`` appears before any
   reference to it. If a future refactor deletes the init line again,
   this fails immediately at collection time.

2. **Runtime check** — drive one full dev-test cycle through
   ``run_dev_test_loop`` with the heavy deps stubbed. The cycle is
   forced down the analysis-paralysis branch, which exercises both
   reference sites (``dev_run_config.get(...)`` at line ~707 and
   ``dev_run_config["_paralysis_retry_count"] = ...`` at line ~829).
   On the second cycle the test verifies the read site picked up the
   value the write site stored (paralysis_retry_count=1). If the init
   is removed, the very first dispatch raises NameError before this
   assertion is reachable.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import inspect
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from equipa import loops


# --- Static guard ---------------------------------------------------------


def test_dev_run_config_initialised_before_use():
    """The init must precede every reference in run_dev_test_loop's body."""
    source = inspect.getsource(loops.run_dev_test_loop)

    # Find the line that assigns the empty dict.
    init_match = re.search(
        r"^\s*dev_run_config\s*:\s*dict\[str,\s*Any\]\s*=\s*\{\s*\}\s*$",
        source,
        flags=re.MULTILINE,
    )
    assert init_match is not None, (
        "Expected `dev_run_config: dict[str, Any] = {}` to be initialised "
        "near the top of run_dev_test_loop. The bootstrap fix from "
        "commit 1a9d945 (TheForge task #2095) was removed or moved."
    )

    init_offset = init_match.start()

    # Every other reference must come AFTER the init.
    for ref in re.finditer(r"\bdev_run_config\b", source):
        if ref.start() == init_match.start() + (
            init_match.group(0).find("dev_run_config")
        ):
            continue
        if ref.start() < init_offset:
            line_no = source.count("\n", 0, ref.start()) + 1
            pytest.fail(
                "dev_run_config is referenced (line "
                f"{line_no} of run_dev_test_loop) BEFORE its initialisation. "
                "This re-introduces the NameError that task #2095 fixed."
            )


# --- Runtime guard --------------------------------------------------------


class _FakeConn:
    """Minimal stand-in for a sqlite3 connection used in the loop."""

    def execute(self, *_args, **_kwargs):
        return self

    def commit(self):
        pass

    def close(self):
        pass


@pytest.mark.asyncio
async def test_run_dev_test_loop_completes_without_name_error(monkeypatch):
    """Run one analysis-paralysis cycle and prove dev_run_config is live.

    First dispatch returns ``early_terminated`` with a paralysis reason,
    which triggers the write at line ~829. Second dispatch returns a
    non-paralysis early-termination, which exits the loop. The second
    dispatch's ``paralysis_retry_count`` kwarg proves the read at line
    ~707 returned the value the write site stored.
    """

    async def _async_none(*_args, **_kwargs):
        return None

    async def _preflight_ok(*_args, **_kwargs):
        return (True, "python", None)

    dispatch_calls: list[dict] = []

    async def _fake_dispatch(*_args, **kwargs):
        dispatch_calls.append(kwargs)
        if len(dispatch_calls) == 1:
            return {
                "success": False,
                "early_terminated": True,
                "early_term_reason": "Killed without file changes",
                "cost": 0.0,
                "duration": 0.0,
                "errors": [],
                "result_text": "",
            }
        return {
            "success": False,
            "early_terminated": True,
            "early_term_reason": "stop_after_paralysis_in_test",
            "cost": 0.0,
            "duration": 0.0,
            "errors": [],
            "result_text": "",
        }

    # Heavy dep stubs ------------------------------------------------------
    monkeypatch.setattr(loops, "auto_install_dependencies", _async_none)
    monkeypatch.setattr(loops, "preflight_build_check", _preflight_ok)
    monkeypatch.setattr(loops, "get_db_connection", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(loops, "get_task_complexity", lambda _t: "simple")
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "test-model")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 10)
    monkeypatch.setattr(
        loops, "calculate_dynamic_budget",
        lambda max_turns, effort=None: (max_turns, max_turns)
    )
    monkeypatch.setattr(loops, "load_checkpoint", lambda *a, **kw: ("", 0))
    monkeypatch.setattr(loops, "fire_hook", _async_none)
    monkeypatch.setattr(loops, "read_agent_messages", lambda *a, **kw: [])
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    # build_cli_command is now a contextmanager; the mock must yield the cmd
    # so the `with build_cli_command(...) as cmd:` callsites still work.
    @contextlib.contextmanager
    def _fake_build_cli_command(*_a, **_kw):
        yield ["cmd"]
    monkeypatch.setattr(loops, "build_cli_command", _fake_build_cli_command)
    monkeypatch.setattr(loops, "_accumulate_cost", lambda *a, **kw: 0.0)
    monkeypatch.setattr(loops, "_check_cost_limit", lambda *a, **kw: None)
    monkeypatch.setattr(loops, "dispatch_agent", _fake_dispatch)

    # is_feature_enabled is imported lazily inside the function via
    # `from equipa.dispatch import is_feature_enabled`, so patch the
    # source module.
    import equipa.dispatch as _dispatch

    monkeypatch.setattr(_dispatch, "is_feature_enabled", lambda *a, **kw: False)

    task = {"id": 999_999_001, "description": "regression-test", "role": "developer"}
    project_context: dict = {}
    args = SimpleNamespace(dispatch_config=None)

    # Capture log output so the test stays quiet on success.
    output = MagicMock()

    result, cycles_completed, outcome_reason = await loops.run_dev_test_loop(
        task, project_dir="/tmp", project_context=project_context,
        args=args, output=output,
    )

    # The loop must have made it past line ~707 (read) AND line ~829 (write)
    # without raising NameError. Two dispatches mean both cycles ran.
    assert len(dispatch_calls) == 2, (
        "Expected 2 dispatch calls (paralysis retry + final exit). "
        f"Got {len(dispatch_calls)}: {[c.get('paralysis_retry_count') for c in dispatch_calls]}"
    )

    # First cycle: write site sets _paralysis_retry_count=1.
    # Second cycle: read site at line ~707 returns 1 and passes it through
    # as the paralysis_retry_count kwarg. This proves both sites work
    # against the same dict.
    assert dispatch_calls[0].get("paralysis_retry_count") == 0
    assert dispatch_calls[1].get("paralysis_retry_count") == 1, (
        "Second dispatch did not see the paralysis count written by the "
        "first cycle. Either the read at line ~707 or the write at line "
        "~829 is broken — the dev_run_config init is the load-bearing "
        "piece (TheForge task #2095)."
    )

    assert outcome_reason == "early_terminated"
    assert cycles_completed == 2
    assert result["early_terminated"] is True
