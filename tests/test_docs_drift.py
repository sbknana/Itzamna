"""Unit tests for scripts/check_docs_drift.py."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "drift_test_docs"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_docs_drift as drift  # noqa: E402  -- path tweak above
import gen_module_report as gen_report  # noqa: E402  -- drift coverage tests


@pytest.fixture
def clean_fixture(tmp_path: Path) -> Path:
    """Copy the static fixture into a tmp dir so tests can mutate it."""

    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


def test_clean_fixture_has_no_path_drift(clean_fixture: Path) -> None:
    """The pristine fixture must not produce any path drifts."""

    drifts = drift.check_paths(
        clean_fixture, drift._discover_doc_files(clean_fixture)
    )
    assert drifts == [], f"unexpected drifts: {[d.message for d in drifts]}"


def test_missing_path_is_detected(clean_fixture: Path) -> None:
    """Editing the README to reference a nonexistent file must fail."""

    readme = clean_fixture / "README.md"
    text = readme.read_text(encoding="utf-8")
    readme.write_text(
        text + "\n\nBroken link: [gone](src/missing_file.py)\n",
        encoding="utf-8",
    )

    drifts = drift.check_paths(
        clean_fixture, drift._discover_doc_files(clean_fixture)
    )
    assert any("src/missing_file.py" in d.message for d in drifts), (
        f"expected drift for src/missing_file.py, got: "
        f"{[d.message for d in drifts]}"
    )


def test_drift_ignore_block_is_honoured(clean_fixture: Path) -> None:
    """References inside <!-- drift-ignore --> blocks are skipped."""

    drifts = drift.check_paths(
        clean_fixture, drift._discover_doc_files(clean_fixture)
    )
    # The fixture intentionally contains src/does_not_exist.py and
    # src/another_missing.py inside the ignore block. They must not
    # appear in the drift list.
    bad = [d for d in drifts if "does_not_exist" in d.message or
           "another_missing" in d.message]
    assert bad == [], f"ignore block leaked: {[d.message for d in bad]}"


def test_sh_example_fence_is_ignored(clean_fixture: Path) -> None:
    """Paths inside ```sh-example fenced blocks must be ignored."""

    drifts = drift.check_paths(
        clean_fixture, drift._discover_doc_files(clean_fixture)
    )
    bad = [d for d in drifts if "imaginary_example" in d.message]
    assert bad == [], (
        f"sh-example fence leaked: {[d.message for d in bad]}"
    )


def test_module_count_within_tolerance_passes(clean_fixture: Path) -> None:
    """Fixture has 2 modules in equipa/, README claims 2 -> clean."""

    drifts = drift.check_module_count(clean_fixture)
    assert drifts == [], (
        f"module count check should be clean: "
        f"{[d.message for d in drifts]}"
    )


def test_module_count_outside_tolerance_fails(clean_fixture: Path) -> None:
    """If README claims 50 modules but only 2 exist, drift must fire."""

    readme = clean_fixture / "README.md"
    text = readme.read_text(encoding="utf-8")
    # Replace the existing module-count claim.
    text = text.replace("2 modules", "50 modules")
    readme.write_text(text, encoding="utf-8")

    drifts = drift.check_module_count(clean_fixture)
    assert any("50" in d.message and "modules" in d.message for d in drifts), (
        f"expected module-count drift, got: "
        f"{[d.message for d in drifts]}"
    )


def test_badge_count_drift_is_caught(clean_fixture: Path) -> None:
    """Badge ``tests-50`` with actual=200 (delta=150) must fire."""

    drifts = drift.check_test_badge(clean_fixture, actual_count=200)
    assert any("tests-50" in d.message for d in drifts), (
        f"expected tests-badge drift, got: {[d.message for d in drifts]}"
    )


def test_badge_count_within_tolerance_passes(clean_fixture: Path) -> None:
    """Badge ``tests-50`` with actual=60 (delta=10) is within tolerance."""

    drifts = drift.check_test_badge(clean_fixture, actual_count=60)
    assert drifts == [], (
        f"badge should be within tolerance: {[d.message for d in drifts]}"
    )


def test_badge_check_skipped_when_pytest_unavailable(
    clean_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If pytest collection returns None, the badge check is skipped."""

    monkeypatch.setattr(drift, "_collect_pytest_count", lambda _root: None)
    drifts = drift.check_test_badge(clean_fixture)
    assert drifts == []


def test_run_all_checks_clean_fixture(clean_fixture: Path) -> None:
    """End-to-end on the pristine fixture, skipping pytest."""

    drifts = drift.run_all_checks(clean_fixture, skip_pytest=True)
    assert drifts == [], (
        f"clean fixture should produce no drifts: "
        f"{[d.message for d in drifts]}"
    )


def test_main_exits_nonzero_on_drift(
    clean_fixture: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI must exit 1 when any drift is detected."""

    readme = clean_fixture / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n[broken](src/totally_missing.py)\n",
        encoding="utf-8",
    )

    rc = drift.main(
        [
            "--repo-root",
            str(clean_fixture),
            "--skip-pytest",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "src/totally_missing.py" in out


def test_main_warn_only_mode_returns_zero(
    clean_fixture: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--warn-only`` reports drifts but always exits 0."""

    readme = clean_fixture / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n[broken](src/also_missing.py)\n",
        encoding="utf-8",
    )

    rc = drift.main(
        [
            "--repo-root",
            str(clean_fixture),
            "--skip-pytest",
            "--warn-only",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "src/also_missing.py" in out
    assert "warn-only" in out


# --- regression tests for cross-platform / false-positive hardening ---


def test_repo_root_relative_ref_in_docs_resolves(clean_fixture: Path) -> None:
    """Repo-root-relative ref in docs/*.md (equipa/a.py) must resolve."""
    (clean_fixture / "docs" / "ref.md").write_text(
        "See `equipa/a.py` for details.", encoding="utf-8"
    )
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert not any("equipa/a.py" in d.message for d in drifts), [d.message for d in drifts]


def test_bare_module_basename_resolves(clean_fixture: Path) -> None:
    """A bare architecture-tree entry `a.py` resolves by basename."""
    (clean_fixture / "docs" / "tree.md").write_text(
        "```" + chr(10) + "equipa/" + chr(10) + "|-- a.py" + chr(10) + "+-- b.py" + chr(10) + "```" + chr(10),
        encoding="utf-8",
    )
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert drifts == [], [d.message for d in drifts]


def test_external_and_runtime_refs_skipped(clean_fixture: Path) -> None:
    """Home/external/runtime refs are not repo source and must never flag."""
    (clean_fixture / "docs" / "ext.md").write_text(
        "Configs: `~/.config/app.json`, `.cursor/mcp.json`, `.forge-state.json`, "
        "`CLAUDE.md`, `package.json`.", encoding="utf-8",
    )
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert drifts == [], [d.message for d in drifts]


def test_placeholder_glob_and_lone_extension_skipped(clean_fixture: Path) -> None:
    """Template placeholders, globs, and bare extensions are not file refs."""
    (clean_fixture / "docs" / "ph.md").write_text(
        "Paths: `<project_dir>/.equipa/roles/<role>.md`, `docs/*.md`, `.md`.",
        encoding="utf-8",
    )
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert drifts == [], [d.message for d in drifts]


def test_prod_only_file_skipped(clean_fixture: Path) -> None:
    """Files in .deploy-allowlist live only in prod; refs must not flag."""
    (clean_fixture / ".deploy-allowlist").write_text("dispatch_config.json", encoding="utf-8")
    (clean_fixture / "docs" / "cfg.md").write_text(
        "Edit `dispatch_config.json` to configure.", encoding="utf-8"
    )
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert not any("dispatch_config.json" in d.message for d in drifts), [d.message for d in drifts]


def test_genuine_repo_internal_missing_is_flagged(clean_fixture: Path) -> None:
    """A doc naming a non-existent repo source module still drifts (the point)."""
    (clean_fixture / "docs" / "ghost.md").write_text("See `equipa/ghost.py`.", encoding="utf-8")
    drifts = drift.check_paths(clean_fixture, drift._discover_doc_files(clean_fixture))
    assert any("equipa/ghost.py" in d.message for d in drifts), [d.message for d in drifts]


# --- module dependency report drift coverage (task 2708 part 2) ---
#
# These exercise check_docs_drift.check_module_report(), the new drift fence
# that fails CI when equipa/MODULE_DEPENDENCY_REPORT.md is stale relative to a
# fresh, deterministic generation. The generator's own --check flag is covered
# in test_gen_module_report.py; here we prove the *docs-drift checker* wires it
# in correctly (skips gracefully when inputs are absent, passes when in sync,
# and FAILS when the committed report is hand-edited).


def _make_report_repo(root: Path, modules: dict[str, str]) -> Path:
    """Build a minimal repo: an ``equipa/`` package, a copy of the generator,
    and a committed report produced by a fresh (in-sync) generation.

    The report is generated with the real ``gen_module_report`` module so the
    committed file starts byte-identical to what ``check_module_report`` will
    regenerate -- i.e. deliberately in-sync until a test mutates it.
    """

    for rel, body in modules.items():
        path = root / "equipa" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        SCRIPTS_DIR / "gen_module_report.py", scripts / "gen_module_report.py"
    )

    (root / "equipa" / "MODULE_DEPENDENCY_REPORT.md").write_text(
        gen_report.generate_report(root), encoding="utf-8"
    )
    return root


def test_module_report_in_sync_has_no_drift(tmp_path: Path) -> None:
    """A committed report matching a fresh generation yields no drift."""

    root = _make_report_repo(
        tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"}
    )
    assert drift.check_module_report(root) == []


def test_hand_edited_module_report_is_detected(tmp_path: Path) -> None:
    """Hand-editing the committed report must be flagged as stale drift.

    This is the task's headline acceptance criterion: check_docs_drift.py FAILS
    if you hand-edit equipa/MODULE_DEPENDENCY_REPORT.md.
    """

    root = _make_report_repo(
        tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"}
    )
    report = root / "equipa" / "MODULE_DEPENDENCY_REPORT.md"
    report.write_text(
        report.read_text(encoding="utf-8") + "\nHAND-EDITED LINE\n",
        encoding="utf-8",
    )

    drifts = drift.check_module_report(root)
    assert len(drifts) == 1, [d.message for d in drifts]
    assert "stale" in drifts[0].message
    assert "gen_module_report.py" in drifts[0].message


def test_hand_edited_report_reports_first_diff_line(tmp_path: Path) -> None:
    """The drift points at the first differing line (here: the header, line 1)."""

    root = _make_report_repo(
        tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"}
    )
    report = root / "equipa" / "MODULE_DEPENDENCY_REPORT.md"
    corrupted = report.read_text(encoding="utf-8").replace(
        "# EQUIPA Module Dependency Report", "# TAMPERED HEADER", 1
    )
    report.write_text(corrupted, encoding="utf-8")

    drifts = drift.check_module_report(root)
    assert len(drifts) == 1
    assert drifts[0].line == 1


def test_module_report_check_skipped_when_report_missing(tmp_path: Path) -> None:
    """No committed report (only the generator) -> check is silently skipped."""

    (tmp_path / "equipa").mkdir()
    (tmp_path / "equipa" / "__init__.py").write_text("", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(
        SCRIPTS_DIR / "gen_module_report.py", scripts / "gen_module_report.py"
    )

    assert drift.check_module_report(tmp_path) == []


def test_module_report_check_skipped_when_generator_missing(tmp_path: Path) -> None:
    """A committed report but no generator -> check is silently skipped."""

    root = _make_report_repo(
        tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"}
    )
    (root / "scripts" / "gen_module_report.py").unlink()

    assert drift.check_module_report(root) == []


def test_committed_real_report_is_in_sync() -> None:
    """The real repo's committed report must match a fresh generation.

    Mirrors test_gen_module_report.test_committed_report_is_in_sync but through
    the drift-checker's own entry point, proving the CI fence passes on a
    freshly-regenerated tree.
    """

    assert drift.check_module_report(REPO_ROOT) == []


def test_run_all_checks_surfaces_stale_report(tmp_path: Path) -> None:
    """End-to-end: a stale report is surfaced by run_all_checks()."""

    root = _make_report_repo(
        tmp_path, {"__init__.py": "", "leaf.py": "value = 1\n"}
    )
    (root / "equipa" / "MODULE_DEPENDENCY_REPORT.md").write_text(
        "totally stale contents\n", encoding="utf-8"
    )

    drifts = drift.run_all_checks(root, skip_pytest=True)
    assert any(
        "stale" in d.message and "report" in d.message.lower() for d in drifts
    ), [d.message for d in drifts]
