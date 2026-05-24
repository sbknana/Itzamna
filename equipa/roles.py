"""EQUIPA roles module — role configuration, model selection, and cost tracking.

Layer 2: Imports from equipa.constants. Does not depend on equipa.db.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import equipa.constants as _equipa_constants
from equipa.config import is_feature_enabled
from equipa.constants import (
    COMPLEXITY_MULTIPLIERS,
    COST_ESTIMATE_PER_TURN,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_ROLE_MODELS,
    DEFAULT_ROLE_TURNS,
    PROMPTS_DIR,
    ROLE_PROMPTS,
)


def get_role_turns(
    role: str,
    args: object,
    config: dict | None = None,
    task: dict | None = None,
) -> int:
    """Resolve max turns for a given role, adjusted by task complexity.

    Priority: dispatch config per-role > CLI --max-turns (if non-default) > DEFAULT_ROLE_TURNS
    Then applies complexity multiplier from the task.
    """
    from equipa.tasks import get_task_complexity

    # Check dispatch config for per-role overrides (e.g. "max_turns_developer": 50)
    effective_config = config or getattr(args, "dispatch_config", None)
    base_turns = None
    if effective_config:
        role_key = role.replace("-", "_")  # security-reviewer -> security_reviewer
        config_key = f"max_turns_{role_key}"
        if config_key in effective_config:
            base_turns = effective_config[config_key]

    if base_turns is None:
        # If CLI specified a non-default value, use it for all roles
        cli_turns = getattr(args, "max_turns", DEFAULT_MAX_TURNS)
        if cli_turns != DEFAULT_MAX_TURNS:
            base_turns = cli_turns
        else:
            # Fall back to per-role defaults
            base_turns = DEFAULT_ROLE_TURNS.get(role, DEFAULT_MAX_TURNS)

    # Apply complexity multiplier
    if task:
        complexity = get_task_complexity(task)
        multiplier = COMPLEXITY_MULTIPLIERS.get(complexity, 1.0)
        adjusted = int(base_turns * multiplier)
        # Enforce minimum of 10 turns
        return max(10, adjusted)

    return base_turns


def get_role_model(
    role: str,
    args: object,
    config: dict | None = None,
    task: dict | None = None,
) -> str:
    """Resolve model for a given role and task complexity.

    Priority:
      1. dispatch config per-complexity (e.g. model_epic, model_complex)
      2. dispatch config per-role (e.g. model_developer, model_tester)
      3. CLI --model
      4. dispatch config global model
      5. DEFAULT_ROLE_MODELS
      6. auto-routing (if auto_model_routing feature flag enabled)

    Raises:
        equipa.routing.CircuitOpenError — when auto-routing is enabled AND
            every suitable circuit is OPEN (auto_select_model returned
            None). The dispatch wrapper catches this and demotes the
            task outcome to ``circuit_breaker_blocked``. NEVER silently
            falls through to DEFAULT_ROLE_MODELS in that case (2453-S1):
            DEFAULT_ROLE_MODELS maps developer/security-reviewer/planner
            /frontend-designer/debugger to "opus", so silent fallthrough
            would defeat RT-02 end-to-end.
    """
    from equipa.tasks import get_task_complexity

    effective_config = config or getattr(args, "dispatch_config", None)

    if effective_config and task:
        # Check complexity-based model override
        complexity = get_task_complexity(task)
        complexity_key = f"model_{complexity}"
        if complexity_key in effective_config:
            return effective_config[complexity_key]

    if effective_config:
        # Check role-based model override
        role_key = role.replace("-", "_")
        role_model_key = f"model_{role_key}"
        if role_model_key in effective_config:
            return effective_config[role_model_key]

    # CLI override
    cli_model = getattr(args, "model", DEFAULT_MODEL)
    if cli_model != DEFAULT_MODEL:
        return cli_model

    # Config global model
    if effective_config and "model" in effective_config:
        return effective_config["model"]

    # Priority 6: Auto-routing (late import, gated by feature flag)
    #
    # S1 (HIGH, follow-up to RT-02) — when auto_model_routing is enabled AND
    # auto_select_model returns None (every circuit OPEN, fail-closed signal),
    # we MUST NOT silently fall through to DEFAULT_ROLE_MODELS. Doing so
    # defeats RT-02's whole point: DEFAULT_ROLE_MODELS maps most roles
    # (developer, security-reviewer, planner, frontend-designer, debugger)
    # to "opus", so a tripped Haiku circuit would force the very cost
    # escalation RT-02 was designed to block.
    #
    # Instead, raise CircuitOpenError. The dispatch wrapper
    # (run_dev_test_loop_with_autoresearch) catches it and demotes the
    # task outcome to ``circuit_breaker_blocked`` — same observable
    # pattern as ``security_review_blocked``. Auto-routing OFF is the
    # legacy path and still falls through to DEFAULT_ROLE_MODELS.
    if effective_config and task and is_feature_enabled(effective_config, "auto_model_routing"):
        from equipa.routing import CircuitOpenError, auto_select_model
        routed_model = auto_select_model(task, effective_config)
        if routed_model:
            return routed_model
        raise CircuitOpenError(role=role, tier_attempted="haiku")

    return DEFAULT_ROLE_MODELS.get(role, DEFAULT_MODEL)


def _discover_roles() -> None:
    """Dynamically build ROLE_PROMPTS from .md files in the prompts directory.

    Scans PROMPTS_DIR for markdown files (excluding _common.md) and maps
    each filename stem to its full path.  Falls back to the hardcoded
    ROLE_PROMPTS dict if the prompts directory doesn't exist.
    """
    if not PROMPTS_DIR.exists():
        return  # keep hardcoded dict

    discovered = {}
    for md_file in sorted(PROMPTS_DIR.glob("*.md")):
        if md_file.name.startswith("_"):
            continue  # skip _common.md and similar
        role_name = md_file.stem  # e.g. "developer", "security-reviewer"
        discovered[role_name] = md_file

    if discovered:
        _equipa_constants.ROLE_PROMPTS = discovered


def _accumulate_cost(
    result: dict,
    label: str | None = None,
    output: list | None = None,
) -> float:
    """Extract cost from an agent result, estimating if actual cost is None.

    Returns the cost amount (float). Logs estimation when applicable.
    """
    from equipa.output import log

    if result.get("cost"):
        return result["cost"]
    num_turns = result.get("num_turns", 0)
    if num_turns:
        estimated = num_turns * COST_ESTIMATE_PER_TURN
        if label and output is not None:
            log(f"  {label} cost=None, estimating ${estimated:.2f} "
                f"({num_turns} turns * ${COST_ESTIMATE_PER_TURN})", output)
        return estimated
    return 0.0


def _apply_cost_totals(
    result: dict,
    total_cost: float,
    total_duration: float,
) -> dict:
    """Stamp accumulated cost and duration onto a result dict."""
    result["cost"] = total_cost
    result["duration"] = total_duration
    return result
