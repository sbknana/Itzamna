"""Regression tests for task #2311.

The PR-#8 fix that wrote the system prompt to a tempfile leaked one file per
``build_cli_command`` call because the cleanup helper was defined but never
called. This module asserts the post-2311 contract: ``build_cli_command`` is a
context manager that owns its tempfile and removes it on exit, even when the
caller raises inside the ``with`` block.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from equipa.agent_runner import build_cli_command


PROMPT_BODY = "SYSTEM PROMPT BODY — task 2311 regression."


def _read_prompt_file(cmd: list[str]) -> str:
    """Return the contents of the file pointed at by --append-system-prompt-file."""
    idx = cmd.index("--append-system-prompt-file")
    path = cmd[idx + 1]
    with open(path, encoding="utf-8") as f:
        return f.read()


def _prompt_path(cmd: list[str]) -> str:
    idx = cmd.index("--append-system-prompt-file")
    return cmd[idx + 1]


def test_build_cli_command_creates_tempfile_with_prompt():
    """Inside the with-block the prompt file exists and contains exactly the prompt."""
    with build_cli_command(
        PROMPT_BODY,
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
    ) as cmd:
        path = _prompt_path(cmd)
        assert os.path.exists(path), "prompt file must exist while context is open"
        assert _read_prompt_file(cmd) == PROMPT_BODY


def test_cleanup_removes_tempfile():
    """Exiting the context (normal exit) deletes the tempfile."""
    with build_cli_command(
        PROMPT_BODY,
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
    ) as cmd:
        captured_path = _prompt_path(cmd)
        assert os.path.exists(captured_path)

    assert not os.path.exists(captured_path), (
        f"prompt tempfile {captured_path} leaked after context exit"
    )


def test_cleanup_idempotent():
    """A second cleanup pass (file already gone) must not raise.

    The contextmanager's finally-block runs unconditionally; if the tempfile
    was removed externally before exit, the cleanup must swallow the
    FileNotFoundError silently. We simulate that by deleting the file from
    inside the with-block.
    """
    with build_cli_command(
        PROMPT_BODY,
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
    ) as cmd:
        path = _prompt_path(cmd)
        # External actor (or a previous cleanup) removes the file.
        os.unlink(path)
        assert not os.path.exists(path)
    # Reaching this line means the context manager's finally-block did NOT
    # raise on the missing file.


def test_cleanup_on_exception():
    """If the caller raises inside the with-block, cleanup still runs."""
    captured_path: dict[str, str] = {}

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with build_cli_command(
            PROMPT_BODY,
            project_dir="/tmp",
            max_turns=10,
            model="sonnet",
            role="developer",
        ) as cmd:
            captured_path["p"] = _prompt_path(cmd)
            assert os.path.exists(captured_path["p"])
            raise Boom("simulated subprocess crash")

    assert not os.path.exists(captured_path["p"]), (
        "prompt tempfile must be removed even when an exception propagates "
        "through the with-block"
    )


def test_no_module_level_tempfile_state():
    """The pre-2311 module-level _prompt_tempfiles list is gone — no global state."""
    import equipa.agent_runner as agent_runner

    assert not hasattr(agent_runner, "_prompt_tempfiles"), (
        "module-level _prompt_tempfiles must be removed (per-call ownership only)"
    )
    assert not hasattr(agent_runner, "cleanup_prompt_tempfiles"), (
        "module-level cleanup_prompt_tempfiles helper must be removed "
        "(context manager handles cleanup per call)"
    )


def test_concurrent_calls_have_independent_tempfiles():
    """Two nested with-blocks must produce two distinct files, each cleaned up
    independently when its own context exits. This is the parallel-mode hygiene
    guarantee that the global-list approach could not provide.
    """
    with build_cli_command(
        "PROMPT_A", project_dir="/tmp", max_turns=10, model="sonnet", role="developer",
    ) as cmd_a:
        path_a = _prompt_path(cmd_a)
        with build_cli_command(
            "PROMPT_B", project_dir="/tmp", max_turns=10, model="sonnet", role="developer",
        ) as cmd_b:
            path_b = _prompt_path(cmd_b)
            assert path_a != path_b
            assert _read_prompt_file(cmd_a) == "PROMPT_A"
            assert _read_prompt_file(cmd_b) == "PROMPT_B"
        # Inner context exited — only path_b should be gone.
        assert not os.path.exists(path_b)
        assert os.path.exists(path_a)
    assert not os.path.exists(path_a)


def test_dry_run_scanner_reports_prompt_file_size_and_cleans_up():
    """Drive the exact cli.py dry-run scanner logic against build_cli_command.

    Pre-2311, the dry-run scanner relied on the module-level _prompt_tempfiles
    list (which was never drained), so dry-run runs littered /tmp with prompt
    files. Post-2311 the dry-run branch in cli.py opens the contextmanager
    around its scan loop, so the tempfile is removed when the loop exits —
    even though the cmd is never exec'd.

    The scanner code in cli.py:925 looks like this::

        with build_cli_command(...) as cmd:
            for i, part in enumerate(cmd):
                if i > 0 and cmd[i - 1] == "--append-system-prompt-file":
                    sz = os.path.getsize(part)   # report file size
                    print(f"  [system prompt file: {part} ({sz} bytes)]")

    This test replicates that scan and asserts:
      1. ``os.path.getsize`` returns >0 for the prompt file (file readable
         while context is open)
      2. The file path is removed once the context exits
    """
    prompt_body = "DRY_RUN_PROMPT_BODY for size check"
    captured_path: str | None = None
    captured_size: int | None = None
    output_lines: list[str] = []

    with build_cli_command(
        prompt_body,
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
    ) as cmd:
        # Replicate cli.py:925-955 dry-run scanner verbatim.
        output_lines.append("--- DRY RUN ---")
        output_lines.append(f"Command ({len(cmd)} args):")
        for i, part in enumerate(cmd):
            if i > 0 and cmd[i - 1] == "--append-system-prompt-file":
                try:
                    sz = os.path.getsize(part)
                except OSError:
                    sz = -1
                captured_path = part
                captured_size = sz
                output_lines.append(f"  [system prompt file: {part} ({sz} bytes)]")
            elif len(part) > 100:
                output_lines.append(f"  {part[:100]}...")
            else:
                output_lines.append(f"  {part}")
        output_lines.append("--- END DRY RUN ---")

    # Scanner must have reported a positive byte-count.
    assert captured_path is not None, "scanner did not see --append-system-prompt-file"
    assert captured_size is not None and captured_size > 0, (
        f"dry-run scanner reported size {captured_size} for prompt file "
        f"{captured_path} — file should be readable while context is open"
    )
    # Body length sanity check — the file content is exactly the prompt body.
    assert captured_size == len(prompt_body.encode("utf-8")), (
        f"prompt-file size {captured_size} does not match prompt body "
        f"length {len(prompt_body.encode('utf-8'))}"
    )
    # Print line is well-formed
    assert any("[system prompt file:" in line and "bytes" in line
               for line in output_lines), (
        f"dry-run scanner output missing prompt-file line:\n"
        + "\n".join(output_lines)
    )
    # Tempfile must be gone now that the with-block exited.
    assert not os.path.exists(captured_path), (
        f"dry-run leaked prompt tempfile at {captured_path} — context "
        f"manager cleanup did not fire on exit"
    )
