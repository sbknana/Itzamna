#!/usr/bin/env python3
"""Claude Code PreToolUse hook — pre-execution Bash security gate.

This is the *prevention* half of EQUIPA's bash security story. The reactive
stream observer in ``equipa.agent_runner`` sees a Bash tool call only AFTER
the Claude CLI subprocess has already executed it — that is
detect-and-terminate, not prevention. This hook, when wired into the spawned
CLI via a generated ``--settings`` file (feature flag
``features.bash_security_pretooluse``, DEFAULT OFF), runs
``equipa.bash_security.check_bash_command`` on the Bash command BEFORE the
tool is allowed to run, giving true pre-execution blocking.

Contract (Claude Code PreToolUse hook, exit-code form)
------------------------------------------------------
* Reads the tool-call JSON from stdin::

      {"hook_event_name": "PreToolUse", "tool_name": "Bash",
       "tool_input": {"command": "...", "description": "..."}}

* Exit 0  -> allow the tool call.
* Exit 2  -> block the tool call; stderr is fed back to the agent so it can
             see WHY and self-correct. Used only for genuinely unsafe commands.
* Any other tool (``tool_name != "Bash"``), an empty command, unparseable
  stdin, or a failure to load the checker -> exit 0 (fail-OPEN). A hook
  infrastructure error must never brick the agent on every command; the
  reactive stream check in agent_runner remains as defense-in-depth. Only a
  command that the checker positively flags as unsafe is blocked.

Import resolution
-----------------
The script may be invoked with an arbitrary cwd (Claude Code runs hooks from
the session directory). It resolves its own repository root from ``__file__``
and loads ``equipa/bash_security.py`` DIRECTLY by file path via importlib,
deliberately bypassing ``equipa/__init__.py`` — that package initializer
imports the whole orchestrator (cli, dispatch, loops, manager, mcp_server),
which would be far too heavy and fragile for a gate that runs before every
single bash command. ``bash_security`` is documented as a standalone,
zero-EQUIPA-import module, so loading it in isolation is safe and fast. The
repo root is also inserted into ``sys.path`` so an ordinary
``import equipa.bash_security`` would resolve as a fallback.

Standard-library only — NO third-party dependencies (Claude Code may run this
hook in a minimal environment).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

# Exit codes per the Claude Code PreToolUse hook contract.
EXIT_ALLOW = 0
EXIT_BLOCK = 2


def _repo_root() -> Path:
    """Return the repository root (the directory that contains ``equipa/``).

    ``__file__`` lives at ``<repo_root>/hooks/pretooluse_bash_gate.py``; the
    root is therefore two levels up. ``resolve()`` makes this robust to being
    invoked via a relative path or a symlink.
    """
    return Path(__file__).resolve().parent.parent


def _load_check_bash_command() -> Callable[[str], Any]:
    """Load ``check_bash_command`` without triggering ``equipa/__init__.py``.

    Loads ``equipa/bash_security.py`` as an isolated module by file path.
    Falls back to a normal package import (after putting the repo root on
    ``sys.path``) if the direct load fails for any reason.

    Raises:
        ImportError: if the checker cannot be loaded by either strategy.
    """
    root = _repo_root()
    module_path = root / "equipa" / "bash_security.py"

    # Primary: direct file-path load — fast, and does NOT execute the heavy
    # equipa package initializer.
    if module_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "equipa_bash_security_gate", module_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            # Register before exec: bash_security defines a @dataclass, and
            # dataclasses resolves ``cls.__module__`` via ``sys.modules`` — an
            # unregistered module makes that lookup return None and raise
            # ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
            check = getattr(module, "check_bash_command", None)
            if callable(check):
                return check

    # Fallback: ordinary package import (accepts the __init__ cost). Only
    # reached if the direct load above did not yield the symbol.
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from equipa.bash_security import check_bash_command  # type: ignore

    return check_bash_command


def _read_tool_input() -> dict[str, Any]:
    """Parse the PreToolUse payload from stdin.

    Returns the decoded object (expected to be a dict). Returns an empty dict
    on any read/parse error so the caller can fail-open.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_bash_command(payload: dict[str, Any]) -> str | None:
    """Return the Bash command to check, or None if this call is not our gate.

    None means "not a Bash tool call, or no command present" — the caller
    treats that as allow (this hook only guards the Bash tool).
    """
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


def main() -> int:
    """Run the gate. Returns the process exit code (0 allow / 2 block)."""
    payload = _read_tool_input()
    command = _extract_bash_command(payload)
    if command is None:
        # Not a Bash tool call (or nothing to check) — allow.
        return EXIT_ALLOW

    try:
        check_bash_command = _load_check_bash_command()
    except (ImportError, OSError, AttributeError) as exc:
        # Infrastructure failure loading the checker. Fail OPEN so the agent
        # is not wedged on every command; the reactive stream check in
        # agent_runner still provides detect-and-terminate coverage.
        print(
            f"pretooluse_bash_gate: could not load checker ({exc}); allowing "
            "command (reactive stream check remains active)",
            file=sys.stderr,
        )
        return EXIT_ALLOW

    result = check_bash_command(command)
    if getattr(result, "safe", True):
        return EXIT_ALLOW

    check_id = getattr(result, "check_id", 0)
    message = getattr(result, "message", "unsafe command")
    # stderr is surfaced to the agent on exit 2 so it can self-correct.
    print(
        f"BashSecurity check {check_id}: {message} — command blocked before "
        "execution by the EQUIPA pre-execution gate.",
        file=sys.stderr,
    )
    return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
