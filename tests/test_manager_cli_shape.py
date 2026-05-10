"""Tests for D3: manager.py uses build_cli_command instead of duplicating
the claude CLI argument list. Guards against regressions where planner or
evaluator dispatch drifts from the central command builder.

Updated for task 2311: build_cli_command is now a context manager that owns
its prompt-file lifetime. Tests must consume the cmd while inside the
``with`` block (the prompt file is deleted on exit).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from equipa import manager
from equipa.agent_runner import build_cli_command


def _make_args(model: str = "sonnet", max_turns: int = 20) -> SimpleNamespace:
    return SimpleNamespace(model=model, max_turns=max_turns)


def _capture_cmd():
    """Build a fake run_agent that captures the cmd AND the prompt-file body.

    The prompt file is deleted when the build_cli_command context exits, so
    we read it eagerly inside the fake (which runs while the context is open).
    """
    captured: dict = {}

    async def fake_run_agent(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        # Eagerly read the tempfile body — it's gone after the with-block exits.
        if "--append-system-prompt-file" in cmd:
            idx = cmd.index("--append-system-prompt-file")
            path = cmd[idx + 1]
            captured["prompt_file_path"] = path
            with open(path, encoding="utf-8") as f:
                captured["prompt_body"] = f.read()
        return {
            "success": True,
            "result_text": "TASKS_CREATED: 42\nGOAL_STATUS: complete",
            "duration": 0.0,
            "cost": 0.0,
        }

    return captured, fake_run_agent


def _assert_common_cli_shape(cmd: list[str], project_dir: str, model: str) -> None:
    """Assert the CLI command has the expected build_cli_command structure."""
    assert cmd[0] == "claude" or cmd[0].endswith("/claude") or cmd[0].endswith("\\claude")
    assert cmd[1] == "-p"

    # Required flag/value pairs
    expected_pairs = {
        "--output-format": "json",
        "--model": model,
        "--mcp-config": None,  # value not pinned (path varies)
        "--permission-mode": "bypassPermissions",
    }
    for flag, expected_val in expected_pairs.items():
        assert flag in cmd, f"missing flag: {flag}"
        if expected_val is not None:
            idx = cmd.index(flag)
            assert cmd[idx + 1] == expected_val, (
                f"{flag} expected {expected_val!r}, got {cmd[idx + 1]!r}"
            )

    # --add-dir occurs at least once with project_dir.
    add_dir_indices = [i for i, a in enumerate(cmd) if a == "--add-dir"]
    assert add_dir_indices, "missing --add-dir"
    assert any(cmd[i + 1] == project_dir for i in add_dir_indices), (
        f"--add-dir does not include project_dir={project_dir!r}"
    )

    # Other required single flags
    assert "--no-session-persistence" in cmd
    assert "--append-system-prompt-file" in cmd
    assert "--max-turns" in cmd


def test_planner_uses_build_cli_command():
    captured, fake_run_agent = _capture_cmd()

    async def go():
        with patch.object(manager, "run_agent", fake_run_agent), \
             patch.object(manager, "build_planner_prompt", return_value="PLANNER_SYS_PROMPT"):
            await manager.run_planner_agent(
                goal="ship feature X",
                project_id=1,
                project_dir="/tmp/proj",
                project_context={},
                args=_make_args(model="sonnet"),
            )

    asyncio.run(go())
    cmd = captured["cmd"]

    _assert_common_cli_shape(cmd, "/tmp/proj", "sonnet")

    # Planner-specific prompt override
    p_idx = cmd.index("-p")
    assert "tasks" in cmd[p_idx + 1].lower()
    assert "/tmp/proj" in cmd[p_idx + 1]

    # System prompt body propagated through the tempfile
    assert captured["prompt_body"] == "PLANNER_SYS_PROMPT"

    # Tempfile must be cleaned up after the context exits
    assert not os.path.exists(captured["prompt_file_path"]), (
        "prompt tempfile leaked past context-manager exit"
    )


def test_evaluator_uses_build_cli_command():
    captured, fake_run_agent = _capture_cmd()

    async def go():
        with patch.object(manager, "run_agent", fake_run_agent), \
             patch.object(manager, "build_evaluator_prompt", return_value="EVAL_SYS_PROMPT"):
            await manager.run_evaluator_agent(
                goal="ship feature X",
                project_id=1,
                project_dir="/tmp/proj",
                project_context={},
                completed_tasks=[],
                blocked_tasks=[],
                args=_make_args(model="opus"),
            )

    asyncio.run(go())
    cmd = captured["cmd"]

    _assert_common_cli_shape(cmd, "/tmp/proj", "opus")

    # Evaluator-specific prompt override
    p_idx = cmd.index("-p")
    assert "evaluate" in cmd[p_idx + 1].lower()
    assert "/tmp/proj" in cmd[p_idx + 1]

    assert captured["prompt_body"] == "EVAL_SYS_PROMPT"
    assert not os.path.exists(captured["prompt_file_path"])


def test_planner_and_evaluator_share_command_builder():
    """Both should produce structurally identical commands (modulo prompt strings,
    role-specific -p text, and per-role max-turns). This is the whole point of D3 —
    no duplicated build logic.
    """
    captured_planner, fake_run_planner = _capture_cmd()
    captured_eval, fake_run_eval = _capture_cmd()

    async def go():
        with patch.object(manager, "build_planner_prompt", return_value="X"):
            with patch.object(manager, "run_agent", fake_run_planner):
                await manager.run_planner_agent(
                    "g", 1, "/tmp/p", {}, _make_args(model="sonnet"),
                )
        with patch.object(manager, "build_evaluator_prompt", return_value="X"):
            with patch.object(manager, "run_agent", fake_run_eval):
                await manager.run_evaluator_agent(
                    "g", 1, "/tmp/p", {}, [], [], _make_args(model="sonnet"),
                )

    asyncio.run(go())

    p_cmd = captured_planner["cmd"]
    e_cmd = captured_eval["cmd"]

    # Both must share the same flag set (order may differ across roles only in the
    # -p prompt and --max-turns value).
    p_flags = {arg for arg in p_cmd if arg.startswith("--")}
    e_flags = {arg for arg in e_cmd if arg.startswith("--")}
    assert p_flags == e_flags, f"flag sets differ: {p_flags ^ e_flags}"


def test_planner_and_evaluator_roles_passed_to_builder():
    """Verify that role='planner' and role='evaluator' are forwarded to
    build_cli_command. These roles are not in ROLE_SKILLS (planner/evaluator
    are exempt research roles with no skill bundles), so build_cli_command
    must handle them gracefully via ROLE_SKILLS.get(role) returning None.
    """
    from equipa.constants import EARLY_TERM_EXEMPT_ROLES, ROLE_SKILLS

    # Sanity-check the contract this refactor relies on
    assert "planner" in EARLY_TERM_EXEMPT_ROLES
    assert "evaluator" in EARLY_TERM_EXEMPT_ROLES

    # build_cli_command must not crash when role has no entry in ROLE_SKILLS
    for role in ("planner", "evaluator"):
        with build_cli_command(
            "SYS_PROMPT",
            "/tmp/proj",
            max_turns=10,
            model="sonnet",
            role=role,
            prompt_message=f"role={role}",
        ) as cmd:
            # No skills directory for these roles → no extra --add-dir beyond project_dir
            add_dir_count = sum(1 for arg in cmd if arg == "--add-dir")
            skill_dir = ROLE_SKILLS.get(role)
            if skill_dir is None or not skill_dir.exists():
                assert add_dir_count == 1, (
                    f"role={role}: expected single --add-dir (no skills), "
                    f"got {add_dir_count}"
                )

    # Verify role kwarg propagates from manager to build_cli_command
    captured_role: dict = {}
    real_build = build_cli_command

    @contextlib.contextmanager
    def spy_build(*args, **kwargs):
        captured_role.setdefault("planner_calls", []).append(kwargs.get("role"))
        with real_build(*args, **kwargs) as cmd:
            yield cmd

    _, fake_run = _capture_cmd()

    async def go():
        with patch.object(manager, "build_planner_prompt", return_value="X"), \
             patch.object(manager, "build_cli_command", spy_build), \
             patch.object(manager, "run_agent", fake_run):
            await manager.run_planner_agent(
                "g", 1, "/tmp/p", {}, _make_args(model="sonnet"),
            )

    asyncio.run(go())
    assert captured_role["planner_calls"] == ["planner"]
