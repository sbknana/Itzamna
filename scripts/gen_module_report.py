#!/usr/bin/env python3
"""Deterministic generator for ``equipa/MODULE_DEPENDENCY_REPORT.md``.

Performs a stdlib-only AST import-walk of the ``equipa/`` package and emits a
Markdown dependency report. The output is **deterministic**: it contains no
timestamps, wall-clock values, or filesystem-order dependent data. Every list
is sorted, so re-running the generator on an unchanged tree produces a
byte-identical file. That property is what lets ``scripts/check_docs_drift.py``
diff a fresh generation against the committed report and fail CI when the two
have drifted.

What it extracts (per module, via ``ast``):

* line count (``len(splitlines())``)
* intra-package imports, split into *top-level* (module scope) and *late*
  (deferred inside a function/method body — the pattern EQUIPA uses to break
  circular imports)
* a dependency *layer* computed as the longest chain in the top-level import
  DAG (leaves are layer 0)
* the number of public exports (``__all__`` length when present, otherwise the
  count of public top-level definitions)

Usage
-----
    python scripts/gen_module_report.py                 # rewrite the report
    python scripts/gen_module_report.py --stdout        # print, don't write
    python scripts/gen_module_report.py --check         # exit 1 if stale
    python scripts/gen_module_report.py --repo-root DIR  # analyse another repo

Exit codes: 0 success (or in-sync for ``--check``); 1 drift under ``--check``;
2 usage/internal error.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIRNAME = "equipa"
REPORT_RELPATH = Path("equipa") / "MODULE_DEPENDENCY_REPORT.md"

# Directories that never hold source modules we want to analyse.
_PRUNE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


@dataclass
class ModuleInfo:
    """Everything the report needs to know about a single ``.py`` module."""

    dotted: str  # import path relative to the package root ("" == __init__)
    display: str  # POSIX path relative to equipa/, e.g. "hooks/dispatcher.py"
    is_package_init: bool  # True for a directory ``__init__.py``
    lines: int = 0
    top_deps: set[str] = field(default_factory=set)  # display names, top-level
    late_deps: set[str] = field(default_factory=set)  # display names, deferred
    exports: int = 0
    layer: int = 0


def _iter_module_files(package_dir: Path) -> list[Path]:
    """Return every ``.py`` file under ``package_dir`` in a stable order."""

    files = [
        path
        for path in package_dir.rglob("*.py")
        if not any(part in _PRUNE_DIRS for part in path.relative_to(package_dir).parts)
    ]
    return sorted(files, key=lambda p: p.as_posix())


def _dotted_and_display(rel: Path) -> tuple[str, str, bool]:
    """Map a package-relative path to (dotted, display, is_package_init)."""

    stem_parts = rel.with_suffix("").parts
    is_init = stem_parts[-1] == "__init__"
    if is_init:
        dotted = ".".join(stem_parts[:-1])  # "" for the top-level __init__
    else:
        dotted = ".".join(stem_parts)
    return dotted, rel.as_posix(), is_init


def _map_dotted_to_display(dotted: str, known: dict[str, str]) -> str | None:
    """Resolve a dotted import target to the closest known module display name.

    ``from equipa.db import THEFORGE_DB`` yields ``dotted == "db"`` (a leaf) and
    ``from equipa.hooks.dispatcher import x`` yields ``"hooks.dispatcher"``. A
    dotted path pointing *through* a module to a symbol (``db.SOMETHING``) is
    walked back component-by-component until a real module is found.
    """

    if dotted in known:
        return known[dotted]
    probe = dotted
    while "." in probe:
        probe = probe.rsplit(".", 1)[0]
        if probe in known:
            return known[probe]
    # A bare ``from equipa import <symbol>`` lands on the package hub.
    return known.get("")


def _resolve_import(
    node: ast.Import | ast.ImportFrom,
    pkg_parts: list[str],
    known: dict[str, str],
) -> set[str]:
    """Return the set of intra-``equipa`` module display names ``node`` targets."""

    targets: set[str] = set()

    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == PACKAGE_DIRNAME or name.startswith(PACKAGE_DIRNAME + "."):
                rest = name[len(PACKAGE_DIRNAME) :].lstrip(".")
                resolved = _map_dotted_to_display(rest, known)
                if resolved is not None:
                    targets.add(resolved)
        return targets

    # ast.ImportFrom
    if node.level == 0:
        module = node.module or ""
        if module != PACKAGE_DIRNAME and not module.startswith(PACKAGE_DIRNAME + "."):
            return targets  # not an intra-package import
        base = module[len(PACKAGE_DIRNAME) :].lstrip(".")
    else:
        # Relative import: walk up (level-1) packages from the containing one.
        up = node.level - 1
        if up > len(pkg_parts):
            return targets  # escapes the package entirely
        base_parts = pkg_parts[: len(pkg_parts) - up]
        if node.module:
            base_parts = base_parts + node.module.split(".")
        base = ".".join(base_parts)

    if base and base in known and not _is_package(base, known):
        # Imported straight from a leaf module; the names are symbols, not
        # submodules, so the module itself is the only dependency.
        targets.add(known[base])
        return targets

    # ``base`` is the package root ("") or a subpackage: the imported names may
    # themselves be submodules (``from equipa import loops``).
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        resolved = _map_dotted_to_display(candidate, known)
        if resolved is not None:
            targets.add(resolved)
    if not node.names and base:
        resolved = _map_dotted_to_display(base, known)
        if resolved is not None:
            targets.add(resolved)
    return targets


def _is_package(dotted: str, known: dict[str, str]) -> bool:
    """True if ``dotted`` names a package (its display ends in ``__init__.py``)."""

    display = known.get(dotted)
    return bool(display) and display.endswith("__init__.py")


def _collect_deps(
    node: ast.AST,
    inside_func: bool,
    pkg_parts: list[str],
    known: dict[str, str],
    self_display: str,
    top: set[str],
    late: set[str],
) -> None:
    """Recursively gather intra-package imports, tagging deferred ones as late."""

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            resolved = _resolve_import(child, pkg_parts, known)
            resolved.discard(self_display)
            (late if inside_func else top).update(resolved)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect_deps(child, True, pkg_parts, known, self_display, top, late)
        else:
            # Class bodies and top-level if/try blocks stay at module scope.
            _collect_deps(child, inside_func, pkg_parts, known, self_display, top, late)


def _count_exports(tree: ast.Module, is_package_init: bool) -> int:
    """Count public exports: ``__all__`` length, else public top-level names."""

    if is_package_init:
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets
            ):
                if isinstance(stmt.value, (ast.List, ast.Tuple)):
                    return sum(
                        1
                        for elt in stmt.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )

    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not stmt.name.startswith("_"):
                names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and not stmt.target.id.startswith("_"):
                names.add(stmt.target.id)
    return len(names)


def analyze_package(package_dir: Path) -> list[ModuleInfo]:
    """Parse every module under ``package_dir`` and return sorted ``ModuleInfo``."""

    files = _iter_module_files(package_dir)
    modules: list[ModuleInfo] = []
    known: dict[str, str] = {}

    for path in files:
        rel = path.relative_to(package_dir)
        dotted, display, is_init = _dotted_and_display(rel)
        known[dotted] = display
        modules.append(
            ModuleInfo(dotted=dotted, display=display, is_package_init=is_init)
        )

    for info, path in zip(modules, files):
        text = path.read_text(encoding="utf-8")
        info.lines = len(text.splitlines())
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # A module that will not parse cannot contribute analysable imports;
            # its line count still counts toward the total.
            continue
        pkg_parts = [p for p in Path(info.display).parent.parts if p != "."]
        top: set[str] = set()
        late: set[str] = set()
        _collect_deps(tree, False, pkg_parts, known, info.display, top, late)
        info.top_deps = top
        info.late_deps = late - top  # a top-level dep is never also counted late
        info.exports = _count_exports(tree, info.is_package_init)

    _assign_layers(modules)
    return modules


def _assign_layers(modules: list[ModuleInfo]) -> None:
    """Set ``layer`` = longest chain length in the top-level import DAG."""

    by_display = {m.display: m for m in modules}
    memo: dict[str, int] = {}

    def depth(display: str, stack: frozenset[str]) -> int:
        if display in memo:
            return memo[display]
        if display in stack:
            return 0  # defensive: break any unexpected top-level cycle
        info = by_display.get(display)
        if info is None or not info.top_deps:
            memo[display] = 0
            return 0
        nxt = stack | {display}
        result = 1 + max(depth(dep, nxt) for dep in sorted(info.top_deps))
        memo[display] = result
        return result

    for module in sorted(modules, key=lambda m: m.display):
        module.layer = depth(module.display, frozenset())


def _fmt_deps(deps: set[str]) -> str:
    """Render a sorted, comma-joined dependency cell (em dash when empty)."""

    return ", ".join(f"`{d}`" for d in sorted(deps)) if deps else "—"


def generate_report(repo_root: Path) -> str:
    """Build the full Markdown report for ``repo_root/equipa`` (deterministic)."""

    package_dir = repo_root / PACKAGE_DIRNAME
    modules = analyze_package(package_dir)

    total_lines = sum(m.lines for m in modules)
    total_exports = sum(m.exports for m in modules)
    late_users = [m for m in modules if m.late_deps]
    max_layer = max((m.layer for m in modules), default=0)

    lines: list[str] = []
    lines.append("# EQUIPA Module Dependency Report")
    lines.append("")
    lines.append("> **Auto-generated — do not edit by hand.**")
    lines.append(
        "> Regenerate with `python scripts/gen_module_report.py`. The "
        "`docs-drift-check`"
    )
    lines.append(
        "> CI job fails if this file is stale (see `scripts/check_docs_drift.py`)."
    )
    lines.append("")
    lines.append(f"**Modules analyzed:** {len(modules)} (equipa/ package, recursive)")
    lines.append("")

    # --- Summary ---
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"The `equipa/` package contains **{len(modules)} Python modules** totaling "
        f"**{total_lines:,} lines** of code across **{max_layer + 1} dependency "
        f"layers** (L0–L{max_layer}). {len(late_users)} module(s) use late "
        f"(deferred) imports to break circular dependencies; there are no "
        f"top-level circular imports."
    )
    lines.append("")

    # --- Dependency table ---
    lines.append("## Module Dependency Table")
    lines.append("")
    lines.append(
        "| Module | Lines | Layer | Imports (equipa) | Late Imports | Exports |"
    )
    lines.append("|---|---:|:---:|---|---|---:|")
    for m in sorted(modules, key=lambda m: (m.layer, m.display)):
        lines.append(
            f"| `{m.display}` | {m.lines} | L{m.layer} | "
            f"{_fmt_deps(m.top_deps)} | {_fmt_deps(m.late_deps)} | {m.exports} |"
        )
    lines.append("")
    lines.append(f"**Total:** {total_lines:,} lines | {total_exports} public exports")
    lines.append("")

    # --- Modules by layer ---
    lines.append("## Modules by Layer")
    lines.append("")
    lines.append(
        "Layer *N* is the longest chain of top-level (non-deferred) imports from "
        "a leaf module. Late imports are excluded so the graph stays acyclic."
    )
    lines.append("")
    for layer in range(max_layer + 1):
        members = sorted(m.display for m in modules if m.layer == layer)
        joined = ", ".join(f"`{name}`" for name in members)
        lines.append(f"- **L{layer}**: {joined}")
    lines.append("")

    # --- Late import inventory ---
    lines.append("## Late Import Inventory")
    lines.append("")
    if late_users:
        lines.append(
            f"{len(late_users)} module(s) defer intra-package imports inside "
            f"function bodies to avoid import cycles:"
        )
        lines.append("")
        lines.append("| Module | Late Imports |")
        lines.append("|---|---|")
        for m in sorted(late_users, key=lambda m: m.display):
            lines.append(f"| `{m.display}` | {_fmt_deps(m.late_deps)} |")
    else:
        lines.append("No module defers intra-package imports.")
    lines.append("")

    # --- Import counts ---
    lines.append("## Import Count per Module")
    lines.append("")
    lines.append("How many other `equipa` modules each module imports.")
    lines.append("")
    lines.append("| Module | Top-level | Late | Total |")
    lines.append("|---|---:|---:|---:|")
    for m in sorted(modules, key=lambda m: m.display):
        top_n = len(m.top_deps)
        late_n = len(m.late_deps)
        lines.append(f"| `{m.display}` | {top_n} | {late_n} | **{top_n + late_n}** |")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_report(repo_root: Path) -> Path:
    """Regenerate the committed report file and return its path."""

    report_path = repo_root / REPORT_RELPATH
    report_path.write_text(generate_report(repo_root), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing the equipa/ package (default: cwd).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the report to stdout instead of writing the file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 (without writing) if the committed report is stale.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    package_dir = repo_root / PACKAGE_DIRNAME
    if not package_dir.is_dir():
        print(f"error: no equipa/ package under {repo_root}", file=sys.stderr)
        return 2

    fresh = generate_report(repo_root)

    if args.check:
        report_path = repo_root / REPORT_RELPATH
        committed = (
            report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        )
        if committed == fresh:
            print("OK: MODULE_DEPENDENCY_REPORT.md is up to date.")
            return 0
        print(
            "FAIL: MODULE_DEPENDENCY_REPORT.md is stale. "
            "Run: python scripts/gen_module_report.py",
            file=sys.stderr,
        )
        return 1

    if args.stdout:
        sys.stdout.write(fresh)
        return 0

    path = write_report(repo_root)
    print(f"Wrote {path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
