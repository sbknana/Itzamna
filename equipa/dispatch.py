"""EQUIPA dispatch module — auto-run scanning, scoring, filtering, and execution.

Extracts dispatch/auto-run logic from forge_orchestrator.py (Phase 5 split).
Includes: scan_pending_work, score_project, apply_dispatch_filters,
run_project_tasks, run_project_dispatch, run_auto_dispatch,
run_parallel_tasks, run_single_goal, run_parallel_goals,
parse_task_ids, load_goals_file, validate_goals.

Feature-flag and dispatch-config primitives now live in equipa.config
(layer 2). They are re-exported here for backward compatibility with
existing `from equipa.dispatch import is_feature_enabled` callers.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from equipa.config import (
    DEFAULT_DISPATCH_CONFIG,
    DEFAULT_FEATURE_FLAGS,
    is_feature_enabled,
    load_dispatch_config,
)
from equipa.constants import (
    ATTEMPT_REFLECTIONS_MAX_CHARS,
    ATTEMPT_SECTION_TRIM_CHARS,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    MAX_MANAGER_ROUNDS,
    MAX_TASK_RANGE,
    PRIORITY_ORDER,
    PROJECT_DIRS,
)
from equipa.db import (
    db_conn,
    get_db_connection,
    record_agent_run,
    update_task_status,
)
from equipa.hooks import fire_async as fire_hook
from equipa.git_ops import _is_git_repo, git_run_async
from equipa.worktree_manager import (
    WorktreeError,
    create as create_worktree,
    destroy as destroy_worktree,
    mark_conflict as mark_worktree_conflict,
    mark_failed as mark_worktree_failed,
    merge_back as merge_worktree_back,
)
from equipa.lessons import update_injected_episode_q_values_for_task
from equipa.loops import (
    run_dev_test_loop,
    run_quality_scoring,
)
from equipa.manager import run_manager_loop
from equipa.parsing import _extract_section
from equipa.output import (
    log,
    print_dispatch_plan,
    print_dispatch_summary,
    print_manager_summary,
    print_parallel_summary,
)
from equipa.prompts import build_planner_prompt
from equipa.reflexion import maybe_run_reflexion
from equipa.roles import get_role_model, get_role_turns
from equipa.tasks import (
    fetch_project_context,
    fetch_project_info,
    fetch_task,
    fetch_tasks_by_ids,
    resolve_project_dir,
)


__all_reexports__ = (
    "DEFAULT_FEATURE_FLAGS",
    "DEFAULT_DISPATCH_CONFIG",
    "is_feature_enabled",
    "load_dispatch_config",
)


# --- Cross-Attempt Memory Helpers ---

_ATTEMPT_MARKER = "\n\n--- PREVIOUS ATTEMPTS ---\n"


def _build_dispatch_attempt_reflection(
    attempt: int,
    outcome: str,
    cycles: int,
    result: dict,
) -> str:
    """Build a concise reflection from a failed dev-test loop attempt.

    Extracts key failure signals from the loop result and outcome to help
    the next attempt avoid repeating the same mistakes.

    Args:
        attempt: 1-based attempt number
        outcome: Outcome string from run_dev_test_loop (e.g. "cycles_exhausted")
        cycles: Number of dev-test cycles completed
        result: Result dict from the dev-test loop

    Returns:
        A concise reflection string (<300 chars).
    """
    duration = result.get("duration", 0)
    cost = result.get("cost", 0)

    # Determine failure category
    if outcome == "cycles_exhausted":
        reason = f"exhausted {cycles} dev-test cycles without passing tests"
    elif outcome == "cost_limit":
        reason = f"hit cost limit (${cost:.2f}) after {cycles} cycle(s)"
    elif outcome == "early_completed_blocked":
        reason = "agent reported blocked"
    elif outcome == "loop_detected":
        reason = "loop detected — agent kept trying the same approach"
    else:
        reason = outcome

    # Extract structured fields from agent output if available
    raw_output = result.get("raw_output", "")
    files_info = ""
    blockers_info = ""
    reflection_info = ""

    if raw_output:
        files_text = _extract_section(raw_output, "FILES_CHANGED")
        if files_text and "none" not in files_text.lower():
            # Strip the marker prefix
            files_text = files_text.replace("FILES_CHANGED:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if files_text:
                files_info = f"\n  Files touched: {files_text}"

        blockers_text = _extract_section(raw_output, "BLOCKERS")
        if blockers_text and "none" not in blockers_text.lower():
            blockers_text = blockers_text.replace("BLOCKERS:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if blockers_text:
                blockers_info = f"\n  Blockers: {blockers_text}"

        reflection_text = _extract_section(raw_output, "REFLECTION", max_lines=3)
        if reflection_text:
            reflection_text = reflection_text.replace("REFLECTION:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if reflection_text:
                reflection_info = f"\n  Agent reflection: {reflection_text}"

    parts = [
        f"ATTEMPT {attempt} FAILED ({reason}, {cycles} cycles, {duration:.0f}s):",
    ]
    if files_info:
        parts.append(files_info)
    if blockers_info:
        parts.append(blockers_info)
    if reflection_info:
        parts.append(reflection_info)
    parts.append("  DO NOT repeat this approach. Try a different strategy.")

    return "\n".join(parts)


def _inject_attempt_reflections(
    conn: object,
    task_id: int,
    reflections: list[str],
) -> None:
    """Inject accumulated attempt reflections into a task's description.

    Appends a PREVIOUS ATTEMPTS block to the task description so the next
    agent attempt knows what was already tried and what failed.

    Args:
        conn: SQLite connection (caller manages commit)
        task_id: Task ID to update
        reflections: List of reflection strings from prior attempts
    """
    cur = conn.execute(  # type: ignore[union-attr]
        "SELECT description FROM tasks WHERE id = ?", (task_id,)
    )
    row = cur.fetchone()
    if not row:
        return

    desc = row[0] or ""

    # Strip any existing reflection block to avoid unbounded growth
    if _ATTEMPT_MARKER in desc:
        desc = desc[: desc.index(_ATTEMPT_MARKER)]

    # Build and append the new block
    reflections_block = "\n\n".join(reflections)

    # Enforce token budget (~500 tokens ≈ ~2000 chars)
    if len(reflections_block) > ATTEMPT_REFLECTIONS_MAX_CHARS:
        reflections_block = (
            reflections_block[:ATTEMPT_REFLECTIONS_MAX_CHARS]
            + "\n[...earlier attempts trimmed...]"
        )

    desc += _ATTEMPT_MARKER + reflections_block

    conn.execute(  # type: ignore[union-attr]
        "UPDATE tasks SET description = ? WHERE id = ?", (desc, task_id)
    )


async def cleanup_failed_attempt(
    task_id: int,
    project_dir: str,
    reflections: list[str],
    output: list[str] | None = None,
) -> None:
    """Reset a failed task for a fresh autoresearch attempt.

    Performs the 5-step cleanup shared by the parallel/auto dispatch path
    and the single-task ``--task`` CLI path:

    1. Verify ``project_dir`` is a git repo.
    2. Detect default branch (``main`` then fall back to ``master``).
    3. Checkout the default branch.
    4. Force-delete the ``forge-task-<id>`` branch.
    5. Reset the task's status to ``todo`` and inject any accumulated
       cross-attempt reflections so the next attempt remembers what was
       already tried.

    Args:
        task_id: Task ID being retried.
        project_dir: Working directory containing the project's git repo.
        reflections: Reflection strings from prior failed attempts. Empty
            list is allowed (skips reflection injection).
        output: Optional buffer for ``log()`` calls; if ``None``, prints.
    """
    branch_name = f"forge-task-{task_id}"

    if _is_git_repo(project_dir):
        try:
            cp = await git_run_async(
                ["rev-parse", "--verify", "main"], project_dir, timeout=10,
            )
            default_branch = "main" if cp.returncode == 0 else "master"
            await git_run_async(
                ["checkout", default_branch], project_dir, timeout=30,
            )
            await git_run_async(
                ["branch", "-D", branch_name], project_dir, timeout=10,
            )
            msg = f"  [Autoresearch] Cleaned up branch {branch_name}"
            if output is not None:
                log(msg, output)
            else:
                print(msg)
        except (subprocess.SubprocessError, OSError) as e:
            warn = f"  [Autoresearch] Git cleanup warning: {e}"
            if output is not None:
                log(warn, output)
            else:
                print(warn)

    conn = get_db_connection(write=True)
    try:
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        if reflections:
            _inject_attempt_reflections(conn, task_id, reflections)
        conn.commit()
    finally:
        conn.close()

    reset_msg = (
        f"  [Autoresearch] Reset task #{task_id} to todo with "
        f"{len(reflections)} attempt reflection(s)"
    )
    if output is not None:
        log(reset_msg, output)
    else:
        print(reset_msg)


# --- DB Scanning & Scoring ---



async def run_dev_test_loop_with_autoresearch(
    task: dict,
    project_dir: str,
    project_context: dict,
    args,
    config: dict,
    output: list[str] | None = None,
):
    """Run run_dev_test_loop with autoresearch retry on failure.

    Bug 2282: extracted from the inline pattern that lived only in
    run_dispatch (single-task path). run_parallel_tasks did not have this
    wrapper, so any failure in --tasks N,M,O dispatch was final. This
    helper is the canonical retry entry point - both call sites use it.

    Returns:
        (result, cycles, outcome, loop_total_cost, loop_total_duration, task)

    The caller adds loop_total_cost / loop_total_duration to its own
    running totals (caller may track preflight + loop costs separately).
    The task dict is returned because autoresearch may re-fetch it
    between attempts to pick up reflection-injected context.
    """
    task_id = task["id"]
    autoresearch_on = is_feature_enabled(config, "autoresearch")
    max_retries = config.get("autoresearch_max_retries", 3) if autoresearch_on else 0
    retry_count = 0
    attempt_reflections: list[str] = []
    loop_total_cost = 0.0
    loop_total_duration = 0.0

    while True:
        result, cycles, outcome = await run_dev_test_loop(
            task, project_dir, project_context, args, output=output,
        )
        loop_total_duration += result.get("duration", 0)
        if result.get("cost"):
            loop_total_cost += result["cost"]

        # Success - break out of retry loop
        if outcome in ("tests_passed", "no_tests", "early_completed_no_changes"):
            break

        # Capture reflection on failed attempt for cross-attempt memory
        attempt_reflection = _build_dispatch_attempt_reflection(
            retry_count + 1, outcome, cycles, result,
        )
        attempt_reflections.append(attempt_reflection)

        # Not retriable or retries exhausted
        if not autoresearch_on or retry_count >= max_retries:
            if retry_count > 0:
                log(
                    f"  [Autoresearch] Exhausted {retry_count}/{max_retries} retries "
                    f"for task #{task_id}. Final outcome: {outcome}",
                    output,
                )
            break

        retry_count += 1
        log(
            f"  [Autoresearch] Task #{task_id} failed ({outcome}). "
            f"Retry {retry_count}/{max_retries}...",
            output,
        )

        # Clean up failed git branch and reset task for next attempt.
        await cleanup_failed_attempt(
            task_id, project_dir, attempt_reflections, output,
        )

        # Re-fetch task to get clean state (with injected reflections)
        refreshed = fetch_task(task_id)
        if not refreshed:
            log(
                f"  [Autoresearch] Task #{task_id} disappeared from DB. "
                f"Aborting retries.",
                output,
            )
            break
        task = refreshed

    return result, cycles, outcome, loop_total_cost, loop_total_duration, task


def scan_pending_work() -> list[dict]:
    """Query DB for all projects with todo tasks, grouped by priority.

    Returns a list of dicts:
    [
        {
            "project_id": 21,
            "project_name": "EQUIPA",
            "codename": "equipa",
            "status": "active",
            "tasks": [<task dicts sorted by priority>],
            "counts": {"critical": 0, "high": 2, "medium": 1, "low": 0},
            "total_todo": 3,
        },
        ...
    ]
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.description, t.priority, t.project_id,
                   p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as codename,
                   p.status as project_status
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'todo'
            ORDER BY t.project_id, t.created_at ASC
            """,
        ).fetchall()

        # Group by project
        projects: dict[int, dict] = {}
        for row in rows:
            row = dict(row)
            pid = row["project_id"]
            if pid not in projects:
                projects[pid] = {
                    "project_id": pid,
                    "project_name": row["project_name"],
                    "codename": row["codename"],
                    "status": (row.get("project_status") or "unknown").lower(),
                    "tasks": [],
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "total_todo": 0,
                }
            projects[pid]["tasks"].append(row)
            projects[pid]["total_todo"] += 1
            priority = str(row.get("priority", "low")).lower()
            if priority in projects[pid]["counts"]:
                projects[pid]["counts"][priority] += 1

        # Sort tasks within each project by priority descending
        for proj in projects.values():
            proj["tasks"].sort(
                key=lambda t: PRIORITY_ORDER.get(
                    str(t.get("priority", "low")).lower(), 0
                ),
                reverse=True,
            )

        return list(projects.values())
    finally:
        conn.close()


def score_project(summary: dict, config: dict) -> int:
    """Score a project for dispatch priority.

    score = (critical*10) + (high*5) + (medium*2) + (low*1)
           + 3 if project status is 'active'
           + priority_boost from config
    """
    counts = summary["counts"]
    score = (
        counts.get("critical", 0) * 10
        + counts.get("high", 0) * 5
        + counts.get("medium", 0) * 2
        + counts.get("low", 0) * 1
    )

    if summary.get("status") == "active":
        score += 3

    # Apply manual boost from config
    codename = summary.get("codename", "").lower()
    boost = config.get("priority_boost", {})
    if codename in boost:
        score += boost[codename]
    # Also check by project_id string
    pid_str = str(summary.get("project_id", ""))
    if pid_str in boost:
        score += boost[pid_str]

    summary["score"] = score
    return score


# --- Config Loading & Filters ---

def apply_dispatch_filters(work: list[dict], config: dict, args) -> list[dict]:
    """Apply skip_projects, only_projects, and --only-project filters.

    Returns filtered list of project summaries.
    """
    filtered = list(work)

    # --only-project CLI args take highest priority
    cli_only = getattr(args, "only_project", None) or []
    if cli_only:
        cli_only_set = set(cli_only)
        filtered = [p for p in filtered if p["project_id"] in cli_only_set]
        return filtered

    # Config-level only_projects (whitelist mode)
    config_only = config.get("only_projects", [])
    if config_only:
        only_set: set[int] = set()
        for item in config_only:
            if isinstance(item, int):
                only_set.add(item)
            elif isinstance(item, str):
                # Match by codename
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        only_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] in only_set]
        return filtered

    # Config-level skip_projects
    skip_list = config.get("skip_projects", [])
    if skip_list:
        skip_set: set[int] = set()
        for item in skip_list:
            if isinstance(item, int):
                skip_set.add(item)
            elif isinstance(item, str):
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        skip_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] not in skip_set]

    return filtered


# --- Per-Project Task Runner ---

async def run_project_tasks(
    project_summary: dict,
    config: dict,
    args,
    output: list[str] | None = None,
) -> dict:
    """Run Dev+Test loops on todo tasks for one project, in priority order.

    Returns a dict with results per task.
    """
    project_id = project_summary["project_id"]
    codename = project_summary.get("codename", "unknown")
    tasks = project_summary["tasks"]

    # Apply max_tasks_per_project cap
    max_tasks = getattr(args, "max_tasks_per_project", None)
    if max_tasks is None:
        max_tasks = config.get("max_tasks_per_project", 5)
    if len(tasks) > max_tasks:
        log(f"  [{codename}] Capping to {max_tasks} tasks (of {len(tasks)} todo)", output)
        tasks = tasks[:max_tasks]

    # Resolve project directory
    codename_lower = codename.lower().strip()
    project_dir = PROJECT_DIRS.get(codename_lower)
    if not project_dir:
        log(f"  [{codename}] ERROR: No directory mapped. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "No directory mapped",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    if not Path(project_dir).exists():
        log(f"  [{codename}] ERROR: Directory does not exist: {project_dir}. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "Directory does not exist",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    project_context = fetch_project_context(project_id)

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    # Build args namespace for dev-test loop
    task_args = argparse.Namespace(
        model=config.get("model", args.model),
        max_turns=config.get("max_turns", args.max_turns),
        dispatch_config=config,  # pass config so get_role_turns can read per-role limits
    )

    for i, task_row in enumerate(tasks, 1):
        task_id = task_row["id"]
        log(f"\n  [{codename}] Task {i}/{len(tasks)}: #{task_id} - {task_row['title']}", output)

        # Re-fetch task to get full data with project info
        task = fetch_task(task_id)
        if not task:
            log(f"  [{codename}] Task #{task_id} not found in DB. Skipping.", output)
            continue

        # Check if still todo
        if task.get("status") != "todo":
            log(f"  [{codename}] Task #{task_id} status is '{task.get('status')}'. Skipping.", output)
            continue

        # --- Lifecycle hooks: pre_dispatch ---
        await fire_hook(
            "pre_dispatch",
            task_id=task_id, project_dir=project_dir, codename=codename,
            title=task.get("title", ""),
        )

        # --- Autoresearch retry loop with cross-attempt memory ---
        # Bug 2282: extracted into module-level helper so run_parallel_tasks
        # can reuse the same retry semantics. Both code paths now share one
        # canonical autoresearch wrapper.
        result, cycles, outcome, _loop_cost, _loop_duration, task = (
            await run_dev_test_loop_with_autoresearch(
                task, project_dir, project_context, task_args, config, output=output,
            )
        )
        total_cost += _loop_cost
        total_duration += _loop_duration

        # Orchestrator-side DB update (don't rely on agent)
        update_task_status(task_id, outcome, output=output)

        # ForgeSmith telemetry
        task_role = task.get("role") or "developer"
        record_agent_run(
            task, result, outcome, role=task_role,
            model=get_role_model(task_role, task_args, task=task),
            max_turns=get_role_turns(task_role, task_args, task=task),
            cycle_number=cycles, output=output,
        )

        # Post-task quality scoring (on success only)
        if outcome in ("tests_passed", "no_tests"):
            run_quality_scoring(task, result, outcome, role=task_role, output=output,
                                dispatch_config=config)

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, outcome, role=task_role, output=output)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task_id, outcome, output=output)

        # --- Lifecycle hooks: post_task_complete ---
        await fire_hook(
            "post_task_complete",
            task_id=task_id, project_dir=project_dir, codename=codename,
            outcome=outcome, cycles=cycles,
            cost=result.get("cost"), duration=result.get("duration", 0),
        )

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            log(f"  [{codename}] Task #{task_id}: COMPLETED ({outcome})", output)
        else:
            blocked.append(task)
            log(f"  [{codename}] Task #{task_id}: BLOCKED ({outcome})", output)

    return {
        "project_id": project_id,
        "codename": codename,
        "tasks_attempted": len(tasks),
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": 0,
        "error": None,
        "total_cost": total_cost,
        "total_duration": total_duration,
    }


async def run_project_dispatch(
    project_summary: dict,
    semaphore: asyncio.Semaphore,
    config: dict,
    args,
) -> dict:
    """Wrapper for concurrent execution of one project's tasks.

    Acquires semaphore slot, runs tasks, returns result with buffered output.
    """
    codename = project_summary.get("codename", "unknown")
    output: list[str] = []

    log(f"\n[{codename}] Queued ({project_summary['total_todo']} todo tasks, "
        f"score: {project_summary.get('score', '?')})", output)

    async with semaphore:
        log(f"[{codename}] Acquired slot, starting...", output)

        try:
            result = await run_project_tasks(
                project_summary, config, args, output=output,
            )
        except Exception as e:
            # TELEMETRY safety net: run_project_tasks transitively invokes
            # AI APIs, subprocess, git, and DB calls — any of which can raise
            # unbounded exception types. Keep broad but log with traceback so
            # failures aren't silently swallowed.
            logger.exception(
                "[Dispatch] project '%s' raised during run_project_tasks", codename
            )
            log(f"[{codename}] EXCEPTION: {e}", output)
            result = {
                "project_id": project_summary["project_id"],
                "codename": codename,
                "tasks_attempted": 0,
                "tasks_completed": [],
                "tasks_blocked": [],
                "tasks_skipped": project_summary["total_todo"],
                "error": str(e),
                "total_cost": 0.0,
                "total_duration": 0.0,
            }

    result["output"] = output
    return result


async def run_auto_dispatch(scored: list[dict], config: dict, args) -> None:
    """Run all project dispatches concurrently with semaphore.

    Prints each project's buffered output as it completes, then summary.
    """
    max_concurrent = config.get("max_concurrent", 4)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nDispatching {len(scored)} projects "
          f"(max {max_concurrent} concurrent)...\n")

    coros = [
        run_project_dispatch(proj, semaphore, config, args)
        for proj in scored
    ]

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Print each project's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  PROJECT EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: {r.get('codename', '?')}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_dispatch_summary(results)


# --- Goals ---

def load_goals_file(filepath: str | Path) -> tuple[dict, list[dict]]:
    """Parse and validate a goals JSON file.

    Returns (defaults_dict, goals_list) tuple.
    Exits with error on invalid input.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: Goals file not found: {filepath}")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in goals file: {e}")
        sys.exit(1)

    if "goals" not in data or not isinstance(data["goals"], list):
        print("ERROR: Goals file must contain a 'goals' array")
        sys.exit(1)

    if not data["goals"]:
        print("ERROR: Goals array is empty")
        sys.exit(1)

    # Extract defaults
    defaults = {
        "max_concurrent": data.get("max_concurrent", 4),
        "model": data.get("model", DEFAULT_MODEL),
        "max_turns": data.get("max_turns", DEFAULT_MAX_TURNS),
        "max_rounds": data.get("max_rounds", MAX_MANAGER_ROUNDS),
    }

    # Validate each goal
    for i, g in enumerate(data["goals"]):
        if "goal" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'goal' field")
            sys.exit(1)
        if "project_id" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'project_id' field")
            sys.exit(1)

    return defaults, data["goals"]


def validate_goals(goals: list[dict]) -> list[dict]:
    """Validate goals: check project_ids exist, dirs exist, no duplicates.

    Returns list of resolved goal dicts with project_dir and project_info added.
    Exits with error on validation failure.
    """
    # Check for duplicate project_ids
    project_ids = [g["project_id"] for g in goals]
    seen: set[int] = set()
    for pid in project_ids:
        if pid in seen:
            print(f"ERROR: Duplicate project_id {pid} in goals file. "
                  f"Two goals cannot target the same project (they'd write to the same directory).")
            sys.exit(1)
        seen.add(pid)

    resolved = []
    for i, g in enumerate(goals):
        project_info = fetch_project_info(g["project_id"])
        if not project_info:
            print(f"ERROR: Goal #{i + 1}: Project {g['project_id']} not found in TheForge")
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(f"ERROR: Goal #{i + 1}: No directory mapped for project "
                  f"'{project_info.get('name', 'Unknown')}'")
            sys.exit(1)

        if not Path(project_dir).exists():
            print(f"ERROR: Goal #{i + 1}: Directory does not exist: {project_dir}")
            sys.exit(1)

        resolved.append({
            **g,
            "project_dir": project_dir,
            "project_info": project_info,
        })

    return resolved


async def run_single_goal(
    goal_entry: dict,
    semaphore: asyncio.Semaphore,
    index: int,
    defaults: dict,
    args,
) -> dict:
    """Run a single Manager loop for one goal, respecting the semaphore.

    Returns a result dict with goal info and outcome.
    """
    goal_text = goal_entry["goal"]
    project_id = goal_entry["project_id"]
    project_dir = goal_entry["project_dir"]
    project_name = goal_entry["project_info"].get("name", "Unknown")

    # Per-goal overrides or defaults
    model = goal_entry.get("model", defaults["model"])
    max_turns = goal_entry.get("max_turns", defaults["max_turns"])
    max_rounds = goal_entry.get("max_rounds", defaults["max_rounds"])

    # Create a namespace that looks like args for the manager loop
    goal_args = argparse.Namespace(
        model=model,
        max_turns=max_turns,
        max_rounds=max_rounds,
    )

    output: list[str] = []  # Buffer all output for this goal
    log(f"\n[Goal {index + 1}] {goal_text}", output)
    log(f"  Project: {project_name} (ID: {project_id})", output)
    log(f"  Directory: {project_dir}", output)
    log(f"  Model: {model}, Max turns: {max_turns}, Max rounds: {max_rounds}", output)

    async with semaphore:
        log(f"\n[Goal {index + 1}] Acquired slot, starting...", output)
        project_context = fetch_project_context(project_id)

        try:
            outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
                goal_text, project_id, project_dir, project_context,
                goal_args, output=output,
            )
        except Exception as e:
            # TELEMETRY safety net: run_manager_loop invokes AI APIs, subprocess,
            # git, and DB calls. Keep broad — narrowing risks dropping a real
            # failure path — but log with traceback for diagnostics.
            logger.exception(
                "[Goal %d] '%s' raised during run_manager_loop",
                index + 1, project_name,
            )
            log(f"\n[Goal {index + 1}] EXCEPTION: {e}", output)
            return {
                "index": index,
                "goal": goal_text,
                "project_name": project_name,
                "project_id": project_id,
                "outcome": "exception",
                "error": str(e),
                "rounds": 0,
                "completed": [],
                "blocked": [],
                "cost": 0.0,
                "duration": 0.0,
                "output": output,
            }

        print_manager_summary(
            goal_text, outcome, rounds, completed, blocked, cost, duration,
            output=output,
        )

    return {
        "index": index,
        "goal": goal_text,
        "project_name": project_name,
        "project_id": project_id,
        "outcome": outcome,
        "rounds": rounds,
        "completed": completed,
        "blocked": blocked,
        "cost": cost,
        "duration": duration,
        "output": output,
    }


async def run_parallel_goals(resolved_goals: list[dict], defaults: dict, args) -> None:
    """Run multiple Manager loops concurrently with a semaphore.

    Prints each goal's buffered output as it completes, then a combined summary.
    """
    max_concurrent = args.max_concurrent or defaults["max_concurrent"]
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nStarting {len(resolved_goals)} parallel goals "
          f"(max {max_concurrent} concurrent)...\n")

    # Launch all goals
    tasks = [
        run_single_goal(g, semaphore, i, defaults, args)
        for i, g in enumerate(resolved_goals)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Print each goal's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  GOAL EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: Goal {r['index'] + 1} — {r['project_name']}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_parallel_summary(results)


# --- Parallel Tasks ---

def parse_task_ids(task_str: str) -> list[int]:
    """Parse comma-separated IDs or ranges into a list of ints.

    Examples: "109,110,111" -> [109, 110, 111]
              "109-114" -> [109, 110, 111, 112, 113, 114]
              "109,112-114" -> [109, 112, 113, 114]

    Each "start-end" range is bounded by MAX_TASK_RANGE to prevent memory
    exhaustion (e.g. "1-999999999" would otherwise materialise ~1B ints).
    """
    ids: list[int] = []
    for part in task_str.split(","):
        part = part.strip()
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            if end - start > MAX_TASK_RANGE:
                raise ValueError(
                    f"task range too large: {part} (max {MAX_TASK_RANGE})"
                )
            ids.extend(range(start, end + 1))
        else:
            ids.append(int(part))
    return ids


# --- Worktree isolation helpers removed in Phase 1 convergence (2026-05-23) ---
# Functions _copy_hooks_to_worktree, _create_isolation_worktrees, _merge_task_branch,
# _stash_uncommitted_in_worktree, and _cleanup_worktrees have been replaced by the
# canonical implementation in equipa.worktree_manager. The parallel-dispatch flow
# in run_parallel_tasks now uses create_worktree / merge_worktree_back / destroy_worktree
# / mark_worktree_failed / mark_worktree_conflict from that module so single-task and
# parallel dispatch share one implementation.

async def run_parallel_tasks(task_ids: list[int], args) -> None:
    """Run multiple tasks concurrently with dev-test loops.

    All tasks must belong to the same project (for safety).

    When ``args.use_flow`` is true (or the parallel fanout has more than
    one task), a row in the ``flows`` table tracks the orchestration so
    the run survives a Claudinator restart and supports sticky cancel.
    """
    # Fetch all tasks
    tasks = fetch_tasks_by_ids(task_ids)
    if not tasks:
        print("ERROR: No tasks found for given IDs.")
        return

    # Verify all tasks are from the same project
    project_ids = set(t.get("project_id") for t in tasks)
    if len(project_ids) > 1:
        print(f"ERROR: --tasks requires all tasks from the same project. "
              f"Found project IDs: {project_ids}")
        return

    project_id = tasks[0].get("project_id")
    project_dir = resolve_project_dir(tasks[0])
    if not project_dir:
        print("ERROR: Could not resolve project directory.")
        return
    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        return

    project_context = fetch_project_context(project_id)
    max_concurrent = getattr(args, "max_concurrent", None) or 4
    semaphore = asyncio.Semaphore(max_concurrent)

    # --- Task Flow tracking (durable revisions + sticky cancel) ---
    # The flow row is created up front so a mid-run restart can find it.
    flow_id: int | None = None
    use_flow = getattr(args, "use_flow", None)
    if use_flow is None:
        use_flow = len(tasks) > 1
    if use_flow:
        try:
            from equipa import flows as _flows
            flow = _flows.create_flow(
                project_id=project_id,
                title=(getattr(args, "flow_title", None)
                       or f"parallel fanout: {len(tasks)} tasks"),
                metadata={
                    "task_ids": [t["id"] for t in tasks],
                    "max_concurrent": max_concurrent,
                },
            )
            flow_id = flow.id
            for t in tasks:
                _flows.add_child(
                    flow_id,
                    t["id"],
                    role=t.get("role") or "developer",
                    relationship="mirrored",
                )
            _flows.transition(
                flow_id,
                "running",
                event="dispatch_start",
                payload={"task_count": len(tasks)},
            )
            print(f"  [Flow] tracking fanout under flow_id={flow_id}")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[flows] could not create flow: %s", e)
            flow_id = None

    print(f"\nParallel task execution: {len(tasks)} tasks, max {max_concurrent} concurrent")
    for t in tasks:
        print(f"  - #{t['id']}: {t['title']}")

    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Create per-task git worktrees for filesystem isolation.
    # Phase 1 convergence (2026-05-23): the parallel path now uses
    # worktree_manager.create() so single-task and parallel dispatch share
    # one canonical implementation. Tasks that fail worktree creation fall
    # back to sharing project_dir (logged as a warning).
    use_worktrees = len(tasks) > 1 and _is_git_repo(project_dir)
    worktree_dirs: dict[int, str] = {}
    if use_worktrees:
        for t in tasks:
            try:
                record = await create_worktree(t["id"], project_dir)
                worktree_dirs[t["id"]] = record.path
                print(f"  [Isolation] Task #{t['id']} -> {Path(record.path).name}")
            except WorktreeError as e:
                print(
                    f"  [Isolation] WARNING: worktree creation failed for "
                    f"task #{t['id']}, using shared dir: {e}"
                )

    async def run_one_task(task):
        output = []
        # Use worktree if available, otherwise shared project_dir
        task_dir = worktree_dirs.get(task["id"], project_dir)
        # Honour sticky cancel: if the flow was cancelled before we reached
        # this task, skip the dev-test loop entirely.
        if flow_id is not None:
            try:
                from equipa import flows as _flows
                if _flows.is_cancelled(flow_id):
                    log(
                        f"[Task #{task['id']}] Skipped — flow_id={flow_id} "
                        f"is sticky-cancelled",
                        output,
                    )
                    return {
                        "task": task,
                        "result": {"cost": 0, "duration": 0},
                        "cycles": 0,
                        "outcome": "cancelled",
                        "output": output,
                        "merge_ok": False,
                        "needs_merge": False,
                    }
            except Exception:  # pragma: no cover
                logger.exception("[flows] cancel check failed")

        async with semaphore:
            # Re-check after acquiring the semaphore — the flow may have
            # been cancelled while we were queued.
            if flow_id is not None:
                try:
                    from equipa import flows as _flows
                    if _flows.is_cancelled(flow_id):
                        return {
                            "task": task,
                            "result": {"cost": 0, "duration": 0},
                            "cycles": 0,
                            "outcome": "cancelled",
                            "output": output,
                            "merge_ok": False,
                            "needs_merge": False,
                        }
                    _flows.update_child_state(flow_id, task["id"], "running")
                except Exception:  # pragma: no cover
                    logger.exception("[flows] child running update failed")

            log(f"\n[Task #{task['id']}] Starting: {task['title']}", output)
            # Bug 2282 fix: parallel mode now gets the same autoresearch
            # retry wrapper as single-task mode. Without this, BashSecurity
            # false positives or analysis-paralysis early-terms in any of
            # the N parallel tasks would be final with no reflection-
            # injected retry. The helper is module-level above.
            _config_for_loop = getattr(args, "dispatch_config", None) or {}
            result, cycles, outcome, _, _, task = (
                await run_dev_test_loop_with_autoresearch(
                    task, task_dir, project_context, args, _config_for_loop, output=output,
                )
            )
            update_task_status(task["id"], outcome, output=output)
            log(f"[Task #{task['id']}] Done: {outcome} ({cycles} cycles)", output)
            if flow_id is not None:
                try:
                    from equipa import flows as _flows
                    child_state = (
                        "done"
                        if outcome in ("tests_passed", "no_tests")
                        else "failed"
                    )
                    _flows.update_child_state(
                        flow_id, task["id"], child_state,
                        payload={"outcome": outcome, "cycles": cycles},
                    )
                except _flows.FlowCancelled:
                    # Sticky cancel beat us to the punch — that's fine.
                    pass
                except Exception:  # pragma: no cover
                    logger.exception("[flows] child terminal update failed")
            # Record telemetry
            task_role = task.get("role") or "developer"
            record_agent_run(
                task, result, outcome, role=task_role,
                model=get_role_model(task_role, args, task=task),
                max_turns=get_role_turns(task_role, args, task=task),
                cycle_number=cycles, output=output,
            )

            # Mark for post-gather sequential merge (avoid parallel merge conflicts)
            merge_ok = False
            needs_merge = task["id"] in worktree_dirs and outcome in ("tests_passed", "no_tests")

            return {
                "task": task,
                "result": result,
                "cycles": cycles,
                "outcome": outcome,
                "output": output,
                "merge_ok": merge_ok,
                "needs_merge": needs_merge,
            }

    results = await asyncio.gather(
        *[run_one_task(t) for t in tasks],
        return_exceptions=True,
    )

    # Reconcile flow state from final child outcomes.
    if flow_id is not None:
        try:
            from equipa import flows as _flows
            _flows.reconcile_after_restart(flow_id)
        except Exception:  # pragma: no cover
            logger.exception("[flows] post-gather reconcile failed")

    # Print results
    print(f"\n{'#' * 60}")
    print("PARALLEL TASKS SUMMARY")
    print(f"{'#' * 60}")

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  EXCEPTION: {r}")
            continue

        task = r["task"]
        outcome = r["outcome"]
        result = r["result"]

        # Print buffered output
        for line in r.get("output", []):
            print(line)

        cost = result.get("cost", 0) or 0
        duration = result.get("duration", 0)
        total_cost += cost
        total_duration += duration

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            print(f"\n  #{task['id']}: COMPLETED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")
        else:
            blocked.append(task)
            print(f"\n  #{task['id']}: BLOCKED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")

    print(f"\nTotal: {len(completed)} completed, {len(blocked)} blocked")
    print(f"Duration: {total_duration:.0f}s total")
    if total_cost > 0:
        print(f"Cost: ${total_cost:.4f}")
    print(f"{'#' * 60}")

    # Sequential merge + cleanup — merge task branches one at a time via
    # worktree_manager so single-task and parallel dispatch share one
    # canonical implementation. On successful merge, destroy the worktree.
    # On failed task (outcome not in success set) or merge failure, the
    # worktree is retained on disk for operator inspection — Phase 3
    # introduces --cleanup-worktrees for batch cleanup.
    if use_worktrees:
        for r in results:
            if isinstance(r, Exception):
                continue
            task_id = r["task"]["id"]
            if task_id not in worktree_dirs:
                continue

            if r["outcome"] in ("tests_passed", "no_tests"):
                merge_result = await merge_worktree_back(task_id)
                if merge_result.ok:
                    r["merge_ok"] = True
                    await destroy_worktree(task_id)
                else:
                    await mark_worktree_conflict(task_id, merge_result)
                    print(
                        f"  [Isolation] Task #{task_id} merge FAILED: "
                        f"{merge_result.message}. Worktree retained at "
                        f"{worktree_dirs[task_id]}"
                    )
            else:
                await mark_worktree_failed(task_id)
                print(
                    f"  [Isolation] Task #{task_id} {r['outcome']}; "
                    f"worktree retained at {worktree_dirs[task_id]}"
                )
