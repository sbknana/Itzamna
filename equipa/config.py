"""EQUIPA configuration module — feature flags and dispatch config loading.

Layer-2 module: depends only on stdlib and equipa.constants. Holds the
read-only feature-flag and dispatch-config primitives that previously lived
in equipa.dispatch. dispatch.py imports loops.py at module load, so any
caller that needed is_feature_enabled / load_dispatch_config from inside
loops.py was forced to do an inline `from equipa.dispatch import ...` to
dodge the cycle. Hosting these primitives here breaks that cycle: both
dispatch.py and loops.py import from equipa.config, which imports
nothing from either of them.

Exports:
    DEFAULT_FEATURE_FLAGS
    DEFAULT_DISPATCH_CONFIG
    is_feature_enabled
    load_dispatch_config

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
from pathlib import Path

from equipa.constants import THEFORGE_DB


DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "language_prompts": True,
    "hooks": False,
    "mcp_health": False,
    "forgesmith_lessons": True,
    "forgesmith_episodes": True,
    "gepa_ab_testing": False,
    "security_review": True,
    # When True, agent_runner injects a Claude Code PreToolUse hook into the
    # spawned CLI subprocess (via a generated --settings file) so that
    # equipa.bash_security.check_bash_command runs BEFORE the Bash tool
    # executes — genuine pre-execution blocking, not the reactive
    # detect-and-terminate that the stream observer provides. DEFAULT FALSE:
    # the reactive stream check (agent_runner.py, defense-in-depth) is always
    # on; this flag only adds the true pre-execution gate on top. See task 2703.
    "bash_security_pretooluse": False,
    # When True (default), a missing SECURITY-REVIEW-NNNN.md artifact after
    # the security-review agent runs is treated as a gate-blocking failure
    # (fail-closed). Set to False only if your workflow accepts the
    # pre-2341 trade-off: a crashed/silenced reviewer silently downgrades
    # to "no findings" and the branch is allowed to merge. See task 2341.
    "security_review_block_on_missing_artifact": True,
    "quality_scoring": True,
    "anti_compaction_state": True,
    "vector_memory": False,
    "auto_model_routing": False,
    "knowledge_graph": False,
    "autoresearch": True,
    "rlm_decompose": False,
    "config_versioning": False,
    "session_persistence": False,
    "project_templates": False,
}

DEFAULT_DISPATCH_CONFIG: dict = {
    "max_concurrent": 8,
    "model": "sonnet",
    "max_turns": 25,
    "max_tasks_per_project": 3,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    "security_review": False,
    "features": dict(DEFAULT_FEATURE_FLAGS),
    "autoresearch_max_retries": 3,
}


def is_feature_enabled(dispatch_config: dict | None, feature_name: str) -> bool:
    """Check if a feature flag is enabled.

    Reads from dispatch_config["features"][feature_name]. Falls back to
    DEFAULT_FEATURE_FLAGS if the feature is not in the config.

    Returns True/False. Unknown features default to False.
    """
    if dispatch_config is None:
        return DEFAULT_FEATURE_FLAGS.get(feature_name, False)
    features = dispatch_config.get("features", {})
    return features.get(feature_name, DEFAULT_FEATURE_FLAGS.get(feature_name, False))


def is_security_review_enabled(args, dispatch_config: dict | None = None) -> bool:
    """Return True iff security review should run for this dispatch.

    Single source of truth for the security-review enablement precedence:
      1. CLI flag ``args.security_review`` wins when not None.
      2. Otherwise, fall back to ``dispatch_config["security_review"]`` (top-level).
      3. The ``features.security_review`` feature flag can disable even when
         the higher-precedence sources enable it (kill-switch semantics).

    This helper was extracted from the duplicate precedence chains that
    previously lived inline in ``equipa.cli:run_dispatch`` and
    ``equipa.dispatch:_is_security_review_enabled``. Bug 2321 was caused
    by exactly this kind of silent divergence between two code paths
    implementing the same policy; centralising the rule here prevents
    a recurrence.

    Args:
        args: argparse Namespace (or any object) with a ``security_review``
            attribute. Missing attribute is treated as None.
        dispatch_config: parsed dispatch config dict. If None, falls back
            to ``getattr(args, "dispatch_config", None)`` for callers
            (such as the dispatch.py call site) that thread the config
            through args instead of passing it explicitly.
    """
    dc = dispatch_config
    if dc is None:
        dc = getattr(args, "dispatch_config", None) or {}
    enabled = getattr(args, "security_review", None)
    if enabled is None:
        enabled = dc.get("security_review", False)
    if not is_feature_enabled(dc, "security_review"):
        enabled = False
    return bool(enabled)


def load_dispatch_config(filepath: str | Path | None) -> dict:
    """Load dispatch_config.json preferences.

    Returns a config dict with defaults for any missing keys.
    Falls back to defaults entirely if file not found.
    """
    config = dict(DEFAULT_DISPATCH_CONFIG)

    if filepath is None:
        # Default location: alongside the TheForge DB (where the orchestrator
        # script lives). Fall back to CWD-relative if that does not exist.
        filepath = Path(THEFORGE_DB).parent / "dispatch_config.json"
        if not filepath.exists():
            filepath = Path("dispatch_config.json")
    else:
        filepath = Path(filepath)

    if not filepath.exists():
        return config

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Could not load dispatch config '{filepath}': {e}")
        print("  Using defaults.")
        return config

    # Merge loaded values over defaults
    for key in DEFAULT_DISPATCH_CONFIG:
        if key in data:
            config[key] = data[key]

    # Deep-merge features: user's partial features dict is overlaid on defaults
    # so specifying e.g. {"features": {"hooks": true}} does not wipe other flags.
    if "features" in data and isinstance(data["features"], dict):
        merged_features = dict(DEFAULT_FEATURE_FLAGS)
        merged_features.update(data["features"])
        config["features"] = merged_features

    # Also merge any extra keys not in defaults (model_developer, model_epic, etc.)
    for key in data:
        if key not in config:
            config[key] = data[key]

    return config
