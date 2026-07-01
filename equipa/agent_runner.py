"""EQUIPA agent_runner — agent dispatch, streaming, and retry logic.

Layer 6: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.security, equipa.roles.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypedDict

logger = logging.getLogger(__name__)


def _run_started_at_utc() -> str:
    """Naive-UTC ISO timestamp marking when an agent run began.

    Stamped onto every run result so consumers (notably the single-agent
    guard's ``validate_tasks_created_claim``, which rejects a ``TASKS_CREATED``
    line referencing task ids that predate the run) can tell a freshly-created
    task from a pre-existing/hallucinated one.

    MUST be naive UTC: ``tasks.created_at`` is ``DEFAULT CURRENT_TIMESTAMP``,
    which SQLite stores as naive UTC. A tz-aware value would raise on the
    ``created < run_started`` comparison (naive vs aware), and a local-time
    value would false-flag tasks legitimately created *during* the run as
    pre-existing (off by the UTC offset).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

if TYPE_CHECKING:
    from equipa.prompts import PromptResult


# Module-level cache of recent tool calls per (task_id, role). Populated by
# the streaming loop via :func:`_record_tool_call`; read by
# :func:`get_recent_tool_calls` from ``equipa.sessions`` at cycle-boundary
# capture time. The buffer storage inside the loop (``tool_history``) is
# unchanged — this is a tiny parallel mirror so the orchestrator-cycle
# session capture can read recent activity without reaching into a frame
# local. PLAN-1067 §2.B2.
_RECENT_TOOL_CALLS: dict[tuple[int, str], list[dict[str, Any]]] = {}
_RECENT_TOOL_CALLS_MAX: int = 50


def _record_tool_call(
    task_id: int,
    role: str,
    tool: str,
    *,
    turn: int,
    ok: bool,
    args_hash: str = "",
) -> None:
    """Append a tool-call record to the per-(task, role) ring buffer.

    Caps the buffer at :data:`_RECENT_TOOL_CALLS_MAX` to keep memory bounded
    for long-running agents. Failure to record is non-fatal.
    """
    try:
        key = (int(task_id), str(role))
    except (TypeError, ValueError):
        return
    buf = _RECENT_TOOL_CALLS.setdefault(key, [])
    buf.append({
        "tool": tool,
        "args_hash": args_hash,
        "ok": bool(ok),
        "turn": int(turn),
    })
    if len(buf) > _RECENT_TOOL_CALLS_MAX:
        del buf[: len(buf) - _RECENT_TOOL_CALLS_MAX]


def get_recent_tool_calls(
    task_id: int,
    role: str,
    n: int = 20,
) -> list[dict[str, Any]]:
    """Return up to the last ``n`` tool calls recorded for ``(task_id, role)``.

    Returns the list in chronological order (oldest first). Returns an empty
    list if nothing has been recorded for this pair — callers must treat the
    buffer as best-effort. This is the ONE accessor exposed by
    ``agent_runner`` for orchestrator-cycle session capture (PLAN-1067 §2.B2);
    the underlying ``tool_history`` remains a frame-local list inside the
    streaming loop.
    """
    if n <= 0:
        return []
    key = (int(task_id), str(role))
    buf = _RECENT_TOOL_CALLS.get(key)
    if not buf:
        return []
    return list(buf[-n:])


class _AgentResultRequired(TypedDict):
    """Always-present keys in an AgentResult.

    Every return path of dispatch_agent and the underlying runners must set
    these six keys. They form the core contract relied on by callers
    (loops.py, dispatch.py, manager.py).
    """

    success: bool
    result_text: str
    num_turns: int
    duration: float
    cost: float | None
    errors: list[str]


class AgentResult(_AgentResultRequired, total=False):
    """Return shape for dispatch_agent and the underlying agent runners.

    The six required keys are declared on _AgentResultRequired. The keys
    below are optional — they are added by specific code paths (streaming
    runner, RLM decompose, callers that annotate results after dispatch).

    Adding a new key? Declare it here so mypy can catch typos at call sites.
    """

    # Streaming runner (run_agent_streaming) additions
    has_file_changes: bool
    early_terminated: bool
    early_term_reason: str
    early_completed: bool
    early_complete_reason: str
    compaction_count: int
    compaction_signals: list[dict[str, str]]
    files_read: list[str]
    files_changed_set: list[str]
    action_log: list[dict[str, Any]]

    # RLM decompose path additions
    rlm_decompose: bool
    files_examined: list[str]

    # Caller-injected annotations (set by loops.py after dispatch)
    turns_allocated: int
    turns_max: int

    # Run-start marker (naive UTC ISO), set by the top-level runners so the
    # single-agent guard can date-check a TASKS_CREATED claim. See
    # _run_started_at_utc.
    started_at: str

from equipa.abort_controller import AbortController, create_child_abort_controller
from equipa.bash_security import check_bash_command
from equipa.config import is_feature_enabled, load_dispatch_config
from equipa.constants import (
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_WARN_TURNS,
    MCP_CONFIG,
    PROCESS_TIMEOUT,
    ROLE_SKILLS,
)
from equipa.db import bulk_log_agent_actions, classify_error
from equipa.checkpoints import (
    SOFT_CHECKPOINT_INTERVAL,
    save_soft_checkpoint,
)
from equipa.monitoring import (
    LOOP_TERMINATE_THRESHOLD,
    LOOP_WARNING_THRESHOLD,
    _build_streaming_result,
    _build_tool_signature,
    _check_git_changes,
    _check_monologue,
    _check_stuck_phrases,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
    detect_compaction_signals,
)
from equipa.output import log
from equipa.parsing import validate_output
from equipa.security import verify_skill_integrity
from equipa.tasks import verify_task_updated

# Retry configuration from Claude Code withRetry.ts
BASE_DELAY_MS = 500
MAX_BACKOFF_MS = 32000
MAX_529_RETRIES = 3  # After 3x 529/overloaded, fall back to cheaper model
JITTER_FACTOR = 0.25  # 25% jitter as per Claude Code

# Persistent retry mode (for unattended sessions)
PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000  # 5 minutes
PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000  # 6 hours
HEARTBEAT_INTERVAL_MS = 30_000  # 30 seconds


def get_retry_delay(
    attempt: int,
    max_delay_ms: int = MAX_BACKOFF_MS,
    persistent: bool = False,
) -> float:
    """Exponential backoff with 25% jitter (Claude Code pattern).

    Args:
        attempt: Attempt number (1-indexed)
        max_delay_ms: Maximum delay cap in milliseconds
        persistent: If True, use persistent retry mode (higher backoff for unattended)

    Returns:
        Delay in seconds
    """
    if persistent:
        max_delay_ms = min(max_delay_ms, PERSISTENT_MAX_BACKOFF_MS)

    base_delay = min(BASE_DELAY_MS * math.pow(2, attempt - 1), max_delay_ms)
    jitter = random.random() * JITTER_FACTOR * base_delay
    return (base_delay + jitter) / 1000.0  # Convert ms to seconds


def is_overloaded_error(stderr: str, stdout: str) -> bool:
    """Detect 529/overloaded errors from Claude CLI output.

    Args:
        stderr: Standard error text
        stdout: Standard output text (may contain JSON error)

    Returns:
        True if this is an overloaded/529 error
    """
    combined = f"{stderr} {stdout}".lower()
    return any(marker in combined for marker in [
        "529",
        "overloaded",
        "overloaded_error",
        "temporarily overloaded",
    ])


def is_transient_capacity_error(stderr: str, stdout: str) -> bool:
    """Check if error is a transient capacity issue (429 or 529/overloaded).

    These errors are suitable for persistent retry mode with long backoff.

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if this is a 429 or 529 capacity error
    """
    combined = f"{stderr} {stdout}".lower()
    return any(marker in combined for marker in ["429", "rate limit", "529", "overloaded"])


def is_retryable_error(stderr: str, stdout: str) -> bool:
    """Check if error is retryable (network, timeout, 429, 500-level).

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if error should be retried
    """
    combined = f"{stderr} {stdout}".lower()
    retryable_markers = [
        "429",
        "rate limit",
        "connection",
        "timeout",
        "econnreset",
        "epipe",
        "500",
        "502",
        "503",
        "504",
    ]
    return any(marker in combined for marker in retryable_markers)


@contextlib.contextmanager
def build_cli_command(
    system_prompt: str | PromptResult,
    project_dir: str,
    max_turns: int,
    model: str,
    role: str = "developer",
    streaming: bool = False,
    prompt_message: str | None = None,
) -> Iterator[list[str]]:
    """Build the claude CLI command as a context manager that owns its tempfile.

    Yields the command list. On exit (normal or exceptional), the temp file
    holding the system prompt is removed. Callers MUST consume the cmd while
    inside the ``with`` block::

        with build_cli_command(prompt, dir, 50, "opus") as cmd:
            result = await run_agent(cmd)

    Args:
        system_prompt: Full system prompt string, or PromptResult from
            build_system_prompt(). PromptResult is coerced to str via
            __str__() which returns the full prompt with boundary marker.
        streaming: If True, use stream-json output format for real-time monitoring.
        prompt_message: Optional override for the user-facing -p message. Defaults
            to a generic "Execute the task..." instruction. Manager-mode dispatch
            (planner/evaluator) supplies role-specific text here.

    Yields:
        The argv list to pass to ``asyncio.create_subprocess_exec`` /
        ``subprocess.run``. The list contains a path to a tempfile that is
        deleted when the context exits — do NOT retain ``cmd`` past the
        ``with`` block.
    """
    # Explicit str() ensures PromptResult.__str__() is called, producing
    # the full prompt with SYSTEM_PROMPT_DYNAMIC_BOUNDARY marker.
    prompt_str = str(system_prompt)
    output_format = "stream-json" if streaming else "json"
    user_prompt = prompt_message or (
        f"Execute the task described in your system prompt. Work in: {project_dir}"
    )
    claude_bin = shutil.which("claude") or "claude"

    # Write system prompt to a temp file to avoid Windows command-line length
    # limits (WinError 206, ~8191 chars). delete=False so the async subprocess
    # can reopen it; the finally-block below removes it on context exit.
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="equipa_prompt_",
        delete=False, encoding="utf-8",
    )
    try:
        prompt_file.write(prompt_str)
        prompt_file.close()

        cmd = [
            claude_bin,
            "-p",
            user_prompt,
            "--output-format", output_format,
            "--model", model,
            "--max-turns", str(max_turns),
            "--no-session-persistence",
            "--append-system-prompt-file", prompt_file.name,
            "--mcp-config", str(MCP_CONFIG),
            "--add-dir", str(project_dir),
            "--permission-mode", "bypassPermissions",
        ]

        # Load effort flag from dispatch_config — production-only config-driven setting.
        # When dispatch_config.json has 'effort' set (e.g. "high"/"xhigh"/"max"), pass
        # it to the Claude CLI for extended thinking. No-op when unset (CLI uses default).
        try:
            _dc = load_dispatch_config(None)
            _effort = _dc.get('effort')
            if _effort:
                cmd.extend(["--effort", _effort])
        except (ImportError, FileNotFoundError, OSError, KeyError, ValueError):
            pass  # config missing/unloadable → CLI default effort

        # stream-json requires --verbose
        if streaming:
            cmd.append("--verbose")

        # Load role-specific skills directory if it exists
        skills_dir = ROLE_SKILLS.get(role)
        if skills_dir and skills_dir.exists():
            cmd.extend(["--add-dir", str(skills_dir)])

        yield cmd
    finally:
        # Idempotent: missing_ok=True means a second cleanup (or one after a
        # partial setup failure) does not raise.
        try:
            os.unlink(prompt_file.name)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "Failed to remove agent prompt tempfile: %s", prompt_file.name,
                exc_info=True,
            )


def _evaluate_paralysis_retry_read_gate(
    paralysis_retry_count: int,
    turn_count: int,
    tool_name: str,
    has_any_file_change: bool,
    must_write_next_turn: bool,
) -> tuple[str | None, bool]:
    """Decide how to handle a read-only first tool call on a paralysis retry.

    Pure helper so the gate logic is unit-testable. Mirrors the in-loop check
    inside ``_run_agent_streaming_impl``.

    Behavior:
        * Not on a paralysis retry, agent already wrote, or tool is not read-only
          → no-op (None, must_write_next_turn unchanged).
        * On paralysis retry >= 1 with a read-only tool on turn 1 (and we have
          not already armed must_write_next_turn) → allow ONE read and arm
          must_write_next_turn so the NEXT call must be Edit/Write or the agent
          dies on the regular paralysis path.

    The pre-2026-05-03 behavior killed instantly on retry >= 2 even on turn 1.
    That made refactor tasks unsatisfiable: after a paralysis kill the agent
    has zero forward-context (soft_checkpoint only stores the file list at
    kill time, not file contents) and must read at least one file to know what
    to edit. Forbidding all reads guaranteed a kill loop. The single-read
    allowance is preserved by the regular FAST_ESCALATION + must_write logic
    that fires on the very next call.

    Args:
        paralysis_retry_count: 0 on the first attempt, 1+ after each paralysis
            kill.
        turn_count: 1-indexed agent turn count.
        tool_name: Name of the tool being invoked (e.g. "Read", "Edit").
        has_any_file_change: True once the agent has produced any file change.
        must_write_next_turn: Current state of the must-write enforcement flag.

    Returns:
        Tuple of (early_term_reason, must_write_next_turn). early_term_reason
        is always None now — the prior kill branch was unsatisfiable.
    """
    if paralysis_retry_count <= 0 or has_any_file_change:
        return None, must_write_next_turn
    if tool_name not in ("Read", "Grep", "Glob", "Agent"):
        return None, must_write_next_turn
    if turn_count == 1 and not must_write_next_turn:
        return None, True
    return None, must_write_next_turn


async def run_agent(
    cmd: list[str],
    timeout: int | None = None,
    max_retries: int = 10,
    fallback_model: str | None = None,
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
) -> AgentResult:
    """Spawn claude -p with retry logic, exponential backoff, and model fallback.

    Implements Claude Code withRetry.ts pattern:
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - After 3 consecutive 529/overloaded errors, fall back to cheaper model
    - Retries on 429, 5xx, connection errors, timeouts
    - Persistent retry mode: for unattended sessions, retries 429/529 indefinitely
      with higher backoff (5 min max) and periodic heartbeats

    Args:
        cmd: Command list for subprocess
        timeout: Per-attempt timeout (default: PROCESS_TIMEOUT)
        max_retries: Maximum retry attempts (default: 10, ignored if persistent=True)
        fallback_model: Model to fall back to after 3x 529 (e.g., "sonnet")
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy

    Returns:
        Result dict with success, result_text, num_turns, duration, cost, errors
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    consecutive_529_errors = 0
    last_error = ""
    model_fallback_triggered = False
    persistent_attempt = 0

    # Create child abort controller if parent provided
    child_controller = (
        create_child_abort_controller(abort_controller)
        if abort_controller
        else AbortController()
    )

    for attempt in range(1, max_retries + 1):
        attempt_start = time.time()

        # Check if already aborted before spawning subprocess
        if child_controller.signal.aborted:
            duration = time.time() - start_time
            return {
                "success": False,
                "result_text": "",
                "num_turns": 0,
                "duration": duration,
                "cost": None,
                "errors": [f"Aborted before execution: {child_controller.signal.reason}"],
                # Task #2314 Phase A: every exit path must set this so
                # _resolve_files_changed_count never falls back to the
                # agent-controlled FILES_CHANGED footer.
                "files_changed_set": [],
            }

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Register abort handler to kill subprocess
            def abort_handler() -> None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass

            child_controller.signal.add_event_listener("abort", abort_handler, once=True)

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                # Try to capture any partial output before killing
                process.kill()
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=5,
                    )
                    partial_text = stdout_bytes.decode("utf-8", errors="replace").strip()
                except (asyncio.TimeoutError, OSError, ProcessLookupError):
                    partial_text = ""
                duration = time.time() - start_time
                return {
                    "success": False,
                    "result_text": partial_text,
                    "num_turns": 0,
                    "duration": duration,
                    "cost": None,
                    "errors": [f"Process timed out after {effective_timeout} seconds"],
                    "files_changed_set": [],
                }

        except FileNotFoundError:
            return {
                "success": False,
                "result_text": "",
                "num_turns": 0,
                "duration": 0,
                "cost": None,
                "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
                "files_changed_set": [],
            }

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        # Parse JSON output
        result: dict[str, Any] = {
            "success": False,
            "result_text": stdout_text,
            "num_turns": 0,
            "duration": time.time() - start_time,
            "cost": None,
            "errors": [],
            # Task #2314 Phase A: non-streaming run_agent path does not observe
            # tool calls, so the set is always empty. Setting it explicitly
            # blocks _resolve_files_changed_count from trusting the agent's
            # FILES_CHANGED footer for this code path.
            "files_changed_set": [],
        }

        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")

        if not stdout_text:
            result["errors"].append("No output from agent")
            last_error = "No output from agent"
        else:
            try:
                data = json.loads(stdout_text)
                result["result_text"] = data.get("result", stdout_text)
                result["num_turns"] = data.get("num_turns", 0)
                result["cost"] = data.get("cost_usd")

                # Check for error subtypes
                subtype = data.get("subtype", "")
                if subtype == "error_max_turns":
                    # Agent ran out of turns but may have done useful work
                    result["success"] = True
                    result["errors"].append("Agent hit max turns limit")
                elif data.get("is_error"):
                    result["success"] = False
                    error_msg = data.get('result', 'unknown')
                    result["errors"].append(f"Agent error: {error_msg}")
                    last_error = error_msg
                else:
                    result["success"] = True

            except json.JSONDecodeError:
                # Output wasn't JSON, treat raw text as result
                result["result_text"] = stdout_text
                result["success"] = process.returncode == 0
                if not result["success"]:
                    last_error = stdout_text[:200]

        # If successful, return immediately
        if result["success"]:
            return result

        # Check for 529/overloaded errors
        if is_overloaded_error(stderr_text, stdout_text):
            consecutive_529_errors += 1
            if consecutive_529_errors >= MAX_529_RETRIES and fallback_model:
                # Trigger model fallback
                if not model_fallback_triggered:
                    # Find --model flag in cmd and replace
                    for i, arg in enumerate(cmd):
                        if arg == "--model" and i + 1 < len(cmd):
                            original_model = cmd[i + 1]
                            cmd[i + 1] = fallback_model
                            model_fallback_triggered = True
                            print(f"  [Retry] Model fallback triggered: "
                                  f"{original_model} -> {fallback_model} "
                                  f"(after {consecutive_529_errors}x 529 errors)")
                            consecutive_529_errors = 0  # Reset counter for new model
                            break
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable
        if not is_retryable_error(stderr_text, stdout_text):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 indefinitely with high backoff
        is_capacity_error = is_transient_capacity_error(stderr_text, stdout_text)
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
            # In persistent mode, use separate attempt counter and higher backoff
            delay_seconds = get_retry_delay(
                persistent_attempt,
                max_delay_ms=PERSISTENT_MAX_BACKOFF_MS,
                persistent=True,
            )
            # Cap total delay at 6 hours
            delay_ms = delay_seconds * 1000
            if delay_ms > PERSISTENT_RESET_CAP_MS:
                delay_ms = PERSISTENT_RESET_CAP_MS
                delay_seconds = delay_ms / 1000.0

            print(f"  [PersistentRetry] Attempt {persistent_attempt} failed "
                  f"({time.time() - attempt_start:.1f}s). "
                  f"Retrying in {delay_seconds:.1f}s... "
                  f"(error: {last_error[:80]})")

            # Chunk long sleeps into heartbeat intervals to show we're alive
            remaining_ms = delay_ms
            while remaining_ms > 0:
                chunk_ms = min(remaining_ms, HEARTBEAT_INTERVAL_MS)
                await asyncio.sleep(chunk_ms / 1000.0)
                remaining_ms -= chunk_ms
                if remaining_ms > 0:
                    print(f"  [Heartbeat] Still retrying... "
                          f"{remaining_ms / 1000.0:.0f}s remaining")

            # Clamp attempt counter so we never exit the loop in persistent mode
            if attempt >= max_retries:
                attempt = max_retries
            continue

        # Last attempt exhausted (non-persistent mode)
        if attempt >= max_retries:
            result["errors"].append(
                f"Max retries ({max_retries}) exhausted. Last error: {last_error}"
            )
            return result

        # Calculate retry delay with exponential backoff + jitter
        delay_seconds = get_retry_delay(attempt)
        print(f"  [Retry] Attempt {attempt}/{max_retries} failed "
              f"({time.time() - attempt_start:.1f}s). "
              f"Retrying in {delay_seconds:.1f}s... "
              f"(error: {last_error[:80]})")

        await asyncio.sleep(delay_seconds)

    # Should never reach here, but fallback return
    return result


async def _run_agent_streaming_impl(
    cmd: list[str],
    role: str = "developer",
    timeout: int | None = None,
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> dict[str, Any]:
    """Internal implementation of streaming agent execution.

    This is the actual implementation that gets wrapped by run_agent_streaming
    with retry logic.
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    from equipa.role_resolver import is_role_early_term_exempt
    is_exempt = is_role_early_term_exempt(role, project_dir)

    # Task #2314 Phase A3: capture HEAD before any agent work so that the
    # post-run git-diff cross-check measures ONLY commits this cycle made.
    # Using HEAD~1..HEAD (the prior implementation) had three failure modes:
    #   (a) multi-commit cycles under-counted (only last commit's files seen)
    #   (b) non-writer roles (code-reviewer / security-reviewer) running after
    #       the developer in the same worktree inherited the developer's diff
    #   (c) deferred commits caused stale-HEAD overrides of fresh edits
    # pre_head..post_head solves all three. None means "no commits yet" — the
    # later cross-check skips the override and trusts files_changed_set.
    pre_head: str | None = None
    if project_dir:
        pre_head = await _git_rev_parse_head(project_dir)

    # Create child abort controller if parent provided
    child_controller = (
        create_child_abort_controller(abort_controller)
        if abort_controller
        else AbortController()
    )

    # Tracking state
    turn_count = 0
    turns_without_file_change = 0
    # Scale early termination with budget — larger budgets get more reading time
    # but cap HARD to prevent analysis paralysis on large codebases.
    # Max kill threshold = 1.25x base. Previous 1.5x was too generous — agents
    # burned 12+ turns reading on 58KB+ patches (FeatureBench task 3).
    effective_kill_turns = min(
        int(EARLY_TERM_KILL_TURNS * 1.25),
        max(EARLY_TERM_KILL_TURNS, int((max_turns or EARLY_TERM_KILL_TURNS) * 0.15))
    )
    # On paralysis retries, progressively tighten kill thresholds.
    # Each retry halves remaining patience: retry 1 → -2 turns, retry 2 → -3, etc.
    # Floor at 3 turns — even the most aggressive retry needs a couple turns.
    if paralysis_retry_count > 0:
        # Loosened 2026-05-02 for Opus 4.7 retest. Previous behavior:
        # halved patience per retry + ZERO free reads from turn 0. That
        # works for 4.6 but kills 4.7 instantly because 4.7 legitimately
        # needs 1-3 reads to plan complex edits. New behavior: gentler
        # reduction (cap at 25% of base, not halving), and DO NOT
        # pre-arm must_write_next_turn — let normal escalation rules
        # handle reading limits.
        reduction = min(paralysis_retry_count, max(1, effective_kill_turns // 4))
        effective_kill_turns = max(8, effective_kill_turns - reduction)
        log(f"  [EarlyTerm] Paralysis retry #{paralysis_retry_count}: "
            f"kill threshold reduced to {effective_kill_turns} turns "
            f"(loosened: free reads still allowed)", output)
    effective_final_warn_turns = max(EARLY_TERM_FINAL_WARN_TURNS, int(effective_kill_turns * 0.7))
    effective_warn_turns = max(EARLY_TERM_WARN_TURNS, int(effective_kill_turns * 0.45))
    has_any_file_change = False
    tool_history: list[str] = []
    tool_errors: list[str | None] = []
    tool_output_hashes: list[str] = []
    action_log: list[dict] = []
    stuck_phrase_count = 0
    consecutive_text_only_turns = 0
    monologue_warning_injected = False
    all_text_chunks: list[str] = []
    # Count read-only calls after the final warning (task #2604 wedge fix).
    # One read is permitted — the agent may need to see current file contents
    # before editing. Killing on the very first post-warning read created an
    # unrecoverable FINAL_WARNING → kill → retry → FINAL_WARNING loop.
    post_final_warning_reads = 0
    # Task #2242 Phase A3: accumulate framework stdout printed by bash/test
    # tool_result events so the downstream Phase-B grep can see what the
    # actual test runner reported (e.g. pytest's "= 3 skipped =" footer).
    # all_text_chunks only captures assistant TEXT blocks; tool stdout
    # never lands there, which previously made Phase B structurally blind
    # to framework-emitted skip counts.
    tool_output_text_chunks: list[str] = []
    result_data: dict | None = None
    warning_injected = False
    final_warning_injected = False
    must_write_next_turn = False  # After final warning, kill on next read-only turn
    consecutive_readonly_tools = 0  # Track read-only streaks for faster escalation
    loop_warning_injected = False
    early_term_reason: str | None = None
    loop_detected_details: str | None = None  # noqa: F841
    agent_signaled_done = False
    early_complete_reason: str | None = None

    # Compaction detection state
    files_read: set[str] = set()
    files_changed: set[str] = set()
    compaction_count: int = 0
    compaction_signals_all: list[dict[str, str]] = []
    turns_since_last_tool: int = 0
    last_soft_checkpoint_turn: int = 0

    # Check if already aborted before spawning subprocess
    if child_controller.signal.aborted:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": time.time() - start_time,
            "cost": None,
            "errors": [f"Aborted before execution: {child_controller.signal.reason}"],
            "files_changed_set": [],
        }

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,  # 4MB buffer for large file reads
        )

        # Register abort handler to kill subprocess
        def abort_handler() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        child_controller.signal.add_event_listener("abort", abort_handler, once=True)

    except FileNotFoundError:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": 0,
            "cost": None,
            "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
            "files_changed_set": [],
        }

    try:
        # Read stdout line-by-line with overall timeout
        while True:
            elapsed = time.time() - start_time
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                early_term_reason = f"Process timed out after {effective_timeout} seconds"
                break

            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=min(remaining, 600),
                )
            except asyncio.TimeoutError:
                early_term_reason = f"No output for 600s (overall timeout: {effective_timeout}s)"
                break

            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Parse stream-json message
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # --- Handle "result" message (final) ---
            if msg_type == "result":
                result_data = msg
                break

            # --- Handle "assistant" messages (agent turns) ---
            if msg_type == "assistant":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                turn_has_file_change = False
                turn_has_tool_calls = False

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "text":
                        text = block.get("text", "")
                        all_text_chunks.append(text)

                        # Check for agent-initiated early completion signal
                        ec_reason = _parse_early_complete(text)
                        if ec_reason and not agent_signaled_done:
                            agent_signaled_done = True
                            early_complete_reason = ec_reason
                            log(f"  [EarlyComplete] Agent signaled done at turn "
                                f"~{turn_count}: {ec_reason}", output)

                        # Check for stuck phrases
                        matched = _check_stuck_phrases(text)
                        if matched:
                            stuck_phrase_count += 1
                            log(f"  [EarlyTerm] Stuck signal detected at turn ~{turn_count}: "
                                f"\"{matched}\" (count: {stuck_phrase_count})", output)
                            if stuck_phrase_count >= 3:
                                early_term_reason = (
                                    f"Agent stuck: repeated stuck phrases "
                                    f"({stuck_phrase_count}x, last: \"{matched}\")"
                                )

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        turn_count += 1
                        turn_has_tool_calls = True

                        # Record action entry for action logging
                        try:
                            input_str = json.dumps(tool_input, default=str)
                        except (TypeError, ValueError):
                            input_str = str(tool_input)
                        action_log.append({
                            "turn": turn_count,
                            "tool": tool_name,
                            "input_preview": input_str[:200],
                            "input_hash": hashlib.sha256(
                                input_str.encode("utf-8", errors="replace")
                            ).hexdigest(),
                            "timestamp": time.time(),
                        })

                        # Track files read for compaction detection
                        if tool_name == "Read":
                            read_path = tool_input.get("file_path", "")
                            if read_path:
                                files_read.add(read_path)
                        elif tool_name in ("Glob", "Grep"):
                            pass  # Search tools — not file reads

                        # Track file-modifying tools
                        if tool_name in ("Edit", "Write", "NotebookEdit"):
                            turn_has_file_change = True
                            has_any_file_change = True
                            file_path = tool_input.get("file_path",
                                                       tool_input.get("notebook_path", ""))
                            if file_path:
                                files_changed.add(file_path)
                        elif tool_name == "Bash":
                            bash_cmd = tool_input.get("command", "")

                            # --- Bash security pre-execution filter ---
                            sec_result = check_bash_command(bash_cmd)
                            if not sec_result.safe:
                                log(f"  [BashSecurity] BLOCKED check={sec_result.check_id}: "
                                    f"{sec_result.message} — cmd={bash_cmd[:120]}", output)
                                early_term_reason = (
                                    f"Bash security violation (check {sec_result.check_id}): "
                                    f"{sec_result.message}"
                                )

                            if any(kw in bash_cmd for kw in [
                                "git commit", "git add", "go build", "npm run build",
                                "mkdir", "cp ", "mv ", "touch ", "tee ", "> ",
                            ]):
                                turn_has_file_change = True
                                has_any_file_change = True

                # After processing all blocks in this assistant message,
                # update the file-change counter ONCE per API turn
                if turn_has_tool_calls and not is_exempt:
                    if turn_has_file_change:
                        turns_without_file_change = 0
                        consecutive_readonly_tools = 0
                        must_write_next_turn = False  # Agent wrote — crisis averted
                        post_final_warning_reads = 0  # Reset post-warning read counter
                    else:
                        turns_without_file_change += 1
                        # On paralysis retries (retry_count > 0), the prompt
                        # tells the agent to start with Edit/Write. If the
                        # very first tool is read-only, allow exactly ONE
                        # read and arm must_write_next_turn so the NEXT call
                        # must be a write or the agent dies on the regular
                        # paralysis path. Applies to all retry counts (>= 1)
                        # — the prior "kill on retry >= 2" branch was
                        # unsatisfiable for refactor tasks (no forward context
                        # carries across cycles, so the agent has to read at
                        # least one file to know what to edit).
                        prev_must_write = must_write_next_turn
                        gate_term, must_write_next_turn = (
                            _evaluate_paralysis_retry_read_gate(
                                paralysis_retry_count,
                                turn_count,
                                tool_name,
                                has_any_file_change,
                                must_write_next_turn,
                            )
                        )
                        if must_write_next_turn and not prev_must_write:
                            log(f"  [EarlyTerm] Paralysis retry "
                                f"#{paralysis_retry_count}: first tool is "
                                f"{tool_name}. Allowing ONE read — next call "
                                f"MUST be Edit/Write or you die.", output)
                            warning_injected = True
                            final_warning_injected = True
                        if gate_term is not None:
                            early_term_reason = gate_term
                            log(f"  [EarlyTerm] {early_term_reason}", output)

                        # Track consecutive read-only tool calls for faster
                        # escalation on large codebases. Threshold loosened
                        # 2026-05-02 from 2 to 12 for Opus 4.7 retest — 4.7
                        # legitimately needs more reading turns than 4.6 to
                        # plan complex edits. If 4.7 retest fails, restore
                        # to 2 (was tuned for 4.6 + FeatureBench task 3).
                        if tool_name in ("Read", "Grep", "Glob", "Agent"):
                            consecutive_readonly_tools += 1
                        if (consecutive_readonly_tools >= 12
                                and not final_warning_injected):
                            log(f"  [EarlyTerm] FAST ESCALATION: "
                                f"{consecutive_readonly_tools} consecutive "
                                f"read-only tool calls without any file edit. "
                                f"Skipping to FINAL WARNING. "
                                f"(role={role}, turn ~{turn_count}). "
                                f"Your NEXT tool call MUST be Edit or Write "
                                f"or you will be TERMINATED.", output)
                            warning_injected = True
                            final_warning_injected = True
                            must_write_next_turn = True

                        tool_history.append(_build_tool_signature(tool_name, tool_input))

                        # Check for loop detection (repeated failing operations)
                        action, count, last_sig = _detect_tool_loop(
                            tool_history,
                            tool_errors,
                            warn_threshold=LOOP_WARNING_THRESHOLD,
                            terminate_threshold=LOOP_TERMINATE_THRESHOLD,
                            tool_output_hashes=tool_output_hashes,
                        )

                        if action == "terminate":
                            early_term_reason = (
                                f"Loop detected: agent repeated the same operation "
                                f"{count} times ({tool_name})"
                            )
                            log(f"  [LoopDetect] {early_term_reason}", output)
                        elif action == "warn" and not loop_warning_injected:
                            log(f"  [LoopDetect] WARNING: Repeated operation detected "
                                f"({count}x: {tool_name}). Try a different approach.", output)
                            loop_warning_injected = True

                        # File-change turn monitoring (non-exempt roles only)
                        if not is_exempt and turns_without_file_change > 0:
                            remaining = effective_kill_turns - turns_without_file_change

                            # Post-final-warning enforcement: if agent was told
                            # "write on your next turn or die" but used a read-only
                            # tool instead, kill immediately. This prevents agents
                            # from burning 2-3 extra turns after final warning.
                            # Kill on ANY tool that is not Edit/Write/Bash-edit
                            # after final warning. Previous list missed Bash,
                            # ToolSearch, and other non-writing tools. Inverting
                            # the check: only Edit and Write are writing tools.
                            write_tools = {"Edit", "Write", "NotebookEdit"}
                            if (must_write_next_turn
                                    and tool_name not in write_tools):
                                post_final_warning_reads += 1
                                if post_final_warning_reads == 1:
                                    # Allow ONE read-only call after the FINAL WARNING.
                                    # The agent may need to see the current file before
                                    # it can edit. Killing immediately here caused the
                                    # task #2604 wedge: FINAL_WARNING → kill → retry →
                                    # FINAL_WARNING loop (50 min, zero commits, PID 343414).
                                    log(
                                        f"  [EarlyTerm] POST-FINAL-WARNING read "
                                        f"#{post_final_warning_reads}: {tool_name} "
                                        f"after FINAL WARNING. ONE read permitted — "
                                        f"next call MUST be Edit/Write or you die.",
                                        output,
                                    )
                                else:
                                    early_term_reason = (
                                        f"Agent terminated: received FINAL WARNING "
                                        f"but made {post_final_warning_reads} "
                                        f"consecutive read-only calls (last: {tool_name}) "
                                        f"instead of Edit/Write. "
                                        f"{turns_without_file_change} turns without "
                                        f"file changes — analysis paralysis"
                                    )
                                    log(
                                        f"  [EarlyTerm] KILLED (post-warning, "
                                        f"{post_final_warning_reads}x read): "
                                        f"{early_term_reason}",
                                        output,
                                    )

                            if (turns_without_file_change >= effective_warn_turns
                                    and not warning_injected):
                                log(f"  [EarlyTerm] WARNING: {turns_without_file_change} "
                                    f"turns without file changes (role={role}, "
                                    f"turn ~{turn_count}). STOP READING AND WRITE "
                                    f"CODE NOW. Your next tool call MUST be Edit or "
                                    f"Write — not Read, not Grep, not Glob. Write a "
                                    f"stub or skeleton immediately. You have "
                                    f"{remaining} turns before termination. This is "
                                    f"not a suggestion — agents that ignore this "
                                    f"warning get killed.", output)
                                warning_injected = True

                            if (turns_without_file_change >= effective_final_warn_turns
                                    and not final_warning_injected):
                                log(f"  [EarlyTerm] FINAL WARNING — IMMINENT KILL: "
                                    f"{turns_without_file_change} turns without file "
                                    f"changes (role={role}, turn ~{turn_count}). "
                                    f"YOU WILL BE TERMINATED IN {remaining} TURNS. "
                                    f"A replacement agent is already queued. Your "
                                    f"ONLY option: call Edit or Write RIGHT NOW. "
                                    f"Write ANYTHING that creates a file change — "
                                    f"a stub, a skeleton, a partial implementation. "
                                    f"If your very next tool call is not Edit or "
                                    f"Write, you are dead.", output)
                                final_warning_injected = True
                                must_write_next_turn = True

                            # Reading-ratio kill: even if the agent made an
                            # early edit, catch agents that relapse into
                            # analysis after one trivial change. If >75% of
                            # tool calls are read-only after turn 8, kill.
                            if (turn_count >= 8
                                    and not early_term_reason
                                    and consecutive_readonly_tools >= 4
                                    and len(tool_history) > 0):
                                read_tools_total = sum(
                                    1 for sig in tool_history
                                    if any(sig.startswith(t)
                                           for t in ("Read:", "Grep:", "Glob:",
                                                     "Agent:"))
                                )
                                ratio = read_tools_total / len(tool_history)
                                if ratio >= 0.75:
                                    early_term_reason = (
                                        f"Agent terminated: {ratio:.0%} of "
                                        f"tool calls are read-only after "
                                        f"{turn_count} turns "
                                        f"({read_tools_total}/{len(tool_history)}). "
                                        f"Reading ratio exceeded 75% threshold "
                                        f"— analysis paralysis with token edits"
                                    )
                                    log(f"  [EarlyTerm] KILLED (reading ratio): "
                                        f"{early_term_reason}", output)

                            if turns_without_file_change >= effective_kill_turns:
                                early_term_reason = (
                                    f"Agent terminated: {turns_without_file_change} "
                                    f"consecutive turns without file changes "
                                    f"(threshold: {effective_kill_turns}). "
                                    f"Agent spent all turns reading/analyzing "
                                    f"instead of writing code — analysis paralysis"
                                )
                                log(f"  [EarlyTerm] KILLED: {early_term_reason}",
                                    output)

                # Budget visibility: log remaining budget at intervals
                if turn_has_tool_calls and max_turns:
                    budget_msg = _get_budget_message(turn_count, max_turns)
                    if budget_msg:
                        log(f"  [Budget] {budget_msg}", output)

                # Monologue detection: track consecutive text-only assistant turns
                if turn_has_tool_calls:
                    consecutive_text_only_turns = 0
                else:
                    consecutive_text_only_turns += 1

                    # Post-final-warning enforcement for text-only turns:
                    # If agent was told "write next turn or die" but responds
                    # with pure text (no tool calls at all), that's worse than
                    # a read-only tool call — kill immediately.
                    if must_write_next_turn and not is_exempt:
                        early_term_reason = (
                            f"Agent terminated: received FINAL WARNING but "
                            f"responded with text-only turn (no tool calls) "
                            f"instead of Edit/Write. "
                            f"{turns_without_file_change} turns without "
                            f"file changes — analysis paralysis"
                        )
                        log(f"  [EarlyTerm] KILLED (post-warning, text-only): "
                            f"{early_term_reason}", output)

                    monologue_action = _check_monologue(
                        consecutive_text_only_turns, turn_count,
                    )
                    if monologue_action == "terminate":
                        early_term_reason = (
                            f"Agent monologue: {consecutive_text_only_turns} "
                            f"consecutive text-only messages without tool use"
                        )
                        log(f"  [Monologue] {early_term_reason}", output)
                    elif (monologue_action == "warn"
                            and not monologue_warning_injected):
                        log(f"  [Monologue] WARNING: {consecutive_text_only_turns} "
                            f"consecutive text-only turns (role={role}, "
                            f"turn ~{turn_count}). Agent may be stuck reasoning "
                            f"without acting.", output)
                        monologue_warning_injected = True

                # Compaction detection: check for signals after processing
                if turn_has_tool_calls and all_text_chunks:
                    # Build recent tool calls for this turn
                    recent_tools = tool_history[-3:] if tool_history else []
                    if turn_has_tool_calls:
                        turns_since_last_tool = 0
                    else:
                        turns_since_last_tool += 1

                    latest_text = all_text_chunks[-1] if all_text_chunks else ""
                    signals = detect_compaction_signals(
                        text=latest_text,
                        turn_count=turn_count,
                        files_read=files_read,
                        recent_tool_calls=recent_tools,
                        turns_since_last_tool=turns_since_last_tool,
                    )
                    if signals:
                        compaction_count += 1
                        compaction_signals_all.extend(signals)
                        signal_types = [s["type"] for s in signals]
                        log(f"  [Compaction] Suspected compaction at turn "
                            f"~{turn_count} (#{compaction_count}): "
                            f"{', '.join(signal_types)}", output)

                        # Immediate soft checkpoint on compaction signal
                        last_text = "\n".join(all_text_chunks[-3:])
                        cp_path = save_soft_checkpoint(
                            task_id=task_id or 0,
                            turn_count=turn_count,
                            files_changed=files_changed,
                            files_read=files_read,
                            last_result_text=last_text,
                            compaction_count=compaction_count,
                            compaction_signals=compaction_signals_all,
                            role=role,
                        )
                        if cp_path:
                            log(f"  [SoftCheckpoint] Saved compaction "
                                f"checkpoint -> {cp_path.name}", output)
                            last_soft_checkpoint_turn = turn_count

                # Periodic soft checkpoint every N turns
                if (turn_has_tool_calls
                        and task_id
                        and turn_count - last_soft_checkpoint_turn
                        >= SOFT_CHECKPOINT_INTERVAL):
                    last_text = "\n".join(all_text_chunks[-3:])
                    cp_path = save_soft_checkpoint(
                        task_id=task_id,
                        turn_count=turn_count,
                        files_changed=files_changed,
                        files_read=files_read,
                        last_result_text=last_text,
                        compaction_count=compaction_count,
                        compaction_signals=compaction_signals_all,
                        role=role,
                    )
                    if cp_path:
                        log(f"  [SoftCheckpoint] Periodic checkpoint at "
                            f"turn {turn_count} -> {cp_path.name}", output)
                        last_soft_checkpoint_turn = turn_count

                # If we found a reason to terminate, break out
                if early_term_reason:
                    break

                # If agent signaled early completion, break after this
                # assistant message is fully processed
                if agent_signaled_done:
                    log(f"  [EarlyComplete] Current message processed, "
                        f"stopping stream.", output)
                    break

            # --- Handle "user" messages (tool results) ---
            elif msg_type == "user":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "tool_result":
                        is_error = block.get("is_error", False)
                        content = block.get("content", "")

                        # Task #2242 Phase A3: capture the raw stdout of tool
                        # results (bash, etc.) so Phase B can grep framework
                        # skip counts that never appear in assistant text.
                        if isinstance(content, str):
                            if content:
                                tool_output_text_chunks.append(content)
                        elif isinstance(content, list):
                            for _c in content:
                                if isinstance(_c, dict) and _c.get("type") == "text":
                                    _t = _c.get("text", "")
                                    if _t:
                                        tool_output_text_chunks.append(_t)

                        error_text = None
                        if is_error:
                            if isinstance(content, str):
                                error_text = content[:200]
                            elif isinstance(content, list):
                                texts = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        texts.append(c.get("text", ""))
                                if texts:
                                    error_text = " ".join(texts)[:200]

                        tool_errors.append(error_text)

                        # Compute output hash for loop detection
                        output_hash = _compute_output_hash(content)
                        tool_output_hashes.append(output_hash)

                        # Update the most recent action_log entry with result
                        if action_log:
                            entry = action_log[-1]
                            if isinstance(content, str):
                                result_len = len(content)
                            elif isinstance(content, list):
                                result_len = sum(
                                    len(c.get("text", ""))
                                    for c in content
                                    if isinstance(c, dict)
                                )
                            else:
                                result_len = 0
                            entry["success"] = not is_error
                            entry["output_length"] = result_len
                            entry["output_hash"] = output_hash
                            entry["duration_ms"] = int(
                                (time.time() - entry.get("timestamp", time.time())) * 1000
                            )
                            if is_error and error_text:
                                entry["error_type"] = classify_error(error_text)
                                entry["error_summary"] = error_text[:200]

                            # After any tool completes, check git for file changes
                            if project_dir:
                                if _check_git_changes(project_dir):
                                    has_any_file_change = True
                                    turns_without_file_change = 0
                                    tool_label = entry.get("tool", "unknown")
                                    log(f"  [FileDetect] Git detected file changes "
                                        f"via {tool_label}", output)

    except Exception as e:
        # Justified broad catch: the streaming monitor parses arbitrary JSON tool events
        # from a long-lived subprocess; any unexpected event shape must NOT crash the
        # orchestrator. Reason captured into early_term_reason for surfacing upstream.
        early_term_reason = f"Streaming monitor error: {e}"
        logger.exception("[Telemetry] streaming monitor caught unexpected error")

    # --- Kill process if still running ---
    if process.returncode is None:
        log(f"  [EarlyTerm] Killing agent process (reason: {early_term_reason})", output)
        process.kill()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            pass

    duration = time.time() - start_time

    result = _build_streaming_result(
        turn_count, duration, has_any_file_change,
        early_term_reason, agent_signaled_done,
        early_complete_reason, result_data, all_text_chunks)

    # Read any remaining stderr
    try:
        stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=2)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")
    except (asyncio.TimeoutError, OSError, AttributeError):
        pass

    # Bulk insert action log to agent_actions table
    if task_id and action_log:
        bulk_log_agent_actions(action_log, task_id, run_id, cycle_number, role)

    # Attach action_log to result for caller inspection
    result["action_log"] = action_log

    # Attach compaction metadata for loop/continuation logic
    result["compaction_count"] = compaction_count
    result["compaction_signals"] = compaction_signals_all
    result["files_read"] = sorted(files_read)
    result["files_changed_set"] = sorted(files_changed)

    # Task #2314 Phase A3: cross-check files_changed_set against the actual
    # git diff over the range pre_head..post_head — commits this cycle made,
    # NOT HEAD~1..HEAD (which mis-counts multi-commit cycles and leaks the
    # developer's diff into the reviewer's row). The override only fires when
    # at least one commit landed during this run; non-writer roles and
    # deferred-commit cycles see pre_head == post_head and keep the
    # files_changed_set count (Phase A made that field authoritative on every
    # exit path). The mismatch warning fires ONLY on real disagreement about
    # actually-committed work — silent on roles that wrote nothing.
    if project_dir and pre_head is not None:
        post_head = await _git_rev_parse_head(project_dir)
        if post_head and post_head != pre_head:
            git_diff_count = await _git_diff_files_changed_count(
                project_dir, pre_head, post_head,
            )
            if git_diff_count is not None:
                tool_count = len(files_changed)
                if git_diff_count != tool_count:
                    log(
                        f"  [Telemetry] files_changed cross-check mismatch: "
                        f"tool-calls={tool_count} git-diff={git_diff_count} "
                        f"(range {pre_head[:8]}..{post_head[:8]}). "
                        f"Bash-driven writes or fabrication possible.",
                        output,
                    )
                # Canonical signal: git's view of what actually committed
                # during this cycle. Only set when commits exist; otherwise
                # files_changed_set (already on result) is authoritative.
                result["files_changed_count"] = git_diff_count

    return result


async def _git_rev_parse_head(project_dir: str) -> str | None:
    """Resolve HEAD to a commit SHA, or ``None`` if the repo has no commits
    yet / project_dir is not a git repo / git errors. Never raises.

    Used as the bookends for the Phase-A3 pre_head..post_head diff range so
    the cross-check measures only commits made during this agent cycle.
    """
    try:
        from equipa.git_ops import git_run_async
        rev = await git_run_async(
            ["rev-parse", "--verify", "HEAD"], project_dir, timeout=10,
        )
        if rev.returncode != 0:
            return None
        sha = rev.stdout.strip()
        return sha or None
    except Exception:
        # Justified broad catch: best-effort telemetry probe. A git failure
        # here must not crash the orchestrator before / after the agent runs.
        return None


async def _git_diff_files_changed_count(
    project_dir: str,
    pre_head: str,
    post_head: str,
) -> int | None:
    """Return the number of unique files changed in the range
    ``pre_head..post_head``, or ``None`` if it cannot be determined.

    Canonical Phase-A3 signal for ``agent_runs.files_changed_count``. The
    range covers every commit made during the current agent cycle, so
    multi-commit cycles count correctly and non-writer roles (no commits)
    never reach this code path. Uses ``git_run_async`` so the event loop is
    not blocked. Never raises.
    """
    try:
        from equipa.git_ops import git_run_async
        diff = await git_run_async(
            ["diff", "--name-only", f"{pre_head}..{post_head}"],
            project_dir, timeout=10,
        )
        if diff.returncode != 0:
            return None
        files = {line for line in diff.stdout.splitlines() if line.strip()}
        return len(files)
    except Exception:
        # Justified broad catch: this is a best-effort telemetry probe; any
        # git error must NOT crash the orchestrator post-cycle.
        return None


async def run_agent_with_retries(
    cmd: list[str],
    task: dict[str, Any],
    max_retries: int,
) -> tuple[dict[str, Any], int]:
    """Run agent with retry logic on failure.

    Returns (result, attempt_number) tuple.
    """
    started_at = _run_started_at_utc()
    result: dict[str, Any] = {}
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"\n--- Retry {attempt}/{max_retries} ---")

        result = await run_agent(cmd)
        # Stamp the run start so downstream date-checks (e.g. the single-agent
        # guard's TASKS_CREATED validation) have a real value, not None.
        result.setdefault("started_at", started_at)

        # Check if output is valid
        is_valid, reason = validate_output(result)

        if is_valid:
            return result, attempt

        print(f"  Attempt {attempt} failed: {reason}")

        # Check if the agent updated the task to blocked — that's intentional
        verified, _ = verify_task_updated(task["id"])
        if verified:
            return result, attempt

        # Don't retry on timeout — the task is probably too complex
        if any("timed out" in e for e in result.get("errors", [])):
            print("  Not retrying: process timed out")
            return result, attempt

    print(f"\n  All {max_retries} attempts failed.")
    return result, max_retries


async def run_agent_streaming_with_retry(
    cmd: list[str],
    role: str = "developer",
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    max_retries: int = 10,
    fallback_model: str | None = "sonnet",
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> AgentResult:
    """Wrap run_agent_streaming with retry logic + exponential backoff + model fallback.

    Same retry architecture as run_agent():
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - After 3 consecutive 529/overloaded errors, fall back to cheaper model
    - Retryable errors: 429, 5xx, connection, timeout, ECONNRESET, EPIPE
    - Non-retryable errors fail immediately
    - Persistent retry mode: for unattended sessions, retries 429/529 indefinitely
      with higher backoff (5 min max) and periodic heartbeats

    Args:
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy
    """
    consecutive_529_errors = 0
    model_fallback_triggered = False
    last_error = ""
    persistent_attempt = 0

    for attempt in range(1, max_retries + 1):
        attempt_start = time.time()

        # Execute streaming agent
        result = await _run_agent_streaming_impl(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, run_id=None, cycle_number=cycle_number,
            project_dir=project_dir, abort_controller=abort_controller,
            paralysis_retry_count=paralysis_retry_count,
        )

        # If successful, return immediately
        if result.get("success"):
            return result

        # Extract error info
        stderr_text = " ".join(result.get("errors", []))
        stdout_text = result.get("result_text", "")
        last_error = stderr_text[:200] if stderr_text else stdout_text[:200]

        # Non-retryable: analysis paralysis kills must fail fast, not retry.
        # Retrying after a paralysis kill restarts the exact same pattern
        # (agent reads → FINAL WARNING → reads again → kill → retry → ...)
        # and burns API cost for 10 attempts before giving up (task #2604 wedge).
        if result.get("early_terminated"):
            term_reason = result.get("early_term_reason", "")
            if (
                "analysis paralysis" in term_reason
                or "without file changes" in term_reason
                or "read-only" in term_reason
                or "reading ratio" in term_reason
            ):
                return result

        # Check for 529/overloaded errors
        if is_overloaded_error(stderr_text, stdout_text):
            consecutive_529_errors += 1
            if consecutive_529_errors >= MAX_529_RETRIES and fallback_model:
                # Trigger model fallback
                if not model_fallback_triggered:
                    # Find --model flag in cmd and replace
                    for i, arg in enumerate(cmd):
                        if arg == "--model" and i + 1 < len(cmd):
                            original_model = cmd[i + 1]
                            cmd[i + 1] = fallback_model
                            model_fallback_triggered = True
                            print(f"  [Retry] Model fallback triggered: "
                                  f"{original_model} -> {fallback_model} "
                                  f"(after {consecutive_529_errors}x 529 errors)")
                            consecutive_529_errors = 0  # Reset counter for new model
                            break
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable
        if not is_retryable_error(stderr_text, stdout_text):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 indefinitely with high backoff
        is_capacity_error = is_transient_capacity_error(stderr_text, stdout_text)
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
            # In persistent mode, use separate attempt counter and higher backoff
            delay_seconds = get_retry_delay(
                persistent_attempt,
                max_delay_ms=PERSISTENT_MAX_BACKOFF_MS,
                persistent=True,
            )
            # Cap total delay at 6 hours
            delay_ms = delay_seconds * 1000
            if delay_ms > PERSISTENT_RESET_CAP_MS:
                delay_ms = PERSISTENT_RESET_CAP_MS
                delay_seconds = delay_ms / 1000.0

            print(f"  [PersistentRetry] Streaming attempt {persistent_attempt} failed "
                  f"({time.time() - attempt_start:.1f}s). "
                  f"Retrying in {delay_seconds:.1f}s... "
                  f"(error: {last_error[:80]})")

            # Chunk long sleeps into heartbeat intervals to show we're alive
            remaining_ms = delay_ms
            while remaining_ms > 0:
                chunk_ms = min(remaining_ms, HEARTBEAT_INTERVAL_MS)
                await asyncio.sleep(chunk_ms / 1000.0)
                remaining_ms -= chunk_ms
                if remaining_ms > 0:
                    print(f"  [Heartbeat] Still retrying... "
                          f"{remaining_ms / 1000.0:.0f}s remaining")

            # Clamp attempt counter so we never exit the loop in persistent mode
            if attempt >= max_retries:
                attempt = max_retries
            continue

        # Last attempt exhausted (non-persistent mode)
        if attempt >= max_retries:
            result["errors"].append(
                f"Max retries ({max_retries}) exhausted. Last error: {last_error}"
            )
            return result

        # Calculate retry delay with exponential backoff + jitter
        delay_seconds = get_retry_delay(attempt)
        print(f"  [Retry] Streaming attempt {attempt}/{max_retries} failed "
              f"({time.time() - attempt_start:.1f}s). "
              f"Retrying in {delay_seconds:.1f}s... "
              f"(error: {last_error[:80]})")

        await asyncio.sleep(delay_seconds)

    # Should never reach here, but fallback return
    return result


async def run_agent_streaming(
    cmd: list[str],
    role: str = "developer",
    timeout: int | None = None,
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> AgentResult:
    """Spawn claude -p with stream-json output for real-time stuck detection.

    Monitors agent output turn-by-turn and terminates early if stuck signals
    are detected. Only applies file-change monitoring to non-exempt roles
    (developer, tester, debugger, etc.).

    When task_id is provided, per-tool actions are logged to the agent_actions
    table for observability and ForgeSmith analysis.

    This function automatically includes retry logic with exponential backoff
    and model fallback (after 3x 529 errors). Use run_agent_streaming_with_retry
    directly if you need to customize retry parameters.

    Returns the same dict format as run_agent().
    """
    started_at = _run_started_at_utc()
    result = await run_agent_streaming_with_retry(
        cmd, role=role, output=output, max_turns=max_turns,
        task_id=task_id, cycle_number=cycle_number, project_dir=project_dir,
        paralysis_retry_count=paralysis_retry_count,
    )
    # Stamp the run start so downstream date-checks (e.g. the single-agent
    # guard's TASKS_CREATED validation) have a real value, not None.
    if isinstance(result, dict):
        result.setdefault("started_at", started_at)
    return result


async def dispatch_agent(
    cmd: list[str],
    role: str,
    output: Any,
    max_turns: int,
    task_id: int,
    cycle: int,
    system_prompt: str | PromptResult | None = None,
    project_dir: str | None = None,
    args: Any = None,
    paralysis_retry_count: int = 0,
) -> AgentResult:
    """Dispatch an agent using the configured provider (Claude or Ollama).

    For Claude: delegates to run_agent_streaming() or run_agent().
    For Ollama: delegates to run_ollama_agent().

    system_prompt accepts str or PromptResult (coerced to str via __str__).

    Returns the same result dict format regardless of provider.
    """
    # Coerce PromptResult to str for downstream consumers
    if system_prompt is not None:
        system_prompt = str(system_prompt)

    # Security: verify skill file integrity before building any agent prompt
    if not verify_skill_integrity():
        return {
            "success": False,
            "result": "blocked",
            "result_text": "CRITICAL: Skill integrity verification failed — agent dispatch refused. "
                          "Run --regenerate-manifest if changes are intentional.",
            "num_turns": 0,
            "cost": 0,
            "duration": 0,
            "errors": ["Skill integrity verification failed"],
        }

    # Late imports to avoid circular dependency
    from equipa.cli import get_ollama_base_url, get_ollama_model, get_provider

    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    provider_override = getattr(args, "provider", None) if args else None

    # Determine provider: CLI override > config > default (claude)
    if provider_override:
        provider = provider_override
    else:
        provider = get_provider(role, dispatch_config)

    if provider == "ollama" and system_prompt and project_dir:
        from ollama_agent import run_ollama_agent
        model = get_ollama_model(role, dispatch_config)
        base_url = get_ollama_base_url(dispatch_config)
        return run_ollama_agent(
            system_prompt=system_prompt,
            project_dir=project_dir,
            role=role,
            model=model,
            base_url=base_url,
            max_turns=max_turns,
        )

    # RLM REPL decomposition for large-repo reviews
    if system_prompt and project_dir and role in ("code-reviewer", "integration-tester"):
        from equipa.rlm_decompose import (
            estimate_context_tokens,
            load_repo_files,
            run_decompose_session,
            should_decompose,
        )
        rlm_enabled = is_feature_enabled(dispatch_config, "rlm_decompose")
        if rlm_enabled:
            repo_files = load_repo_files(project_dir)
            ctx_tokens = estimate_context_tokens(system_prompt, repo_files)
            if should_decompose(role, ctx_tokens, rlm_enabled):
                from equipa.output import log
                log(
                    f"RLM Decompose active: {len(repo_files)} files, "
                    f"~{ctx_tokens:,} tokens, role={role}"
                )
                decompose_result = run_decompose_session(
                    system_prompt=system_prompt,
                    project_dir=project_dir,
                    role=role,
                    repo_files=repo_files,
                    mcp_config=mcp_config or "",
                )
                return {
                    "success": decompose_result.success,
                    "result_text": decompose_result.output,
                    "num_turns": decompose_result.sub_queries_run,
                    "duration": 0,
                    "cost": 0,
                    "errors": decompose_result.errors,
                    "rlm_decompose": True,
                    "files_examined": decompose_result.files_examined,
                }

    # Default: Claude via run_agent_streaming (with retry wrapper)
    from equipa.role_resolver import is_role_early_term_exempt
    use_streaming = not is_role_early_term_exempt(role, project_dir)
    if use_streaming:
        # Wrap streaming with retry logic
        return await run_agent_streaming_with_retry(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, cycle_number=cycle, project_dir=project_dir,
            paralysis_retry_count=paralysis_retry_count)
    else:
        return await run_agent(cmd)
