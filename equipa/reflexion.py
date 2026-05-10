"""EQUIPA reflexion — post-task self-reflection for learning (Reflexion pattern).

Layer 5: Imports from equipa.db, equipa.lessons, equipa.parsing, equipa.output.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import shutil
from typing import Any

from equipa.db import db_conn
from equipa.lessons import record_agent_episode
from equipa.output import log
from equipa.parsing import parse_reflection


# Reflexion prompt — asks for specific, actionable self-reflection
REFLEXION_PROMPT = (
    "Reflect on this task. What approach did you take? What worked? "
    "What did not? What would you do differently next time? "
    "Be specific and concise (3-5 sentences). Reference exact files, "
    "error messages, tools, or strategies."
)

# Initial Q-value for new episodes (neutral prior)
INITIAL_Q_VALUE = 0.5


async def run_reflexion_agent(
    task: dict[str, Any] | int,
    result: dict[str, Any] | str,
    outcome: str,
    role: str = "developer",
    output: Any = None,
) -> None:
    """Spawn a lightweight agent to generate reflection when not in output.

    This is a fallback — if the agent included REFLECTION: in its
    structured output, we already have it. This function is only called
    when parse_reflection() returned None.

    Uses minimal turns (max 2) and sonnet model to keep cost low.
    The reflection is stored back into the most recent agent_episode.
    """
    # Late import to avoid circular dependency during transition
    from equipa.agent_runner import run_agent

    try:
        task_id = task.get("id") if isinstance(task, dict) else task
        task_title = task.get("title", "unknown") if isinstance(task, dict) else "unknown"
        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0

        # Build a concise context for the reflection agent
        # Use last 1500 chars of output to stay within prompt limits
        output_tail = result_text[-1500:] if len(result_text) > 1500 else result_text

        reflection_prompt = (
            f"You are reflecting on a completed task.\n\n"
            f"Task: #{task_id} - {task_title}\n"
            f"Role: {role}\n"
            f"Outcome: {outcome}\n"
            f"Turns used: {num_turns}\n\n"
            f"Agent output (tail):\n{output_tail}\n\n"
            f"{REFLEXION_PROMPT}\n\n"
            f"Respond with ONLY your reflection text (3-5 sentences). "
            f"No preamble, no formatting, no markdown."
        )

        claude_bin = shutil.which("claude") or "claude"
        cmd = [
            claude_bin,
            "-p", reflection_prompt,
            "--output-format", "json",
            "--model", "sonnet",
            "--max-turns", "2",
            "--no-session-persistence",
        ]

        log(f"  [Reflexion] Spawning reflection agent for task #{task_id}...", output)
        ref_result = await run_agent(cmd, timeout=60)

        if not ref_result.get("success"):
            log(f"  [Reflexion] Reflection agent failed: {ref_result.get('errors', [])}", output)
            return

        reflection_text = ref_result.get("result_text", "").strip()
        if not reflection_text or len(reflection_text) < 20:
            log(f"  [Reflexion] Reflection too short, discarding.", output)
            return

        # Strip any JSON wrapper if present
        try:
            parsed = json.loads(reflection_text)
            if isinstance(parsed, dict) and "result" in parsed:
                reflection_text = parsed["result"].strip()
        except (json.JSONDecodeError, KeyError):
            pass  # not JSON, use raw text

        # Update the most recent episode for this task (subquery for portability)
        with db_conn(write=True) as conn:
            conn.execute(
                """UPDATE agent_episodes SET reflection = ?
                   WHERE id = (
                       SELECT id FROM agent_episodes
                       WHERE task_id = ? AND reflection IS NULL
                       ORDER BY id DESC LIMIT 1
                   )""",
                (reflection_text, task_id),
            )

        preview = reflection_text[:120] + "..." if len(reflection_text) > 120 else reflection_text
        log(f"  [Reflexion] Captured reflection: {preview}", output)

    except Exception as e:
        log(f"  [Reflexion] WARNING: Standalone reflection failed: {e}", output)


async def maybe_run_reflexion(
    task: dict[str, Any] | int,
    result: dict[str, Any] | str,
    outcome: str,
    role: str = "developer",
    output: Any = None,
) -> None:
    """Record episode and optionally spawn reflection agent.

    This is the main entry point for the Reflexion pattern. Call this
    after record_agent_run() at every task completion point.

    Flow:
    1. Record the episode (extracts reflection from output if present)
    2. If no reflection was found in output, spawn lightweight agent
    """
    record_agent_episode(task, result, outcome, role=role, output=output)

    # Check if reflection was captured from the structured output
    result_text = result.get("result_text", "") if isinstance(result, dict) else ""
    if not parse_reflection(result_text):
        await run_reflexion_agent(task, result, outcome, role=role, output=output)
