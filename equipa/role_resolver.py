"""EQUIPA project-scoped role resolution.

Resolves a role name to its prompt file + config, checking a project's
private ``<project_dir>/.equipa/roles/`` overlay BEFORE the shared base
``prompts/`` set.

Isolation guarantee
-------------------
Two projects that each define a role of the same name (e.g. both have a
``cybersecurity-engineer``) are fully isolated: resolution is keyed on the
dispatching project's directory and holds NO process-global mutable role
state. This is what makes it safe under ``--auto-run``, which dispatches
multiple projects concurrently in one process — project A's role can never
shadow or leak into project B's.

Self-describing roles
---------------------
A role ``.md`` may declare its own config via optional frontmatter at the
top of the file (``model``, ``turns``, ``effort``, ``early_term_exempt``,
``skills``). Absent frontmatter, behaviour is identical to the legacy
global role set, so the change is fully backward-compatible — no base
``constants.py`` edits are required to add a project role.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import equipa.constants as _equipa_constants
from equipa.constants import EARLY_TERM_EXEMPT_ROLES, PROMPTS_DIR

# Relative location of a project's private role overlay, joined onto project_dir.
PROJECT_ROLES_SUBDIR = (".equipa", "roles")


@dataclass
class RoleConfig:
    """Resolved role: its prompt body (frontmatter stripped) + optional config."""

    name: str
    path: Path
    body: str
    model: str | None = None
    turns: int | None = None
    effort: str | None = None
    early_term_exempt: bool | None = None
    skills: list[str] = field(default_factory=list)
    is_project_role: bool = False


def _coerce(raw: str):
    """Coerce a frontmatter scalar/inline-list string to a Python value."""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw.strip("'\"")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split an optional leading ``--- ... ---`` frontmatter block from a role file.

    Returns ``(meta, body)``. When no well-formed frontmatter block is present,
    ``meta`` is empty and ``body`` is the original text unchanged.

    Intentionally minimal (no PyYAML dependency): supports scalar
    ``key: value`` lines and inline lists ``key: [a, b, c]``. A block without a
    closing ``---`` fence is treated as ordinary body text, not frontmatter.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            body = "".join(lines[i + 1:])
            return meta, body.lstrip("\n")
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, raw = stripped.partition(":")
        meta[key.strip()] = _coerce(raw.strip())
    # No closing fence — not frontmatter; treat the whole text as body.
    return {}, text


def _role_file(role: str, project_dir: str | None) -> tuple[Path | None, bool]:
    """Locate a role's ``.md``: project overlay wins, then the base set.

    Returns ``(path, is_project_role)``; ``(None, False)`` if no file exists.
    """
    if project_dir:
        candidate = Path(project_dir).joinpath(*PROJECT_ROLES_SUBDIR, f"{role}.md")
        if candidate.is_file():
            return candidate, True
    base = _equipa_constants.ROLE_PROMPTS.get(role)
    if base and Path(base).is_file():
        return Path(base), False
    direct = PROMPTS_DIR / f"{role}.md"
    if direct.is_file():
        return direct, False
    return None, False


def resolve_role(role: str, project_dir: str | None = None) -> RoleConfig | None:
    """Resolve ``role`` to a :class:`RoleConfig`, project overlay taking precedence.

    Returns ``None`` when neither a project nor a base file exists for the role.
    Stateless and side-effect-free: reads files fresh per call and shares no
    mutable global state, so concurrent resolution for different projects is
    safe and same-named roles in different projects never interfere.
    """
    path, is_project = _role_file(role, project_dir)
    if path is None:
        return None
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    turns = meta.get("turns")
    exempt = meta.get("early_term_exempt")
    return RoleConfig(
        name=role,
        path=path,
        body=body,
        model=meta.get("model"),
        turns=turns if isinstance(turns, int) else None,
        effort=meta.get("effort"),
        early_term_exempt=bool(exempt) if isinstance(exempt, bool) else None,
        skills=list(meta.get("skills") or []),
        is_project_role=is_project,
    )


def role_exists(role: str, project_dir: str | None = None) -> bool:
    """True if ``role`` resolves to a project-overlay or base prompt file."""
    return _role_file(role, project_dir)[0] is not None


def is_role_early_term_exempt(role: str, project_dir: str | None = None) -> bool:
    """Whether ``role`` is exempt from the no-file-change early-termination kill.

    A role file's frontmatter ``early_term_exempt`` (when set) wins; otherwise
    falls back to the base ``EARLY_TERM_EXEMPT_ROLES`` set, preserving legacy
    behaviour for built-in roles.
    """
    rc = resolve_role(role, project_dir)
    if rc is not None and rc.early_term_exempt is not None:
        return rc.early_term_exempt
    return role in EARLY_TERM_EXEMPT_ROLES
