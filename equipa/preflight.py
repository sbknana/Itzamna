"""EQUIPA preflight — dependency installation and build checks.

Layer 7: Imports from equipa.constants, equipa.output, equipa.agent_runner, equipa.prompts,
         equipa.roles.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from equipa.agent_runner import AgentResult

from equipa.constants import (
    AUTOFIX_COST_LIMIT,
    AUTOFIX_DEBUGGER_BUDGET,
    AUTOFIX_MAX_DEBUGGER_CYCLES,
    AUTOFIX_PLANNER_BUDGET,
    PREFLIGHT_SKIP_KEYWORDS,
    PREFLIGHT_TIMEOUT,
)
from equipa.output import log


async def _run_install_cmd(
    cmd: list[str],
    cwd: str,
    label: str,
    output: Any = None,
) -> bool:
    """Run an install command, log result. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            log(f"  [Auto-Install] {label} installed successfully.", output)
            return True
        err = stderr.decode("utf-8", errors="replace")[:200]
        log(f"  [Auto-Install] {label} failed (rc={proc.returncode}): {err}", output)
    except FileNotFoundError:
        log(f"  [Auto-Install] {cmd[0]} not found. Skipping {label}.", output)
    except Exception as e:
        log(f"  [Auto-Install] {label} error: {e}", output)
    return False


async def auto_install_dependencies(project_dir: str, output: Any = None) -> None:
    """Auto-install project dependencies if manifest exists but deps are missing."""
    pdir = Path(project_dir)

    # Python: pyproject.toml or requirements.txt without venv
    has_pyproject = (pdir / "pyproject.toml").exists()
    has_requirements = (pdir / "requirements.txt").exists()
    has_venv = (pdir / "venv").exists() or (pdir / ".venv").exists()

    if (has_pyproject or has_requirements) and not has_venv:
        log(f"  [Auto-Install] Python project without venv detected. Installing...", output)
        venv_path = pdir / "venv"
        await _run_install_cmd(
            [sys.executable, "-m", "venv", str(venv_path)], str(pdir), "Python venv", output)
        pip_path = venv_path / "bin" / "pip"
        if not pip_path.exists():
            pip_path = venv_path / "Scripts" / "pip"
        install_cmd = ([str(pip_path), "install", "-e", f"{project_dir}[dev]"]
                       if has_pyproject
                       else [str(pip_path), "install", "-r", str(pdir / "requirements.txt")])
        await _run_install_cmd(install_cmd, str(pdir), "Python deps", output)

    # Node.js: package.json without node_modules
    if (pdir / "package.json").exists() and not (pdir / "node_modules").exists():
        log(f"  [Auto-Install] Node.js project without node_modules detected. Installing...", output)
        await _run_install_cmd(["npm", "install"], str(pdir), "Node.js deps", output)

    # Go: go.mod present
    if (pdir / "go.mod").exists():
        log(f"  [Auto-Install] Go project detected. Running go mod download...", output)
        await _run_install_cmd(["go", "mod", "download"], str(pdir), "Go modules", output)


def _resolve_build_command(project_dir: str) -> tuple[str, list[str] | None, str | None]:
    """Resolve language and build command for a project directory.

    Returns (language: str, build_cmd: list | None, skip_reason: str | None).
    """
    pdir = Path(project_dir)

    if (pdir / "package.json").exists():
        cmd = (["npx", "tsc", "--noEmit"] if (pdir / "tsconfig.json").exists()
               else ["npm", "run", "build"])
        return "node", cmd, None
    if (pdir / "go.mod").exists():
        return "go", ["go", "build", "./..."], None
    if (pdir / "pyproject.toml").exists() or (pdir / "requirements.txt").exists():
        for entry in ("main.py", "app.py"):
            if (pdir / entry).exists():
                return "python", ["python3", "-m", "py_compile", str(pdir / entry)], None
        return "python", None, "no Python entry point found"
    csproj = list(pdir.glob("*.csproj"))
    if csproj:
        return "csharp", ["dotnet", "build", str(csproj[0]), "--no-restore"], None
    return "unknown", None, "no recognized project files"


async def preflight_build_check(
    project_dir: str,
    task_description: str | None = None,
    output: Any = None,
) -> tuple[bool, str, str]:
    """Run a lightweight build check before the developer agent starts.

    Returns (success: bool, language: str, error_details: str).
    """
    # Skip if task description mentions build-fix keywords
    if task_description:
        desc_lower = task_description.lower()
        for keyword in PREFLIGHT_SKIP_KEYWORDS:
            if keyword in desc_lower:
                log(f"  [Preflight] Skipped — task description contains '{keyword}' "
                    f"(task is likely to fix the build)", output)
                return (True, "unknown", f"Skipped: task description contains '{keyword}'")

    language, build_cmd, skip_reason = _resolve_build_command(project_dir)
    if not build_cmd:
        msg = skip_reason or ""
        if msg:
            log(f"  [Preflight] {language} project: {msg}. Skipping build check.", output)
        return (True, language, f"Skipped: {msg}" if msg else "")

    log(f"  [Preflight] Detected {language} project. Running build check: {' '.join(build_cmd)}", output)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_cmd, cwd=str(Path(project_dir)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=PREFLIGHT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            error_msg = f"Build check timed out after {PREFLIGHT_TIMEOUT}s"
            log(f"  [Preflight] TIMEOUT: {error_msg}", output)
            return (False, language, error_msg)

        if proc.returncode == 0:
            log(f"  [Preflight] Build check passed ({language}).", output)
            return (True, language, "")

        combined = (stderr.decode("utf-8", errors="replace") + "\n"
                    + stdout.decode("utf-8", errors="replace")).strip()
        if len(combined) > 1000:
            combined = combined[:1000] + "\n... (truncated)"
        log(f"  [Preflight] Build FAILED ({language}, rc={proc.returncode}). "
            f"Error preview: {combined[:200]}", output)
        return (False, language, combined)

    except FileNotFoundError:
        error_msg = f"Build tool not found for {language}: {build_cmd[0]}"
        log(f"  [Preflight] {error_msg}. Skipping build check.", output)
        return (True, language, f"Skipped: {error_msg}")
    except Exception as e:
        error_msg = f"Preflight error: {e}"
        log(f"  [Preflight] {error_msg}. Continuing without build check.", output)
        return (True, language, f"Skipped: {error_msg}")


async def _dispatch_autofix_agent(
    role: str,
    task_dict: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    budget: int,
    task_id: int,
    cycle: int,
    args: Any,
    output: Any = None,
) -> tuple[AgentResult, float]:
    """Dispatch a single auto-fix agent (debugger or planner). Returns (result, cost)."""
    from equipa.agent_runner import build_cli_command, dispatch_agent
    from equipa.constants import COST_ESTIMATE_PER_TURN
    from equipa.prompts import build_system_prompt
    from equipa.roles import get_role_model

    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    model = get_role_model(role, args, task=task_dict)
    streaming = role != "planner"
    prompt = build_system_prompt(
        task_dict, project_context, project_dir,
        role=role, max_turns=budget, dispatch_config=dispatch_config,
    )
    with build_cli_command(
        prompt, project_dir, budget, model, role=role, streaming=streaming,
    ) as cmd:
        result: AgentResult = await dispatch_agent(
            cmd, role=role, output=output, max_turns=budget, task_id=task_id,
            cycle=cycle, system_prompt=prompt, project_dir=project_dir, args=args,
        )
    cost = result.get("cost") or (result.get("num_turns", 0) * COST_ESTIMATE_PER_TURN)
    return result, cost


async def _handle_preflight_failure(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    preflight_lang: str,
    preflight_error: str,
    args: Any,
    output: Any = None,
) -> tuple[bool, float, str]:
    """Auto-dispatch debugger agent to fix a broken build before the main task.

    Strategy: debugger attempts -> planner analysis -> guided debugger -> give up.
    Returns (fixed: bool, cost: float, summary: str).
    """
    task_id = task["id"]
    total_cost = 0.0

    log(f"  [AutoFix] Build broken — dispatching debugger agent", output)
    log(f"  [AutoFix] Language: {preflight_lang}, error preview: "
        f"{preflight_error[:200]}", output)

    # --- Phase 1: Debugger attempts ---
    for attempt in range(1, AUTOFIX_MAX_DEBUGGER_CYCLES + 1):
        log(f"  [AutoFix] Debugger attempt {attempt}/{AUTOFIX_MAX_DEBUGGER_CYCLES}", output)
        fix_task = {
            "id": task_id,
            "title": f"[AutoFix] Fix {preflight_lang} build errors",
            "description": (
                f"The project build is BROKEN. Your ONLY job is to make it compile clean.\n\n"
                f"**Build error output:**\n```\n{preflight_error}\n```\n\n"
                f"DO NOT work on any other task. DO NOT refactor. DO NOT add features.\n"
                f"Read the error, find the broken file(s), fix them, verify the build passes.\n"
                f"Start writing fixes IMMEDIATELY — do not read more than 3 files."
            ),
        }
        _, cost = await _dispatch_autofix_agent(
            "debugger", fix_task, project_dir, project_context,
            AUTOFIX_DEBUGGER_BUDGET, task_id, attempt, args, output)
        total_cost += cost

        if total_cost >= AUTOFIX_COST_LIMIT:
            log(f"  [AutoFix] Cost limit reached (${total_cost:.2f}). Giving up.", output)
            return False, total_cost, "cost_limit_exceeded"

        fixed, _, new_error = await preflight_build_check(project_dir, output=output)
        if fixed:
            log(f"  [AutoFix] Build FIXED by debugger (attempt {attempt}, cost: ${total_cost:.2f})", output)
            return True, total_cost, f"debugger_fixed_attempt_{attempt}"

        log(f"  [AutoFix] Debugger attempt {attempt} failed. Build still broken.", output)
        if new_error:
            preflight_error = new_error

    # --- Phase 2: Planner analysis ---
    log(f"  [AutoFix] Debugger failed {AUTOFIX_MAX_DEBUGGER_CYCLES}x. Escalating to planner.", output)
    planner_task = {
        "id": task_id,
        "title": f"[AutoFix] Analyze build failure and write fix plan",
        "description": (
            f"The project build is BROKEN and a debugger agent failed to fix it "
            f"after {AUTOFIX_MAX_DEBUGGER_CYCLES} attempts.\n\n"
            f"**Build error output:**\n```\n{preflight_error}\n```\n\n"
            f"Your job: 1) Analyze root cause 2) Identify EXACT files and lines "
            f"3) Write step-by-step fix plan with specific code changes 4) Output "
            f"as a numbered list. Do NOT fix the code — write the plan."
        ),
    }
    planner_result, cost = await _dispatch_autofix_agent(
        "planner", planner_task, project_dir, project_context,
        AUTOFIX_PLANNER_BUDGET, task_id, AUTOFIX_MAX_DEBUGGER_CYCLES + 1, args, output)
    total_cost += cost

    if total_cost >= AUTOFIX_COST_LIMIT:
        log(f"  [AutoFix] Cost limit reached after planner (${total_cost:.2f}). Giving up.", output)
        return False, total_cost, "cost_limit_exceeded"

    plan_text = str(planner_result.get("result") or planner_result.get("output", "No plan."))[:2000]

    # --- Phase 3: Guided debugger with plan ---
    log(f"  [AutoFix] Dispatching debugger with planner's fix plan", output)
    guided_task = {
        "id": task_id,
        "title": f"[AutoFix] Fix build using analysis plan",
        "description": (
            f"The project build is BROKEN. A planner produced this fix plan:\n\n"
            f"**Fix Plan:**\n{plan_text}\n\n"
            f"**Build error output:**\n```\n{preflight_error}\n```\n\n"
            f"Execute the plan. Fix the build. Verify it compiles. Start IMMEDIATELY."
        ),
    }
    _, cost = await _dispatch_autofix_agent(
        "debugger", guided_task, project_dir, project_context,
        AUTOFIX_DEBUGGER_BUDGET, task_id, AUTOFIX_MAX_DEBUGGER_CYCLES + 2, args, output)
    total_cost += cost

    fixed, _, _ = await preflight_build_check(project_dir, output=output)
    if fixed:
        log(f"  [AutoFix] Build FIXED by guided debugger (cost: ${total_cost:.2f})", output)
        return True, total_cost, "planner_guided_fix"

    log(f"  [AutoFix] Build still broken after all attempts (cost: ${total_cost:.2f}).", output)
    return False, total_cost, "all_attempts_failed"
