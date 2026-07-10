"""Unit tests for scripts/gen_module_report.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gen_module_report as gen  # noqa: E402  -- path tweak above


def _make_pkg(root: Path, files: dict[str, str]) -> Path:
    """Create a tiny fake ``equipa/`` package under ``root`` and return root."""

    for rel, body in files.items():
        path = root / "equipa" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_generation_is_deterministic() -> None:
    """Two generations over the real repo must be byte-identical."""

    first = gen.generate_report(REPO_ROOT)
    second = gen.generate_report(REPO_ROOT)
    assert first == second


def test_report_ends_with_single_newline() -> None:
    """Output must end with exactly one trailing newline (stable diffing)."""

    report = gen.generate_report(REPO_ROOT)
    assert report.endswith("\n")
    assert not report.endswith("\n\n")


def test_report_contains_no_timestamp_markers() -> None:
    """The report must not embed a dated ``Generated:`` line -- breaks drift."""

    report = gen.generate_report(REPO_ROOT).lower()
    # The stale report had a "**Generated:** 2026-03-24" line; the deterministic
    # generator must never emit one (or any wall-clock date field).
    assert "generated:" not in report
    assert "**generated:" not in report


def test_committed_report_is_in_sync() -> None:
    """The checked-in report must equal a fresh generation (guards staleness)."""

    committed = (REPO_ROOT / "equipa" / "MODULE_DEPENDENCY_REPORT.md").read_text(
        encoding="utf-8"
    )
    assert committed == gen.generate_report(REPO_ROOT)


def test_leaf_module_has_layer_zero(tmp_path: Path) -> None:
    """A module with no intra-package imports sits at layer 0."""

    root = _make_pkg(
        tmp_path,
        {
            "__init__.py": "from equipa.leaf import value\n",
            "leaf.py": "value = 1\n",
        },
    )
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    assert modules["leaf.py"].layer == 0
    assert modules["leaf.py"].top_deps == set()


def test_top_level_import_creates_dependency_and_layer(tmp_path: Path) -> None:
    """A top-level ``from equipa.leaf import`` yields a layer-1 dependency."""

    root = _make_pkg(
        tmp_path,
        {
            "__init__.py": "",
            "leaf.py": "value = 1\n",
            "user.py": "from equipa.leaf import value\n\n\ndef use() -> int:\n    return value\n",
        },
    )
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    assert modules["user.py"].top_deps == {"leaf.py"}
    assert modules["user.py"].layer == 1


def test_late_import_is_classified_as_deferred(tmp_path: Path) -> None:
    """An import inside a function body is late, not top-level."""

    body = (
        "def run() -> None:\n"
        "    from equipa.leaf import value\n"
        "    print(value)\n"
    )
    root = _make_pkg(
        tmp_path,
        {
            "__init__.py": "",
            "leaf.py": "value = 1\n",
            "deferred.py": body,
        },
    )
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    deferred = modules["deferred.py"]
    assert deferred.late_deps == {"leaf.py"}
    assert deferred.top_deps == set()
    # A purely-late importer stays at layer 0 (late edges are excluded).
    assert deferred.layer == 0


def test_top_level_dep_not_double_counted_as_late(tmp_path: Path) -> None:
    """A dep imported both at module scope and in a function counts once (top)."""

    body = (
        "from equipa.leaf import value\n"
        "\n"
        "def run() -> int:\n"
        "    from equipa.leaf import value as v2\n"
        "    return value + v2\n"
    )
    root = _make_pkg(
        tmp_path,
        {"__init__.py": "", "leaf.py": "value = 1\n", "both.py": body},
    )
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    both = modules["both.py"]
    assert both.top_deps == {"leaf.py"}
    assert both.late_deps == set()


def test_subpackage_module_is_analyzed(tmp_path: Path) -> None:
    """Files under a subpackage (hooks/) are walked and named by relpath."""

    root = _make_pkg(
        tmp_path,
        {
            "__init__.py": "",
            "constants.py": "X = 1\n",
            "hooks/__init__.py": "",
            "hooks/plug.py": "from equipa.constants import X\n",
        },
    )
    displays = {m.display for m in gen.analyze_package(root / "equipa")}
    assert "hooks/plug.py" in displays
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    assert modules["hooks/plug.py"].top_deps == {"constants.py"}


def test_exports_prefers_dunder_all(tmp_path: Path) -> None:
    """``__init__`` export count uses ``__all__`` length when present."""

    root = _make_pkg(
        tmp_path,
        {
            "__init__.py": '__all__ = ["a", "b", "c"]\n',
            "leaf.py": "def a() -> None: ...\n",
        },
    )
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    assert modules["__init__.py"].exports == 3


def test_exports_counts_public_defs_only(tmp_path: Path) -> None:
    """Underscore-prefixed defs are private and excluded from export counts."""

    body = "def public() -> None: ...\n\n\ndef _private() -> None: ...\n\nX = 1\n_Y = 2\n"
    root = _make_pkg(tmp_path, {"__init__.py": "", "mod.py": body})
    modules = {m.display: m for m in gen.analyze_package(root / "equipa")}
    # public + X == 2 public exports.
    assert modules["mod.py"].exports == 2


def test_check_flag_detects_stale_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--check`` returns 1 when the committed file is stale, 0 when current."""

    root = _make_pkg(tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"})
    (root / "equipa" / "MODULE_DEPENDENCY_REPORT.md").write_text(
        "stale\n", encoding="utf-8"
    )
    rc = gen.main(["--repo-root", str(root), "--check"])
    assert rc == 1

    gen.write_report(root)
    rc = gen.main(["--repo-root", str(root), "--check"])
    assert rc == 0


def test_missing_package_returns_usage_error(tmp_path: Path) -> None:
    """Pointing at a directory with no equipa/ package exits 2."""

    rc = gen.main(["--repo-root", str(tmp_path)])
    assert rc == 2
