"""Tests for equipa.scaffold (Task #1044 — ForgeScaffold auto-clone)."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from equipa import scaffold


@contextlib.contextmanager
def _mem_db_with_project(**columns):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT,
            codename TEXT,
            category TEXT,
            summary TEXT,
            target_market TEXT,
            revenue_model TEXT,
            local_path TEXT
        )
        """
    )
    base = {
        "id": 100,
        "name": "Demo",
        "codename": "demo",
        "category": None,
        "summary": None,
        "target_market": None,
        "revenue_model": None,
        "local_path": None,
    }
    base.update(columns)
    conn.execute(
        """
        INSERT INTO projects (id, name, codename, category, summary,
                              target_market, revenue_model, local_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            base["id"],
            base["name"],
            base["codename"],
            base["category"],
            base["summary"],
            base["target_market"],
            base["revenue_model"],
            base["local_path"],
        ),
    )
    conn.commit()

    @contextlib.contextmanager
    def factory():
        yield conn

    try:
        yield factory
    finally:
        conn.close()


def _make_scaffold(tmp_path: Path) -> Path:
    src = tmp_path / "ForgeScaffold"
    src.mkdir()
    (src / "CLAUDE.md").write_text("# ForgeScaffold\nGeneric scaffold docs.\n")
    (src / "package.json").write_text('{"name":"forgescaffold"}\n')
    (src / "src").mkdir()
    (src / "src" / "index.ts").write_text("export const x = 1;\n")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "junk.bin").write_text("BIG")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return src


def test_is_uninitialized_detects_missing_dir(tmp_path):
    assert scaffold.is_uninitialized(tmp_path / "does-not-exist") is True


def test_is_uninitialized_detects_empty_dir(tmp_path):
    assert scaffold.is_uninitialized(tmp_path) is True


def test_is_uninitialized_accepts_only_placeholder(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("stub\n")
    assert scaffold.is_uninitialized(tmp_path) is True


def test_is_uninitialized_rejects_real_content(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("stub\n")
    (tmp_path / "package.json").write_text("{}")
    assert scaffold.is_uninitialized(tmp_path) is False


def test_is_scaffold_project_via_category():
    with _mem_db_with_project(category="scaffold-based") as factory:
        assert scaffold.is_scaffold_project(100, db_conn_factory=factory) is True


def test_is_scaffold_project_via_summary():
    with _mem_db_with_project(summary="built on ForgeScaffold") as factory:
        assert scaffold.is_scaffold_project(100, db_conn_factory=factory) is True


def test_is_scaffold_project_negative():
    with _mem_db_with_project(category="standalone") as factory:
        assert scaffold.is_scaffold_project(100, db_conn_factory=factory) is False


def test_is_scaffold_project_env_override(monkeypatch):
    with _mem_db_with_project() as factory:
        monkeypatch.setenv("EQUIPA_SCAFFOLD_PROJECTS", "100")
        assert scaffold.is_scaffold_project(100, db_conn_factory=factory) is True


def test_resolve_scaffold_source_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EQUIPA_FORGESCAFFOLD_DIR", str(tmp_path))
    assert scaffold.resolve_scaffold_source() == tmp_path


def test_resolve_scaffold_source_config(monkeypatch, tmp_path):
    monkeypatch.delenv("EQUIPA_FORGESCAFFOLD_DIR", raising=False)
    result = scaffold.resolve_scaffold_source({"forgescaffold_dir": str(tmp_path)})
    assert result == tmp_path


def test_ensure_scaffold_clones_and_rewrites_claude_md(tmp_path, monkeypatch):
    src = _make_scaffold(tmp_path)
    monkeypatch.setenv("EQUIPA_FORGESCAFFOLD_DIR", str(src))

    dest = tmp_path / "DemoProject"
    dest.mkdir()
    (dest / "CLAUDE.md").write_text("placeholder\n")

    with _mem_db_with_project(category="scaffold") as factory:
        cloned = scaffold.ensure_scaffold(dest, 100, db_conn_factory=factory)

    assert cloned is True
    # Scaffold tree copied across, excluding heavy/excluded directories.
    assert (dest / "package.json").exists()
    assert (dest / "src" / "index.ts").exists()
    assert not (dest / "node_modules").exists()
    assert not (dest / ".git").exists()

    # CLAUDE.md replaced with project-specific content + Agent Quick Start.
    claude = (dest / "CLAUDE.md").read_text()
    assert "ForgeScaffold" not in claude.splitlines()[0]
    assert "Demo" in claude
    assert "Agent Quick Start" in claude
    assert "TheForge project_id" in claude

    breadcrumb = (dest / ".forge-scaffold.json").read_text()
    assert "100" in breadcrumb


def test_ensure_scaffold_skips_populated_dir(tmp_path, monkeypatch):
    src = _make_scaffold(tmp_path)
    monkeypatch.setenv("EQUIPA_FORGESCAFFOLD_DIR", str(src))
    dest = tmp_path / "Populated"
    dest.mkdir()
    (dest / "main.py").write_text("print('hi')\n")
    (dest / "CLAUDE.md").write_text("real docs\n")

    with _mem_db_with_project(category="scaffold") as factory:
        cloned = scaffold.ensure_scaffold(dest, 100, db_conn_factory=factory)

    assert cloned is False
    # CLAUDE.md untouched (no rewrite of populated projects).
    assert (dest / "CLAUDE.md").read_text() == "real docs\n"


def test_ensure_scaffold_skips_non_scaffold_project(tmp_path, monkeypatch):
    src = _make_scaffold(tmp_path)
    monkeypatch.setenv("EQUIPA_FORGESCAFFOLD_DIR", str(src))
    dest = tmp_path / "Empty"
    dest.mkdir()

    with _mem_db_with_project(category="standalone") as factory:
        cloned = scaffold.ensure_scaffold(dest, 100, db_conn_factory=factory)

    assert cloned is False
    assert list(dest.iterdir()) == []


def test_ensure_scaffold_raises_when_source_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("EQUIPA_FORGESCAFFOLD_DIR", str(tmp_path / "nope"))
    dest = tmp_path / "Empty"
    dest.mkdir()
    with _mem_db_with_project(category="scaffold") as factory:
        with pytest.raises(scaffold.ScaffoldCloneError):
            scaffold.ensure_scaffold(dest, 100, db_conn_factory=factory)


def test_build_project_claude_md_includes_quick_start():
    info = {
        "id": 7,
        "name": "AcmeApp",
        "codename": "acme",
        "category": "saas-scaffold",
        "summary": "An Acme app.",
        "target_market": "SMBs",
        "revenue_model": "Subscription",
    }
    text = scaffold.build_project_claude_md(7, project_info=info)
    assert text.startswith("# AcmeApp")
    assert "Agent Quick Start" in text
    assert "Where to put code" in text
    assert "TheForge project_id:** 7" in text
