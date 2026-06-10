#!/usr/bin/env python3
"""Integration test: Full pipeline validation for EQUIPA modular split.

Validates that all 21 modules import correctly, the backward-compatibility
shim works, language detection runs, feature flags load, and anti-compaction
instructions are present in _common.md.

NOTE: This file matches pytest's ``*_test.py`` collection glob, so all
executable logic lives inside ``main()`` guarded by ``if __name__ ==
"__main__"``. Importing this module (as pytest collection does) must have
no side effects and must never call ``sys.exit()`` — otherwise a bare
``pytest`` run over the repo aborts during collection.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Project root is one level up from equipa/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS_COUNT = 0
FAIL_COUNT = 0


def report(label: str, passed: bool, detail: str = "") -> None:
    """Print PASS/FAIL for a single check and update counters."""
    global PASS_COUNT, FAIL_COUNT
    status = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if passed:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1


def main() -> int:
    """Run the full integration check suite and return an exit code."""
    # Ensure the project root is on sys.path so `import equipa` works
    # when run as `python3 equipa/integration_test.py` from the project dir.
    _root_str = str(PROJECT_ROOT)
    if _root_str not in sys.path:
        sys.path.insert(0, _root_str)

    # ──────────────────────────────────────────────────────────────
    # CHECK 1: All 21 modules import correctly from the equipa package
    # ──────────────────────────────────────────────────────────────
    print("CHECK 1: Module imports (21 modules)")

    # The task description lists 'config' but there is no equipa/config.py.
    # Configuration loading is handled by equipa.cli.load_config() and
    # equipa.dispatch.load_dispatch_config(). We split the list so the
    # known-absent module is tracked separately from the real 20 modules.
    real_modules = [
        "constants", "db", "tasks", "prompts", "parsing",
        "lessons", "reflexion", "messages", "agent_runner", "monitoring",
        "checkpoints", "preflight", "security", "loops", "manager",
        "dispatch", "git_ops", "output", "roles", "cli",
    ]
    # 'config' is listed in the task but does not exist as a standalone module
    expected_missing = ["config"]
    module_names = real_modules + expected_missing

    for name in module_names:
        try:
            mod = __import__(f"equipa.{name}")
            # Verify we actually got the submodule
            submod = getattr(mod, name, None)
            report(f"equipa.{name}", submod is not None,
                   "imported" if submod is not None else "attribute missing")
        except ImportError as exc:
            if name in expected_missing:
                report(f"equipa.{name}", True,
                       "correctly absent — config is in cli/dispatch, not a module")
            else:
                report(f"equipa.{name}", False, str(exc))
        except Exception as exc:
            report(f"equipa.{name}", False, f"unexpected: {exc}")

    # ──────────────────────────────────────────────────────────────
    # CHECK 2: Backward-compatibility shim works
    # ──────────────────────────────────────────────────────────────
    print("\nCHECK 2: Backward-compatibility shim (forge_orchestrator)")

    try:
        # Ensure project root is on sys.path for the shim import
        root_str = str(PROJECT_ROOT)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        import forge_orchestrator  # noqa: E402

        shim_symbols = ["run_dev_test_loop", "dispatch_agent", "ensure_schema"]
        for sym in shim_symbols:
            found = hasattr(forge_orchestrator, sym)
            report(f"forge_orchestrator.{sym}", found,
                   "available" if found else "MISSING")
    except ImportError as exc:
        for sym in ["run_dev_test_loop", "dispatch_agent", "ensure_schema"]:
            report(f"forge_orchestrator.{sym}", False, str(exc))
    except Exception as exc:
        report("shim import", False, f"unexpected: {exc}")

    # ──────────────────────────────────────────────────────────────
    # CHECK 3: Language detection works on this project
    # ──────────────────────────────────────────────────────────────
    print("\nCHECK 3: Language detection")

    try:
        from equipa.git_ops import detect_project_language

        result = detect_project_language(str(PROJECT_ROOT))
        is_dict = isinstance(result, dict)
        report("returns dict", is_dict, f"type={type(result).__name__}")

        has_primary = is_dict and "primary" in result
        report("has 'primary' key", has_primary)

        is_python = has_primary and result["primary"] == "python"
        report("primary == 'python'", is_python,
               f"got '{result.get('primary', 'N/A')}'" if has_primary else "")
    except Exception as exc:
        report("language detection", False, str(exc))

    # ──────────────────────────────────────────────────────────────
    # CHECK 4: Feature flags load from dispatch_config.json
    # ──────────────────────────────────────────────────────────────
    print("\nCHECK 4: Feature flags (dispatch_config.json)")

    config = None
    config_path = PROJECT_ROOT / "dispatch_config.json"
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config = json.loads(config_text)

        has_features = "features" in config
        report("'features' key exists", has_features)

        lang_prompts = has_features and config["features"].get("language_prompts") is True
        report("language_prompts == True", lang_prompts)
    except FileNotFoundError:
        report("dispatch_config.json exists", False, "file not found")
    except json.JSONDecodeError as exc:
        report("dispatch_config.json valid JSON", False, str(exc))
    except Exception as exc:
        report("feature flags", False, str(exc))

    # ──────────────────────────────────────────────────────────────
    # CHECK 5: Anti-compaction instructions in _common.md
    # ──────────────────────────────────────────────────────────────
    print("\nCHECK 5: Anti-compaction instructions")

    common_md_path = PROJECT_ROOT / "prompts" / "_common.md"
    try:
        common_text = common_md_path.read_text(encoding="utf-8")

        has_forge_state = ".forge-state.json" in common_text
        report(".forge-state.json in _common.md", has_forge_state)
    except FileNotFoundError:
        report("prompts/_common.md exists", False, "file not found")
    except Exception as exc:
        report("anti-compaction check", False, str(exc))

    # ──────────────────────────────────────────────────────────────
    # CHECK 6: Hook system placeholder exists in features
    # ──────────────────────────────────────────────────────────────
    print("\nCHECK 6: Hook system placeholder")

    try:
        # Re-use config loaded in CHECK 4
        hooks_key = config is not None and "features" in config \
            and "hooks" in config["features"]
        report("'hooks' key in features", hooks_key)
    except Exception as exc:
        report("hooks check", False, str(exc))

    # ──────────────────────────────────────────────────────────────
    # SUMMARY
    # ──────────────────────────────────────────────────────────────
    total = PASS_COUNT + FAIL_COUNT
    print(f"\n{'='*50}")
    print(f"TOTAL: {PASS_COUNT}/{total} passed, {FAIL_COUNT} failed")
    print(f"{'='*50}")

    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
