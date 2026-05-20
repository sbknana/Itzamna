"""Architecture invariants enforcing the unified merge-gate (task #2451).

These tests use ``ast`` so they survive multi-line argv lists, tuple-style
calls, and string concatenation — the grep-based test the attempt-1 review
flagged (GATE-05) could not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EQUIPA_DIR = REPO_ROOT / "equipa"


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _count_calls(file_path: Path, function_name: str) -> int:
    tree = ast.parse(file_path.read_text())
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == function_name
    )


def _argv_strings_in_call(call: ast.Call) -> list[str]:
    """Return every string-literal argv element passed to ``call``.

    Walks positional list/tuple arguments (the conventional argv shape for
    ``subprocess``/``git_run_async``) and pulls out ``ast.Constant`` strings.
    Multi-line list literals are flattened via ``ast.walk`` so a definition
    spanning many lines is matched the same as a one-liner.
    """
    strings: list[str] = []
    for arg in call.args:
        if isinstance(arg, (ast.List, ast.Tuple)):
            for elt in ast.walk(arg):
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    strings.append(elt.value)
    return strings


def _calls_that_pass_merge_argv(file_path: Path) -> list[tuple[int, str]]:
    """Find every subprocess-style call whose argv contains "merge"."""
    tree = ast.parse(file_path.read_text())
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in {"git_run_async", "run", "Popen", "check_output", "check_call"}:
            continue
        for s in _argv_strings_in_call(node):
            if s == "merge":
                hits.append((node.lineno, name))
                break
    return hits


def test_merge_task_branch_called_only_from_dispatch():
    """``_merge_task_branch`` must live behind ``_gated_merge_task``.

    Phase B unification: cli.py must NOT call ``_merge_task_branch``
    directly — it goes through ``_gated_merge_task`` so the artifact
    re-check, defensive invariant, and audit-log scaffolding cannot be
    bypassed by a future caller.
    """
    dispatch_path = EQUIPA_DIR / "dispatch.py"
    cli_path = EQUIPA_DIR / "cli.py"
    loops_path = EQUIPA_DIR / "loops.py"

    assert _count_calls(dispatch_path, "_merge_task_branch") == 1, (
        "dispatch.py must contain exactly one call to _merge_task_branch "
        "(inside _gated_merge_task)"
    )
    assert _count_calls(cli_path, "_merge_task_branch") == 0, (
        "cli.py must not call _merge_task_branch directly — go through "
        "dispatch._gated_merge_task instead"
    )
    assert _count_calls(loops_path, "_merge_task_branch") == 0


def test_no_raw_git_merge_outside_helpers():
    """No file outside ``_merge_task_branch`` may issue ``git merge``.

    AST-walk replaces the brittle single-line regex from attempt-1: multi-line
    argv lists, tuple-style calls, and string concatenation are all covered.
    """
    forbidden_files = [
        EQUIPA_DIR / "loops.py",
        EQUIPA_DIR / "cli.py",
    ]
    for path in forbidden_files:
        if not path.exists():
            continue
        hits = _calls_that_pass_merge_argv(path)
        assert hits == [], (
            f"{path.name}: raw git-merge argv found at "
            f"{hits} — must route through dispatch._gated_merge_task"
        )


def test_dispatch_merge_argv_only_inside_merge_task_branch():
    """Sanity check: dispatch.py's only ``"merge"`` argv lives inside the
    documented helpers (``_merge_task_branch`` / ``_stash_uncommitted_in_worktree``).
    """
    dispatch_path = EQUIPA_DIR / "dispatch.py"
    tree = ast.parse(dispatch_path.read_text())

    allowed_funcs = {"_merge_task_branch"}
    bad: list[int] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if func.name in allowed_funcs:
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                if "merge" in _argv_strings_in_call(node):
                    bad.append(node.lineno)
    assert bad == [], (
        f"dispatch.py: 'merge' argv outside _merge_task_branch at lines {bad}"
    )
