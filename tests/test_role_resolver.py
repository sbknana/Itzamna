"""Tests for equipa.role_resolver — project-scoped role resolution + frontmatter.

The centerpiece is `test_two_projects_same_named_role_are_isolated`: two projects
that each define a role of the same name must resolve to their own file/config,
never each other's. That isolation is what makes per-project roles safe under
--auto-run's concurrent multi-project dispatch.
"""

import importlib

import pytest

from equipa import role_resolver as rr


def _write_role(project_dir, name, body, frontmatter=None):
    """Create <project_dir>/.equipa/roles/<name>.md, optional frontmatter dict."""
    roles_dir = project_dir / ".equipa" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    text = ""
    if frontmatter is not None:
        lines = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
        text += f"---\n{lines}\n---\n"
    text += body
    (roles_dir / f"{name}.md").write_text(text, encoding="utf-8")
    return roles_dir / f"{name}.md"


# --------------------------------------------------------------------------- #
# parse_frontmatter
# --------------------------------------------------------------------------- #

def test_parse_frontmatter_none():
    text = "## No frontmatter here\nbody line\n"
    meta, body = rr.parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_scalars_bools_ints_lists():
    text = (
        "---\n"
        "model: opus\n"
        "turns: 35\n"
        "early_term_exempt: true\n"
        "skills: [security, review]\n"
        "---\n"
        "## Prompt body\n"
    )
    meta, body = rr.parse_frontmatter(text)
    assert meta["model"] == "opus"
    assert meta["turns"] == 35 and isinstance(meta["turns"], int)
    assert meta["early_term_exempt"] is True
    assert meta["skills"] == ["security", "review"]
    assert body == "## Prompt body\n"


def test_parse_frontmatter_unclosed_is_treated_as_body():
    text = "---\nmodel: opus\n## but never closed\n"
    meta, body = rr.parse_frontmatter(text)
    assert meta == {}
    assert body == text


# --------------------------------------------------------------------------- #
# project-scoped resolution + the isolation guarantee
# --------------------------------------------------------------------------- #

def test_resolve_project_role(tmp_path):
    proj = tmp_path / "projA"
    _write_role(proj, "cybersecurity-engineer", "A-body", {"model": "opus", "turns": 40})
    rc = rr.resolve_role("cybersecurity-engineer", str(proj))
    assert rc is not None
    assert rc.is_project_role is True
    assert rc.body == "A-body"
    assert rc.model == "opus"
    assert rc.turns == 40


def test_two_projects_same_named_role_are_isolated(tmp_path):
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    _write_role(proj_a, "cybersecurity-engineer", "ALPHA prompt", {"model": "opus", "turns": 40})
    _write_role(proj_b, "cybersecurity-engineer", "BRAVO prompt", {"model": "sonnet", "turns": 15})

    rc_a = rr.resolve_role("cybersecurity-engineer", str(proj_a))
    rc_b = rr.resolve_role("cybersecurity-engineer", str(proj_b))

    # Same name, completely independent resolution.
    assert rc_a.body == "ALPHA prompt"
    assert rc_b.body == "BRAVO prompt"
    assert (rc_a.model, rc_a.turns) == ("opus", 40)
    assert (rc_b.model, rc_b.turns) == ("sonnet", 15)
    assert rc_a.path != rc_b.path


def test_unknown_role_returns_none(tmp_path):
    assert rr.resolve_role("does-not-exist", str(tmp_path)) is None
    assert rr.resolve_role("does-not-exist", None) is None


# --------------------------------------------------------------------------- #
# precedence vs base + early-term exemption
# --------------------------------------------------------------------------- #

@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    """Point the resolver's base PROMPTS_DIR at a tmp dir with controllable files."""
    base = tmp_path / "base_prompts"
    base.mkdir()
    monkeypatch.setattr(rr, "PROMPTS_DIR", base)
    # Ensure base lookup falls through to PROMPTS_DIR (not a stale ROLE_PROMPTS entry).
    monkeypatch.setattr(rr._equipa_constants, "ROLE_PROMPTS", {}, raising=False)
    return base


def test_project_role_overrides_base(tmp_path, base_dir):
    (base_dir / "planner.md").write_text("BASE planner", encoding="utf-8")
    proj = tmp_path / "projA"
    _write_role(proj, "planner", "PROJECT planner")

    rc_base = rr.resolve_role("planner", None)
    rc_proj = rr.resolve_role("planner", str(proj))
    assert rc_base.body == "BASE planner" and rc_base.is_project_role is False
    assert rc_proj.body == "PROJECT planner" and rc_proj.is_project_role is True


def test_early_term_exempt_frontmatter_true(tmp_path):
    proj = tmp_path / "projA"
    _write_role(proj, "ip-analyst", "body", {"early_term_exempt": "true"})
    assert rr.is_role_early_term_exempt("ip-analyst", str(proj)) is True


def test_early_term_exempt_frontmatter_false_overrides_base(tmp_path, monkeypatch):
    # Force "planner" into the base exempt set, then let a project role opt OUT.
    monkeypatch.setattr(rr, "EARLY_TERM_EXEMPT_ROLES", {"planner"})
    proj = tmp_path / "projA"
    _write_role(proj, "planner", "body", {"early_term_exempt": "false"})
    assert rr.is_role_early_term_exempt("planner", str(proj)) is False


def test_early_term_exempt_falls_back_to_base_set(tmp_path, monkeypatch, base_dir):
    # No file anywhere for "planner"; membership in the base set decides.
    monkeypatch.setattr(rr, "EARLY_TERM_EXEMPT_ROLES", {"planner"})
    assert rr.is_role_early_term_exempt("planner", None) is True
    assert rr.is_role_early_term_exempt("developer", None) is False


# --------------------------------------------------------------------------- #
# shipped opt-in example pack
# --------------------------------------------------------------------------- #

def test_example_role_pack_parses(tmp_path):
    """Every examples/roles/*.md parses cleanly with the expected frontmatter."""
    from pathlib import Path

    pack = Path(__file__).resolve().parent.parent / "examples" / "roles"
    files = [p for p in pack.glob("*.md") if p.name != "README.md"]
    assert files, "expected example role files under examples/roles/"
    for f in files:
        # Copy into a throwaway project overlay and resolve it through the real path.
        proj = tmp_path / f.stem
        _write_role(proj, f.stem, "")  # ensure dir exists
        (proj / ".equipa" / "roles" / f.name).write_text(
            f.read_text(encoding="utf-8"), encoding="utf-8"
        )
        rc = rr.resolve_role(f.stem, str(proj))
        assert rc is not None, f.name
        assert rc.model == "opus"
        assert rc.turns == 35
        assert rc.early_term_exempt is True
        # frontmatter must be stripped from the body
        assert not rc.body.lstrip().startswith("---")


# --------------------------------------------------------------------------- #
# available_roles — drives CLI dispatch validation for project-overlay roles
# (regression for: project roles rejected by argparse `choices=` before the
# project dir was known — they must survive the CLI layer and be dispatchable)
# --------------------------------------------------------------------------- #

def test_available_roles_includes_project_overlay(tmp_path, base_dir):
    (base_dir / "developer.md").write_text("base dev", encoding="utf-8")
    proj = tmp_path / "projA"
    _write_role(proj, "scilab-engineer", "body", {"model": "opus", "turns": 40})

    base_only = rr.available_roles(None)
    with_proj = rr.available_roles(str(proj))

    # Base role visible in both; the project overlay role only with project_dir.
    assert "developer" in base_only
    assert "scilab-engineer" not in base_only
    assert "scilab-engineer" in with_proj
    assert "developer" in with_proj


def test_project_overlay_role_passes_cli_validation(tmp_path):
    """A project-overlay role must satisfy role_exists (the CLI dispatch gate)
    for its project, while an unknown role is rejected — the helpful error then
    lists it via available_roles."""
    proj = tmp_path / "projA"
    _write_role(proj, "my-custom-role", "body")

    assert rr.role_exists("my-custom-role", str(proj)) is True   # survives CLI layer
    assert rr.role_exists("my-custom-role", None) is False       # not a base role
    assert rr.role_exists("nope", str(proj)) is False
    assert "my-custom-role" in rr.available_roles(str(proj))     # shown in error list
