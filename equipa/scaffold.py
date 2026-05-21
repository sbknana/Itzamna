"""Auto-clone ForgeScaffold for new scaffold-based projects.

When a task targets a project that is built on ForgeScaffold but whose local
directory does not yet contain any code (empty, missing, or only carries a
placeholder ``CLAUDE.md``), the orchestrator copies the scaffold tree into
the project directory and rewrites ``CLAUDE.md`` to reflect the actual
project — not ForgeScaffold itself.

Public entry point: :func:`ensure_scaffold`. Both ``equipa.cli`` and
``equipa.dispatch`` call it just before they would otherwise abort with
"project directory does not exist" / "directory has no code".

Design notes
------------
* Detection is conservative: a directory is considered "uninitialized" only
  when it does not exist, is empty, or contains exclusively a single
  ``CLAUDE.md`` (the placeholder pattern the orchestrator drops in when it
  registers a new project). Any other content means the project has been
  seeded by hand and we must NOT touch it.
* The scaffold source is resolved from (in order): an explicit
  ``EQUIPA_FORGESCAFFOLD_DIR`` environment variable, the
  ``forgescaffold_dir`` key in ``forge_config.json``/``dispatch_config.json``,
  and finally a hard-coded fallback under ``/srv/forge-share/AI_Stuff``.
* Copy excludes ``node_modules``, ``.next``, build/dist artefacts, and any
  ``.git`` directory — the destination is meant to be a fresh repository
  rooted at the project directory.
* ``CLAUDE.md`` is always regenerated from project metadata (DB row,
  open-questions, recent decisions). The Agent Quick Start section is
  injected at the top so the first agent to touch the new project does not
  have to hunt for the "where do I put code" answer.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Files / directories the scaffold-clone NEVER copies. node_modules in
# particular routinely exceeds 500MB and is reproducible from
# package-lock.json; copying it across projects also poisons future
# `npm install` runs with stale binaries built against another working tree.
_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".next",
        "node_modules",
        "dist",
        "build",
        ".turbo",
        ".cache",
        ".forge-worktrees",
        ".forge-state.json",
        "tsconfig.tsbuildinfo",
    }
)

# Files that, when present in isolation, do NOT count as "real" project
# content. The orchestrator typically drops a stub CLAUDE.md into the
# project directory when registering a new project — that stub must not
# block auto-cloning.
_PLACEHOLDER_FILENAMES: frozenset[str] = frozenset({"CLAUDE.md", ".gitkeep"})

_FALLBACK_SCAFFOLD_DIR = Path("/srv/forge-share/AI_Stuff/ForgeScaffold")


class ScaffoldCloneError(RuntimeError):
    """Raised when auto-clone is attempted but cannot complete safely."""


def resolve_scaffold_source(config: dict | None = None) -> Path:
    """Resolve the path to the ForgeScaffold source tree.

    Resolution order:
      1. ``EQUIPA_FORGESCAFFOLD_DIR`` environment variable.
      2. ``forgescaffold_dir`` key in the active dispatch/forge config dict.
      3. Hard-coded fallback under ``/srv/forge-share/AI_Stuff/ForgeScaffold``.
    """
    env_path = os.environ.get("EQUIPA_FORGESCAFFOLD_DIR")
    if env_path:
        return Path(env_path).expanduser()
    if config:
        cfg_path = config.get("forgescaffold_dir") if isinstance(config, dict) else None
        if cfg_path:
            return Path(cfg_path).expanduser()
    return _FALLBACK_SCAFFOLD_DIR


def is_uninitialized(project_dir: str | Path) -> bool:
    """Return True if ``project_dir`` looks like a brand-new project shell.

    A directory is "uninitialized" when any of the following hold:
      * it does not exist
      * it exists but contains no entries
      * every entry it contains is a recognised placeholder filename
        (e.g. a stub ``CLAUDE.md`` and/or a ``.gitkeep``)

    Anything else — even a stray dotfile — means the directory has been
    populated by hand and the auto-clone must be a no-op.
    """
    path = Path(project_dir)
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    try:
        entries = list(path.iterdir())
    except PermissionError:
        return False
    if not entries:
        return True
    return all(
        entry.is_file() and entry.name in _PLACEHOLDER_FILENAMES
        for entry in entries
    )


def is_scaffold_project(project_id: int, db_conn_factory=None) -> bool:
    """Return True when the given project should be auto-scaffolded.

    A project is considered "scaffold-based" when its ``category`` column
    (or its ``summary``) mentions the scaffold marker, OR when the project
    has been explicitly registered via the ``EQUIPA_SCAFFOLD_PROJECTS``
    environment variable (comma-separated list of project IDs or codenames).
    """
    if project_id is None:
        return False

    env_list = os.environ.get("EQUIPA_SCAFFOLD_PROJECTS", "")
    if env_list:
        registered = {item.strip().lower() for item in env_list.split(",") if item.strip()}
        if str(project_id) in registered:
            return True

    try:
        if db_conn_factory is None:
            from equipa.db import db_conn as db_conn_factory  # type: ignore
        with db_conn_factory() as conn:
            row = conn.execute(
                """
                SELECT category, summary, codename
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("scaffold: project lookup failed for %s: %s", project_id, exc)
        return False

    if not row:
        return False

    haystack_parts: list[str] = []
    # Both sqlite Row and dict tolerate the same access pattern below.
    for key in ("category", "summary", "codename"):
        try:
            value = row[key]
        except (KeyError, IndexError):
            value = None
        if value:
            haystack_parts.append(str(value).lower())

    haystack = " ".join(haystack_parts)
    if not haystack:
        return False
    if env_list:
        # Allow registration by codename too.
        codename_value = None
        try:
            codename_value = row["codename"]
        except (KeyError, IndexError):
            codename_value = None
        if codename_value and codename_value.strip().lower() in registered:
            return True
    return "scaffold" in haystack or "forgescaffold" in haystack


def _iter_copyable_entries(source: Path) -> Iterable[Path]:
    for entry in source.iterdir():
        if entry.name in _EXCLUDED_NAMES:
            continue
        yield entry


def _copy_tree(source: Path, dest: Path) -> list[str]:
    """Copy ``source`` into ``dest`` (which must already exist).

    Returns the list of top-level entry names that were copied. Raises
    :class:`ScaffoldCloneError` if any individual copy fails (callers will
    surface the error to the orchestrator log).
    """
    copied: list[str] = []
    for entry in _iter_copyable_entries(source):
        target = dest / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(
                    entry,
                    target,
                    ignore=shutil.ignore_patterns(*_EXCLUDED_NAMES),
                    dirs_exist_ok=False,
                    symlinks=False,
                )
            else:
                shutil.copy2(entry, target)
        except FileExistsError:
            # A retry partially copied — re-raise so the caller can decide.
            raise ScaffoldCloneError(
                f"Refusing to overwrite existing entry: {target}"
            )
        except OSError as exc:
            raise ScaffoldCloneError(
                f"Failed to copy {entry} -> {target}: {exc}"
            ) from exc
        copied.append(entry.name)
    return copied


_AGENT_QUICK_START_TEMPLATE = """\
## Agent Quick Start

You are working in **{project_name}**, a freshly cloned ForgeScaffold-based
project. Read these four lines before you do anything else:

1. **Where to put code.** New application code goes under `src/`. New API
   handlers go under `src/server/`. Tests go alongside the file under test
   (`*.test.ts` / `*.test.tsx`). DO NOT recreate directory structure.
2. **What NOT to modify.** `prisma/schema.prisma`, `package.json`,
   `forge.config.ts`, `next.config.mjs`, `tsconfig.json`, and any file
   under `scripts/` are scaffold infrastructure. Only touch them if your
   task explicitly says so.
3. **What to start writing immediately.** If your task names a file, open
   it and write the code. If it does not, your first edit should still be
   inside `src/` — never in the repo root.
4. **Database, secrets, ports.** All of these come from the orchestrator
   environment. Never hard-code a connection string, an API key, or a
   port. If you need one, read it from `process.env`.

If something below contradicts the orchestrator's task description, the
task description wins — flag the conflict in your `DECISIONS` block and
proceed.
"""


def _project_metadata(project_id: int, db_conn_factory=None) -> dict:
    """Pull display metadata for the project (best-effort)."""
    if project_id is None:
        return {}
    try:
        if db_conn_factory is None:
            from equipa.db import db_conn as db_conn_factory  # type: ignore
        with db_conn_factory() as conn:
            row = conn.execute(
                """
                SELECT id, name, codename, category, summary,
                       target_market, revenue_model, local_path
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("scaffold: metadata lookup failed for %s: %s", project_id, exc)
        return {}
    if not row:
        return {}
    out: dict = {}
    for key in (
        "id",
        "name",
        "codename",
        "category",
        "summary",
        "target_market",
        "revenue_model",
        "local_path",
    ):
        try:
            out[key] = row[key]
        except (KeyError, IndexError):
            out[key] = None
    return out


def build_project_claude_md(
    project_id: int,
    project_info: dict | None = None,
    db_conn_factory=None,
) -> str:
    """Construct a project-specific ``CLAUDE.md`` from TheForge metadata."""
    info = project_info or _project_metadata(project_id, db_conn_factory=db_conn_factory)
    name = info.get("name") or f"Project {project_id}"
    codename = info.get("codename") or name
    summary = info.get("summary") or "(no summary recorded yet)"
    category = info.get("category") or "scaffold-based"
    target = info.get("target_market") or "(not recorded)"
    revenue = info.get("revenue_model") or "(not recorded)"

    header = (
        f"# {name}\n\n"
        f"**TheForge project_id:** {project_id}\n"
        f"**Codename:** {codename}\n"
        f"**Category:** {category}\n\n"
    )
    quick_start = _AGENT_QUICK_START_TEMPLATE.format(project_name=name)
    overview = (
        "\n## Project Overview\n\n"
        f"{summary}\n\n"
        f"- **Target market:** {target}\n"
        f"- **Revenue model:** {revenue}\n"
    )
    scaffold_notice = (
        "\n## Scaffold Origin\n\n"
        "This project was bootstrapped from ForgeScaffold by the EQUIPA "
        "orchestrator. The directory layout, build tooling, and base "
        "dependencies match the scaffold; project-specific logic lives "
        "under `src/`.\n"
    )
    return header + quick_start + overview + scaffold_notice


def ensure_scaffold(
    project_dir: str | Path,
    project_id: int | None,
    *,
    config: dict | None = None,
    project_info: dict | None = None,
    force: bool = False,
    db_conn_factory=None,
) -> bool:
    """Auto-clone ForgeScaffold into ``project_dir`` if needed.

    Returns True when a clone was performed, False when nothing needed
    doing (directory already populated, or project is not scaffold-based).

    Raises :class:`ScaffoldCloneError` only for genuine failures the
    orchestrator should surface (scaffold source missing, copy failed).
    A non-scaffold project, a populated directory, and a permission error
    on detection all return False.
    """
    if project_dir is None:
        return False

    dest = Path(project_dir)

    if not force:
        if not is_uninitialized(dest):
            return False
        if project_id is None or not is_scaffold_project(
            project_id, db_conn_factory=db_conn_factory
        ):
            return False

    source = resolve_scaffold_source(config)
    if not source.exists() or not source.is_dir():
        raise ScaffoldCloneError(
            f"ForgeScaffold source not found at {source}. Set "
            "EQUIPA_FORGESCAFFOLD_DIR or add forgescaffold_dir to the "
            "dispatch config."
        )

    dest.mkdir(parents=True, exist_ok=True)
    # Remove any placeholder files so copytree does not collide.
    for entry in list(dest.iterdir()):
        if entry.is_file() and entry.name in _PLACEHOLDER_FILENAMES:
            try:
                entry.unlink()
            except OSError as exc:
                raise ScaffoldCloneError(
                    f"Could not remove placeholder {entry}: {exc}"
                ) from exc

    copied = _copy_tree(source, dest)
    logger.info(
        "scaffold: cloned %d top-level entries from %s -> %s",
        len(copied),
        source,
        dest,
    )

    # Replace the scaffold's CLAUDE.md with one that names the actual
    # project (and includes the Agent Quick Start block at the top).
    claude_md = dest / "CLAUDE.md"
    project_claude = build_project_claude_md(
        project_id if project_id is not None else 0,
        project_info=project_info,
        db_conn_factory=db_conn_factory,
    )
    claude_md.write_text(project_claude, encoding="utf-8")

    # Drop a tiny breadcrumb so later runs can tell the directory was
    # auto-scaffolded (useful for debugging, not used for control flow).
    try:
        (dest / ".forge-scaffold.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "source": str(source),
                    "entries": copied,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # Breadcrumb is best-effort; failure here must not abort dispatch.
        logger.debug("scaffold: could not write .forge-scaffold.json", exc_info=True)

    return True
