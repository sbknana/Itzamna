"""EQUIPA manager — Manager loop: Plan -> Execute -> Evaluate -> Repeat.

Layer 7: Imports from equipa.agent_runner, equipa.constants, equipa.db, equipa.loops,
         equipa.output, equipa.prompts, equipa.roles, equipa.tasks.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from typing import Any

from equipa.agent_runner import build_cli_command, run_agent
from equipa.constants import (
    MANAGER_COST_LIMIT,
    MAX_FOLLOWUP_TASKS,
    MAX_TASKS_PER_PLAN,
)
from equipa.db import update_task_status
from equipa.loops import run_dev_test_loop
from equipa.output import log
from equipa.prompts import build_evaluator_prompt, build_planner_prompt
from equipa.roles import get_role_turns
from equipa.tasks import _get_task_status, fetch_tasks_by_ids


def parse_planner_output(result_text: str) -> list[int]:
    """Extract TASKS_CREATED list from Planner agent output.

    Returns a list of integer task IDs, or empty list on failure.
    """
    if not result_text:
        return []

    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if not value or value.lower() == "none":
                return []
            ids: list[int] = []
            for part in value.split(","):
                part = part.strip()
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
            return ids

    return []


def parse_evaluator_output(result_text: str) -> dict[str, Any]:
    """Extract GOAL_STATUS, TASKS_CREATED, EVALUATION, BLOCKERS from Evaluator output.

    Returns a dict with parsed fields.
    """
    parsed: dict[str, Any] = {
        "goal_status": "blocked",
        "tasks_created": [],
        "evaluation": "",
        "blockers": "none",
    }

    if not result_text:
        return parsed

    for line in result_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("GOAL_STATUS:"):
            status = stripped.split(":", 1)[1].strip().lower()
            if status in ("complete", "needs_more", "blocked"):
                parsed["goal_status"] = status

        elif stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value.lower() != "none":
                for part in value.split(","):
                    part = part.strip()
                    try:
                        parsed["tasks_created"].append(int(part))
                    except ValueError:
                        continue

        elif stripped.startswith("EVALUATION:"):
            parsed["evaluation"] = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("BLOCKERS:"):
            parsed["blockers"] = stripped.split(":", 1)[1].strip()

    return parsed


async def run_planner_agent(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], list[int]]:
    """Spawn the Planner agent to break a goal into tasks.

    Returns (result, task_ids) tuple.
    """
    log("\n  [Planner] Building prompt...", output)
    system_prompt = build_planner_prompt(goal, project_id, project_dir, project_context)

    with build_cli_command(
        system_prompt,
        project_dir,
        get_role_turns("planner", args),
        args.model,
        role="planner",
        prompt_message=f"Break this goal into tasks. Project dir: {project_dir}",
    ) as cmd:
        log(f"  [Planner] Spawning agent (prompt: {len(system_prompt)} chars)...", output)
        result = await run_agent(cmd)

    if not result["success"]:
        log(f"  [Planner] Agent failed: {result.get('errors', [])}", output)
        return result, []

    task_ids = parse_planner_output(result.get("result_text", ""))

    if len(task_ids) > MAX_TASKS_PER_PLAN:
        log(f"  [Planner] Created {len(task_ids)} tasks (max {MAX_TASKS_PER_PLAN}). "
            f"Using first {MAX_TASKS_PER_PLAN}.", output)
        task_ids = task_ids[:MAX_TASKS_PER_PLAN]

    if task_ids:
        log(f"  [Planner] Created {len(task_ids)} tasks: {task_ids}", output)
    else:
        log(f"  [Planner] No task IDs found in output.", output)

    return result, task_ids


async def run_evaluator_agent(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    completed_tasks: list[dict],
    blocked_tasks: list[dict],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Spawn the Evaluator agent to assess goal completion.

    Returns (result, parsed_eval) tuple.
    """
    log("\n  [Evaluator] Building prompt...", output)
    system_prompt = build_evaluator_prompt(
        goal, project_id, project_dir, project_context,
        completed_tasks, blocked_tasks,
    )

    with build_cli_command(
        system_prompt,
        project_dir,
        get_role_turns("evaluator", args),
        args.model,
        role="evaluator",
        prompt_message=(
            f"Evaluate whether this goal is complete. Project dir: {project_dir}"
        ),
    ) as cmd:
        log(f"  [Evaluator] Spawning agent (prompt: {len(system_prompt)} chars)...", output)
        result = await run_agent(cmd)

    if not result["success"]:
        log(f"  [Evaluator] Agent failed: {result.get('errors', [])}", output)
        return result, {"goal_status": "blocked", "tasks_created": [],
                        "evaluation": "Evaluator agent failed", "blockers": "Agent error"}

    parsed = parse_evaluator_output(result.get("result_text", ""))

    if len(parsed["tasks_created"]) > MAX_FOLLOWUP_TASKS:
        log(f"  [Evaluator] Created {len(parsed['tasks_created'])} follow-up tasks "
            f"(max {MAX_FOLLOWUP_TASKS}). Using first {MAX_FOLLOWUP_TASKS}.", output)
        parsed["tasks_created"] = parsed["tasks_created"][:MAX_FOLLOWUP_TASKS]

    log(f"  [Evaluator] Goal status: {parsed['goal_status']}", output)
    log(f"  [Evaluator] Evaluation: {parsed['evaluation'][:200]}", output)
    if parsed["tasks_created"]:
        log(f"  [Evaluator] Follow-up tasks: {parsed['tasks_created']}", output)

    return result, parsed


async def run_manager_loop(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[str, int, list[dict], list[dict], float, float]:
    """Run the full Manager loop: Plan -> Execute -> Evaluate -> Repeat.

    Returns (outcome, total_rounds, all_completed, all_blocked, total_cost, total_duration).
    """
    max_rounds = args.max_rounds
    cost_limit = getattr(args, "manager_cost_limit", None)
    if cost_limit is None:
        cost_limit = MANAGER_COST_LIMIT
    all_completed: list[dict] = []
    all_blocked: list[dict] = []
    total_cost = 0.0
    total_duration = 0.0

    for round_num in range(1, max_rounds + 1):
        log(f"\n{'#' * 60}", output)
        log(f"  MANAGER ROUND {round_num}/{max_rounds}", output)
        log(f"{'#' * 60}", output)

        # --- Phase 1: Plan ---
        log(f"\n--- Phase 1: Planning ---", output)
        planner_result, task_ids = await run_planner_agent(
            goal, project_id, project_dir, project_context, args, output=output,
        )
        total_duration += planner_result.get("duration", 0)
        if planner_result.get("cost"):
            total_cost += planner_result["cost"]

        if not task_ids:
            log(f"\n  [Manager] Planner failed to create tasks. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # Fetch the created tasks
        tasks = fetch_tasks_by_ids(task_ids)
        if not tasks:
            log(f"\n  [Manager] Could not fetch tasks {task_ids} from TheForge. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # --- Phase 2: Execute each task via Dev+Tester loop ---
        log(f"\n--- Phase 2: Executing {len(tasks)} tasks ---", output)
        round_completed: list[dict] = []
        round_blocked: list[dict] = []

        for i, task in enumerate(tasks, 1):
            log(f"\n{'=' * 50}", output)
            log(f"  TASK {i}/{len(tasks)}: #{task['id']} - {task['title']}", output)
            log(f"{'=' * 50}", output)

            current_status = _get_task_status(task["id"])
            if current_status == "done":
                log(f"  Task already marked done. Skipping.", output)
                round_completed.append(task)
                continue

            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args, output=output,
            )
            total_duration += result.get("duration", 0)
            if result.get("cost"):
                total_cost += result["cost"]

            update_task_status(task["id"], outcome, output=output)

            if outcome in ("tests_passed", "no_tests"):
                round_completed.append(task)
                log(f"  Task #{task['id']}: COMPLETED ({outcome})", output)
            else:
                round_blocked.append(task)
                log(f"  Task #{task['id']}: BLOCKED ({outcome})", output)

        all_completed.extend(round_completed)
        all_blocked.extend(round_blocked)

        # --- Phase 3: Evaluate ---
        log(f"\n--- Phase 3: Evaluating ---", output)
        log(f"  Completed: {len(round_completed)}, Blocked: {len(round_blocked)}", output)

        eval_result, eval_parsed = await run_evaluator_agent(
            goal, project_id, project_dir, project_context,
            all_completed, all_blocked, args, output=output,
        )
        total_duration += eval_result.get("duration", 0)
        if eval_result.get("cost"):
            total_cost += eval_result["cost"]

        if eval_parsed["goal_status"] == "complete":
            log(f"\n  [Manager] Goal COMPLETE!", output)
            return "goal_complete", round_num, all_completed, all_blocked, total_cost, total_duration

        elif eval_parsed["goal_status"] == "blocked":
            log(f"\n  [Manager] Goal BLOCKED: {eval_parsed['blockers']}", output)
            return "goal_blocked", round_num, all_completed, all_blocked, total_cost, total_duration

        if total_cost >= cost_limit:
            log(
                f"\n  [Manager] Aggregate cost ${total_cost:.2f} reached limit "
                f"${cost_limit:.2f}. Aborting.",
                output,
            )
            return (
                "cost_limit_exceeded", round_num, all_completed, all_blocked,
                total_cost, total_duration,
            )

        if eval_parsed["goal_status"] == "needs_more":
            if round_num >= max_rounds:
                log(f"\n  [Manager] Needs more work but max rounds reached.", output)
                break
            log(f"\n  [Manager] Needs more work. Continuing to round {round_num + 1}...", output)
            if eval_parsed["tasks_created"]:
                continue

    return "rounds_exhausted", max_rounds, all_completed, all_blocked, total_cost, total_duration
