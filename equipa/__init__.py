"""EQUIPA — multi-agent orchestrator for software-engineering tasks.

Public surface
==============
This package exposes a small set of public entry points. Anything not listed
in ``__all__`` (and in particular any name with a leading underscore) is an
internal implementation detail and is **not** part of the stable API. Internal
helpers move, rename, and disappear between versions; import them directly
from the relevant submodule (e.g. ``from equipa.parsing import _extract_section``)
if you must, and accept the breakage risk.

Submodules are available via attribute access in the usual way
(``import equipa; equipa.loops`` or ``from equipa import loops``).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from equipa.cli import async_main, main
from equipa.dispatch import (
    run_auto_dispatch,
    run_parallel_goals,
    run_parallel_tasks,
    run_project_dispatch,
    run_single_goal,
)
from equipa.loops import (
    run_dev_test_loop,
    run_quality_scoring,
    run_security_review,
)
from equipa.manager import run_manager_loop
from equipa.monitoring import LoopDetector
from equipa.prompts import PromptResult

__all__ = [
    # CLI entry points
    "main",
    "async_main",
    # Top-level loops
    "run_dev_test_loop",
    "run_security_review",
    "run_quality_scoring",
    "run_manager_loop",
    # Dispatch entry points
    "run_auto_dispatch",
    "run_project_dispatch",
    "run_single_goal",
    "run_parallel_goals",
    "run_parallel_tasks",
    # MCP server
    "run_server",
    # Public types
    "PromptResult",
    "LoopDetector",
]


def __getattr__(name: str):
    """Resolve ``run_server`` lazily (PEP 562).

    ``equipa.mcp_server`` is both a submodule and an entry point executed as
    ``python -m equipa.mcp_server``. Importing it eagerly here meant runpy loaded
    it twice — once as ``equipa.mcp_server`` while importing this package, then
    again as ``__main__`` — which emitted a RuntimeWarning on every server start
    and left two copies of the module's state (including the rate-limit buckets)
    in memory.

    Deferring the import removes the duplication while keeping ``run_server`` in
    ``__all__`` and importable as ``from equipa import run_server``, so the
    public surface is unchanged.
    """
    if name == "run_server":
        from equipa.mcp_server import run_server

        return run_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
