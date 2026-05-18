"""Tests for the pre_cycle hook's extra_context injection.

Backport commit a5adf49 added an extension point that lets a pre_cycle
hook callback return ``{"extra_context": "..."}``. The text is merged
into the developer prompt's ``extra_context`` for that cycle. Other
return shapes (None, non-dict, dict without ``extra_context``) must be
ignored without crashing. Duplicate text must not be appended twice.

These tests drive a single cycle of ``run_dev_test_loop`` with all the
heavy machinery stubbed out, capturing the ``extra_context`` argument
that ``build_system_prompt`` receives.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from equipa import loops


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeConn:
    def execute(self, *_a, **_kw):
        return self

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def stub_loops(monkeypatch):
    """Stub out everything ``run_dev_test_loop`` calls into so we can
    drive a single dev-test cycle deterministically.

    Returns a SimpleNamespace with:
        prompts: list[str] — successive ``extra_context`` values that
                 build_system_prompt was called with.
        fire_hook_returns: dict — mutable mapping
                 {hook_name: returned_list}; tests assign to
                 ``stub.fire_hook_returns["pre_cycle"]`` to control
                 what the hook returns.
    """

    prompts: list[str | None] = []
    fire_hook_returns: dict[str, list] = {}

    async def _async_none(*_a, **_kw):
        return None

    async def _preflight_ok(*_a, **_kw):
        return (True, "python", None)

    async def _fake_fire_hook(name, **_kwargs):
        return list(fire_hook_returns.get(name, []))

    async def _fake_dispatch(*_a, **_kw):
        # Return early_terminated so the loop exits after a single cycle.
        return {
            "success": False,
            "early_terminated": True,
            "early_term_reason": "stop_after_first_cycle",
            "cost": 0.0,
            "duration": 0.0,
            "errors": [],
            "result_text": "",
        }

    def _capture_prompt(_task, _ctx, _dir, **kw):
        prompts.append(kw.get("extra_context"))
        return "prompt"

    @contextlib.contextmanager
    def _fake_build_cli_command(*_a, **_kw):
        yield ["cmd"]

    # Heavy dep stubs
    monkeypatch.setattr(loops, "auto_install_dependencies", _async_none)
    monkeypatch.setattr(loops, "preflight_build_check", _preflight_ok)
    monkeypatch.setattr(loops, "get_db_connection", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(loops, "get_task_complexity", lambda _t: "simple")
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "test-model")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 10)
    monkeypatch.setattr(
        loops,
        "calculate_dynamic_budget",
        lambda max_turns, effort=None: (max_turns, max_turns),
    )
    monkeypatch.setattr(loops, "load_checkpoint", lambda *a, **kw: ("", 0))
    monkeypatch.setattr(loops, "fire_hook", _fake_fire_hook)
    monkeypatch.setattr(loops, "read_agent_messages", lambda *a, **kw: [])
    monkeypatch.setattr(loops, "build_system_prompt", _capture_prompt)
    monkeypatch.setattr(loops, "build_cli_command", _fake_build_cli_command)
    monkeypatch.setattr(loops, "_accumulate_cost", lambda *a, **kw: 0.0)
    monkeypatch.setattr(loops, "_check_cost_limit", lambda *a, **kw: None)
    monkeypatch.setattr(loops, "dispatch_agent", _fake_dispatch)

    import equipa.dispatch as _dispatch

    monkeypatch.setattr(_dispatch, "is_feature_enabled", lambda *a, **kw: False)

    return SimpleNamespace(prompts=prompts, fire_hook_returns=fire_hook_returns)


async def _run_one_cycle(stub):
    """Drive ``run_dev_test_loop`` for one cycle. Returns the captured
    extra_context that was passed to build_system_prompt."""
    task = {
        "id": 999_999_900,
        "description": "pre_cycle hook test",
        "role": "developer",
    }
    args = SimpleNamespace(dispatch_config=None)
    output = MagicMock()

    await loops.run_dev_test_loop(
        task,
        project_dir="/tmp",
        project_context={},
        args=args,
        output=output,
    )

    assert stub.prompts, "build_system_prompt was never called"
    return stub.prompts[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreCycleExtraContextInjection:
    """Successful injection path."""

    @pytest.mark.asyncio
    async def test_extra_context_merged_from_hook(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            {"extra_context": "INJECTED-FROM-PLUGIN"},
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert extra_context is not None
        assert "INJECTED-FROM-PLUGIN" in extra_context

    @pytest.mark.asyncio
    async def test_multiple_hooks_all_merged(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            {"extra_context": "FIRST-PLUGIN"},
            {"extra_context": "SECOND-PLUGIN"},
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert "FIRST-PLUGIN" in extra_context
        assert "SECOND-PLUGIN" in extra_context


class TestPreCycleExtraContextIgnored:
    """Non-injection return shapes must NOT crash and must not add text."""

    @pytest.mark.asyncio
    async def test_hook_returns_none(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [None]
        # No exception → already a pass; verify text was not added.
        extra_context = await _run_one_cycle(stub_loops)
        assert extra_context is not None  # base extra_context still built
        # extra_context might be empty string for a fresh task, that's fine

    @pytest.mark.asyncio
    async def test_hook_returns_non_dict(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = ["not a dict", 42, ["list"]]
        extra_context = await _run_one_cycle(stub_loops)
        # Did not crash. Sanity-check none of the bogus values bled through.
        assert "not a dict" not in (extra_context or "")
        assert "42" not in (extra_context or "")

    @pytest.mark.asyncio
    async def test_hook_returns_dict_without_extra_context(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            {"status": "ok", "metric": 1.0},
            {"extra_context": ""},  # falsy → must also be ignored
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert "status" not in (extra_context or "")
        assert "ok" not in (extra_context or "")

    @pytest.mark.asyncio
    async def test_hook_returns_empty_list(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = []
        extra_context = await _run_one_cycle(stub_loops)
        assert extra_context is not None  # base extra_context still built

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_entries(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            None,
            "bogus",
            {"unrelated_key": "value"},
            {"extra_context": "ONLY-VALID-ENTRY"},
            42,
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert "ONLY-VALID-ENTRY" in extra_context
        # Bogus entries did not poison the context.
        assert "value" not in extra_context
        assert "bogus" not in extra_context


class TestPreCycleDuplicateGuard:
    """The ``not in`` guard prevents the same text from being added twice."""

    @pytest.mark.asyncio
    async def test_same_text_from_two_hooks_appended_once(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            {"extra_context": "DUPLICATE-MARKER"},
            {"extra_context": "DUPLICATE-MARKER"},
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert extra_context.count("DUPLICATE-MARKER") == 1, (
            "Duplicate extra_context must be appended at most once "
            "(the `not in` guard in run_dev_test_loop)."
        )

    @pytest.mark.asyncio
    async def test_distinct_texts_both_appended(self, stub_loops):
        stub_loops.fire_hook_returns["pre_cycle"] = [
            {"extra_context": "ALPHA-MARKER"},
            {"extra_context": "BETA-MARKER"},
            {"extra_context": "ALPHA-MARKER"},  # duplicate of first
        ]
        extra_context = await _run_one_cycle(stub_loops)
        assert extra_context.count("ALPHA-MARKER") == 1
        assert extra_context.count("BETA-MARKER") == 1
