"""EQUIPA output module — logging and summary printing functions.

Layer 1: Pure output functions with no dependencies beyond constants.
Extracted from forge_orchestrator.py as part of Phase 2 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from pathlib import Path

from equipa.constants import (
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    MAX_DEV_TEST_CYCLES,
    NO_PROGRESS_LIMIT,
    PROJECT_DIRS,
)


def log(msg: str, output: list[str] | None = None) -> None:
    """Print a message or buffer it for later display.

    In single-goal mode, output is None and messages print immediately.
    In parallel mode, output is a list and messages are collected for
    display after the goal completes.
    """
    if output is not None:
        output.append(msg)
    else:
        print(msg)


def print_manager_summary(
    goal: str,
    outcome: str,
    rounds: int,
    completed: list[dict],
    blocked: list[dict],
    cost: float,
    duration: float,
    output: list[str] | None = None,
) -> None:
    """Print a formatted summary of the Manager mode run."""
    outcome_messages = {
        "goal_complete": "Goal achieved successfully",
        "goal_blocked": "Goal blocked — cannot proceed",
        "planner_failed": "Planner failed to create tasks",
        "rounds_exhausted": f"Max rounds ({rounds}) exhausted without completion",
    }

    is_success = outcome == "goal_complete"

    log("\n" + "#" * 60, output)
    log("EQUIPA MANAGER MODE SUMMARY", output)
    log("#" * 60, output)

    log(f"\nGoal:      {goal[:100]}", output)
    log(f"Verdict:   {'SUCCESS' if is_success else 'INCOMPLETE'}", output)
    log(f"Outcome:   {outcome_messages.get(outcome, outcome)}", output)
    log(f"Rounds:    {rounds}", output)
    log(f"Tasks:     {len(completed)} completed, {len(blocked)} blocked", output)
    log(f"Duration:  {duration:.1f}s total", output)
    if cost > 0:
        log(f"Cost:      ${cost:.4f} total", output)

    if completed:
        log(f"\nCompleted Tasks:", output)
        for t in completed:
            log(f"  - #{t['id']} {t['title']}", output)

    if blocked:
        log(f"\nBlocked Tasks:", output)
        for t in blocked:
            log(f"  - #{t['id']} {t['title']}", output)

    log("\n" + "#" * 60, output)


def _print_task_summary(
    title: str,
    task: dict,
    result: dict,
    verified: bool,
    verify_msg: str,
    cycles: int | None = None,
    outcome: str | None = None,
) -> None:
    """Print a formatted summary of an agent run (single or dev-test)."""
    outcome_messages = {
        "tests_passed": "All tests passed",
        "no_tests": "No tests found, Developer result accepted",
        "developer_blocked": "Developer marked task as blocked",
        "developer_timeout": "Developer agent timed out",
        "developer_failed": "Developer agent failed",
        "tester_blocked": "Tester could not run (build error, missing deps)",
        "tester_timeout": "Tester agent timed out",
        "tests_inconclusive": "All tests skipped — no actual validation occurred (likely missing env vars)",
        "no_progress": f"No file changes for {NO_PROGRESS_LIMIT} consecutive cycles",
        "cycles_exhausted": f"All {MAX_DEV_TEST_CYCLES} fix-test cycles used without passing",
    }

    is_success = (outcome in ("tests_passed", "no_tests") if outcome
                  else result.get("success", False))

    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

    print(f"\nTask:      #{task['id']} - {task['title']}")
    print(f"Project:   {task.get('project_name', 'Unknown')}")
    print(f"Verdict:   {'SUCCESS' if is_success else 'BLOCKED' if outcome else ('SUCCESS' if result.get('success') else 'FAILED')}")
    if cycles is not None:
        print(f"Cycles:    {cycles}/{MAX_DEV_TEST_CYCLES}")
    if outcome:
        print(f"Outcome:   {outcome_messages.get(outcome, outcome)}")
    if not outcome:
        print(f"Turns:     {result.get('num_turns', 0)}")
    print(f"Duration:  {result.get('duration', 0):.1f}s{'  total' if outcome else ''}")

    cost = result.get("cost")
    if cost is not None:
        print(f"Cost:      ${cost:.4f}{'  total' if outcome else ''}")

    print(f"\nDB Verify: {'PASS' if verified else 'FAIL'} - {verify_msg}")

    if result.get("errors"):
        print("\nErrors:")
        for err in result["errors"]:
            print(f"  - {err[:200]}{'...' if len(err) > 200 else ''}")

    output_text = result.get("result_text", "")
    if output_text:
        label = "Last Agent Output" if outcome else "Agent Output"
        print(f"\n{label} (last 500 chars):")
        print("-" * 40)
        tail = output_text[-500:] if len(output_text) > 500 else output_text
        print(tail.encode("ascii", errors="replace").decode("ascii"))

    print("\n" + "=" * 60)


def print_summary(
    task: dict, result: dict, verified: bool, verify_msg: str
) -> None:
    """Print a formatted summary of a single agent run."""
    _print_task_summary("EQUIPA AGENT RUN SUMMARY",
                        task, result, verified, verify_msg)


def print_dev_test_summary(
    task: dict,
    result: dict,
    cycles: int,
    outcome: str,
    verified: bool,
    verify_msg: str,
) -> None:
    """Print a formatted summary of the dev-test loop run."""
    _print_task_summary("EQUIPA DEV-TEST LOOP SUMMARY",
                        task, result, verified, verify_msg,
                        cycles=cycles, outcome=outcome)


def _print_batch_summary(
    title: str, results: list, mode: str = "goals"
) -> None:
    """Print summary for parallel goals or auto-dispatch results.

    mode="goals": expects results with outcome, completed, blocked, goal keys.
    mode="dispatch": expects results with tasks_completed, tasks_blocked, codename keys.
    """
    print(f"\n{'#' * 60}")
    print(title)
    print(f"{'#' * 60}")

    total_completed, total_blocked = 0, 0
    total_cost, total_duration = 0.0, 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  [?] EXCEPTION: {r}")
            continue

        if mode == "goals":
            n_completed = len(r.get("completed", []))
            n_blocked = len(r.get("blocked", []))
            cost, duration = r.get("cost", 0.0), r.get("duration", 0.0)
            status = "OK" if r.get("outcome") == "goal_complete" else r.get("outcome", "?").upper()
            label = f"[{r.get('index', 0) + 1}] {r.get('project_name', '?')}"
            print(f"\n  {label} — {status}")
            print(f"      Goal: {r.get('goal', '')[:80]}")
            print(f"      Rounds: {r.get('rounds', '?')}, "
                  f"Tasks: {n_completed} done / {n_blocked} blocked, "
                  f"Duration: {duration:.1f}s")
        else:  # dispatch
            n_completed = len(r.get("tasks_completed", []))
            n_blocked = len(r.get("tasks_blocked", []))
            cost, duration = r.get("total_cost", 0.0), r.get("total_duration", 0.0)
            codename = r.get("codename", "?")
            if r.get("error"):
                print(f"\n  [{codename}] ERROR: {r['error']}")
                total_cost += cost
                total_duration += duration
                continue
            status = ("ALL DONE" if n_blocked == 0 and n_completed > 0
                      else "NO TASKS" if n_completed == 0 and n_blocked == 0
                      else "PARTIAL")
            print(f"\n  [{codename}] {status}")
            print(f"      Tasks: {n_completed} completed, {n_blocked} blocked "
                  f"(of {r.get('tasks_attempted', '?')} attempted)")
            print(f"      Duration: {duration:.1f}s")
            for t in r.get("tasks_completed", []):
                print(f"        + #{t['id']} {t['title']}")
            for t in r.get("tasks_blocked", []):
                print(f"        x #{t['id']} {t['title']}")

        total_completed += n_completed
        total_blocked += n_blocked
        total_cost += cost
        total_duration += duration
        if cost > 0:
            print(f"      Cost: ${cost:.4f}")

    print(f"\n  {'=' * 40}")
    items = f"{len(results)} goals" if mode == "goals" else f"{total_completed} completed, {total_blocked} blocked"
    print(f"  TOTALS: {items}")
    print(f"  Duration: {total_duration:.1f}s")
    if total_cost > 0:
        print(f"  Cost: ${total_cost:.4f}")
    print(f"\n{'#' * 60}")


def print_parallel_summary(results: list) -> None:
    """Print a combined summary table for all parallel goals."""
    _print_batch_summary("EQUIPA PARALLEL GOALS SUMMARY", results, mode="goals")


def print_dispatch_summary(results: list) -> None:
    """Final report: tasks completed/blocked per project."""
    _print_batch_summary("AUTO-RUN DISPATCH SUMMARY", results, mode="dispatch")


def print_dispatch_plan(scored: list, config: dict) -> None:
    """Preview: which projects will run, scores, task counts."""
    from equipa.monitoring import calculate_dynamic_budget

    max_tasks = config.get("max_tasks_per_project", 5)
    max_concurrent = config.get("max_concurrent", 4)

    print(f"\n{'#' * 60}")
    print("AUTO-RUN DISPATCH PLAN")
    print(f"{'#' * 60}")
    print(f"\n  Max concurrent: {max_concurrent}")
    print(f"  Max tasks/project: {max_tasks}")
    print(f"  Model: {config.get('model', DEFAULT_MODEL)}")
    max_turns_val = config.get("max_turns", DEFAULT_MAX_TURNS)
    start_budget, _ = calculate_dynamic_budget(max_turns_val)
    print(f"  Max turns: {max_turns_val} (dynamic start: {start_budget})")
    print()

    total_tasks = 0
    for i, proj in enumerate(scored, 1):
        counts = proj["counts"]
        capped = min(proj["total_todo"], max_tasks)
        total_tasks += capped

        codename_lower = proj.get("codename", "").lower().strip()
        has_dir = (
            codename_lower in PROJECT_DIRS
            and Path(PROJECT_DIRS.get(codename_lower, "")).exists()
        )

        print(
            f"  [{i}] {proj['project_name']} (ID: {proj['project_id']}, "
            f"codename: {proj.get('codename', '?')})"
        )
        print(
            f"      Score: {proj.get('score', '?')} | "
            f"Status: {proj.get('status', '?')}"
        )
        print(
            f"      Tasks: {capped}/{proj['total_todo']} "
            f"(C:{counts['critical']} H:{counts['high']} "
            f"M:{counts['medium']} L:{counts['low']})"
        )
        print(f"      Dir: {'OK' if has_dir else 'MISSING'}")
        print()

    print(f"  TOTAL: {len(scored)} projects, {total_tasks} tasks to attempt")
    print(f"\n{'#' * 60}")
