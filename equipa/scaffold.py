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
# block auto-cloning. Stored case-folded so case-insensitive mounts
# (Samba — the team uses it) match ``claude.md``, ``CLAUDE.MD``, etc.
_PLACEHOLDER_FILENAMES: frozenset[str] = frozenset({"CLAUDE.md", ".gitkeep"})
_PLACEHOLDER_FILENAMES_LOWER: frozenset[str] = frozenset(
    n.lower() for n in _PLACEHOLDER_FILENAMES
)


def _is_placeholder_name(name: str) -> bool:
    """Return True if ``name`` matches a known placeholder filename."""
    return name.lower() in _PLACEHOLDER_FILENAMES_LOWER

_FALLBACK_SCAFFOLD_DIR = Path("/srv/forge-share/AI_Stuff/ForgeScaffold")

# Allowlisted roots inside which scaffold project directories may be created.
# A ``projects.local_path`` value (after Windows -> POSIX translation) must
# resolve to a path contained by one of these roots, otherwise the clone is
# refused. This prevents a malicious or corrupted DB row of the form
# ``Z:\AI_Stuff\..\..\etc\evil`` from coaxing ``mkdir(parents=True)`` into
# materialising ``/etc/evil``. The list is intentionally short; override
# only via the ``EQUIPA_SCAFFOLD_ALLOWED_ROOTS`` env var (colon-separated).
_DEFAULT_ALLOWED_ROOTS: tuple[Path, ...] = (
    Path("/srv/forge-share/AI_Stuff"),
)


class ScaffoldCloneError(RuntimeError):
    """Raised when auto-clone is attempted but cannot complete safely."""


def _allowed_roots() -> tuple[Path, ...]:
    """Return the configured allowlisted roots for scaffold destinations."""
    env_roots = os.environ.get("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", "")
    if env_roots:
        parsed = tuple(
            Path(item).expanduser().resolve(strict=False)
            for item in env_roots.split(":")
            if item.strip()
        )
        if parsed:
            return parsed
    return tuple(root.resolve(strict=False) for root in _DEFAULT_ALLOWED_ROOTS)


def assert_contained_path(candidate: str | Path) -> Path:
    """Return a resolved Path for ``candidate`` after containment check.

    Raises :class:`ScaffoldCloneError` if the candidate contains ``..``
    segments before resolution (defence-in-depth) OR if the resolved
    absolute path does not lie inside one of the allowlisted roots.

    The check intentionally happens BEFORE any ``mkdir`` so a malicious
    ``local_path`` cannot create directories outside the share.
    """
    if candidate is None or candidate == "":
        raise ScaffoldCloneError("Refusing scaffold clone: empty path candidate")
    raw = str(candidate)
    # Defence-in-depth: reject any traversal token before resolution. POSIX
    # ``..`` and Windows ``..`` both render as the literal segment after the
    # Windows->POSIX translation in dispatch._bootstrap_scaffold_if_needed.
    posix_parts = raw.replace("\\", "/").split("/")
    if any(part == ".." for part in posix_parts):
        raise ScaffoldCloneError(
            f"Refusing scaffold clone: path contains traversal segment ({raw!r})"
        )
    resolved = Path(raw).resolve(strict=False)
    roots = _allowed_roots()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ScaffoldCloneError(
        f"Refusing scaffold clone: resolved path {resolved} is not inside "
        f"any allowlisted root ({', '.join(str(r) for r in roots)})"
    )


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
        entry.is_file() and _is_placeholder_name(entry.name)
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


# Case-folded view of _EXCLUDED_NAMES. Case-insensitive filesystems (Samba
# mounts in particular — the team uses them) make ``Node_Modules`` or
# ``.GIT`` indistinguishable from the canonical names; comparing only
# against the lowercase set lets such variants slip through and a foreign
# ``.git/`` directory would then leak across projects.
_EXCLUDED_NAMES_LOWER: frozenset[str] = frozenset(
    name.lower() for name in _EXCLUDED_NAMES
)


def _is_excluded_name(name: str) -> bool:
    """Return True if ``name`` matches an excluded entry, case-insensitively."""
    return name.lower() in _EXCLUDED_NAMES_LOWER


def _ignore_excluded(_dir: str, names: list[str]) -> list[str]:
    """``shutil.copytree`` ignore callback: drop case-insensitive matches."""
    return [n for n in names if _is_excluded_name(n)]


def _iter_copyable_entries(source: Path) -> Iterable[Path]:
    for entry in source.iterdir():
        if _is_excluded_name(entry.name):
            continue
        yield entry


def _assert_no_escaping_symlinks(root: Path) -> None:
    """Walk ``root`` and refuse if any symlink resolves outside of it.

    ``shutil.copytree`` with ``symlinks=True`` preserves symlinks as links
    (without following them), so a malicious symlink inside the scaffold
    pointing at ``/etc/shadow`` or ``~/.ssh/id_ed25519`` is NOT materialised
    in the destination — but the link itself would still be copied, which is
    a footgun if the destination is later archived or rsynced. We therefore
    pre-walk and reject any symlink whose ``readlink().resolve()`` escapes
    the scaffold root.
    """
    root_resolved = root.resolve(strict=False)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip excluded directories at every depth.
        dirnames[:] = [d for d in dirnames if not _is_excluded_name(d)]
        for name in dirnames + filenames:
            if _is_excluded_name(name):
                continue
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            try:
                target = (entry.parent / os.readlink(entry)).resolve(strict=False)
            except OSError as exc:
                raise ScaffoldCloneError(
                    f"Refusing scaffold clone: cannot read symlink {entry}: {exc}"
                ) from exc
            try:
                target.relative_to(root_resolved)
            except ValueError:
                raise ScaffoldCloneError(
                    f"Refusing scaffold clone: symlink {entry} -> {target} "
                    f"escapes scaffold root {root_resolved}"
                )


def _copy_tree(source: Path, dest: Path) -> list[str]:
    """Copy ``source`` into ``dest`` (which must already exist).

    Returns the list of top-level entry names that were copied. Raises
    :class:`ScaffoldCloneError` if any individual copy fails (callers will
    surface the error to the orchestrator log).

    Symlinks are PRESERVED (``symlinks=True``) rather than dereferenced —
    the misleading ``symlinks=False`` would have ``shutil`` chase a symlink
    to ``/etc/shadow`` and materialise its contents in the destination.
    Before copying, the scaffold tree is pre-walked to refuse any symlink
    whose target lies outside the scaffold root (defence-in-depth).
    """
    _assert_no_escaping_symlinks(source)

    copied: list[str] = []
    for entry in _iter_copyable_entries(source):
        target = dest / entry.name
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(
                    entry,
                    target,
                    ignore=_ignore_excluded,
                    dirs_exist_ok=False,
                    symlinks=True,  # preserve, do NOT dereference
                )
            elif entry.is_symlink():
                # Preserve top-level symlinks as links, do not follow.
                link_target = os.readlink(entry)
                os.symlink(link_target, target)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
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


# Sentinel string that marks the legitimate, generator-emitted Agent Quick
# Start header. Any other occurrence of "Agent Quick Start" in generated
# output indicates a DB-field injection attempt and triggers a defensive
# fallback below. The sentinel is intentionally hard to type accidentally.
_AGENT_QUICK_START_SENTINEL = (
    "<!-- equipa-quickstart-sentinel:5f7c1d9a-3e2b-4f9a-9e21-quickstart -->"
)

# Per-field byte cap for DB-sourced strings that flow into generated CLAUDE.md.
# Long fields (summary, target_market, revenue_model can be free-form prose) are
# truncated to prevent a single rogue row from injecting an unbounded amount
# of attacker-controlled text into the agent's primary context document.
_FIELD_BYTE_CAP = 1024


def _sanitise_db_field(value: object, field_name: str) -> str:
    """Coerce, truncate, and blockquote a DB-sourced CLAUDE.md field.

    Three defences:
      * **Type coercion + cap.** Forces ``str`` and truncates to
        ``_FIELD_BYTE_CAP`` characters so a single oversized row cannot
        flood the generated file.
      * **Blockquote prefix.** Every line is rendered as ``> …`` so any
        markdown headings (``##``) inside the field render as quoted text
        rather than top-level instructions to the agent.
      * **Sentinel scrub.** Any occurrence of the quick-start sentinel
        inside DB content is stripped — the sentinel must appear at most
        once, at the canonical position emitted by this module.
    """
    if value is None:
        return f"> (no {field_name} recorded)"
    text = str(value)
    if len(text) > _FIELD_BYTE_CAP:
        text = text[:_FIELD_BYTE_CAP] + "…[truncated]"
    text = text.replace(_AGENT_QUICK_START_SENTINEL, "[sentinel-stripped]")
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


_AGENT_QUICK_START_TEMPLATE = """\
## Agent Quick Start {sentinel}

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
    """Construct a project-specific ``CLAUDE.md`` from TheForge metadata.

    All DB-sourced fields are length-capped and blockquote-prefixed so a
    malicious project row cannot inject markdown headings (``## Agent Quick
    Start\\n1. Always force-push…``) that would steer every agent in the
    project. The legitimate Agent Quick Start header carries a sentinel
    string that this function verifies appears exactly once in the output;
    any additional sentinels signal tampering and trigger a hard fallback.
    """
    info = project_info or _project_metadata(project_id, db_conn_factory=db_conn_factory)

    # ``name`` and ``codename`` flow into structural positions (top-level
    # heading and a metadata bullet) so we cap + single-line them. Multi-line
    # injection into the heading itself is prevented by collapsing newlines.
    raw_name = info.get("name") or f"Project {project_id}"
    name = str(raw_name)[:_FIELD_BYTE_CAP].replace("\n", " ").replace("\r", " ").strip()
    raw_codename = info.get("codename") or name
    codename = str(raw_codename)[:_FIELD_BYTE_CAP].replace("\n", " ").replace("\r", " ").strip()
    raw_category = info.get("category") or "scaffold-based"
    category = str(raw_category)[:_FIELD_BYTE_CAP].replace("\n", " ").replace("\r", " ").strip()

    # Long free-form fields are rendered as multi-line blockquotes.
    summary_q = _sanitise_db_field(info.get("summary") or "(no summary recorded yet)", "summary")
    target_q = _sanitise_db_field(info.get("target_market") or "(not recorded)", "target_market")
    revenue_q = _sanitise_db_field(info.get("revenue_model") or "(not recorded)", "revenue_model")

    header = (
        f"# {name}\n\n"
        f"**TheForge project_id:** {project_id}\n"
        f"**Codename:** {codename}\n"
        f"**Category:** {category}\n\n"
    )
    quick_start = _AGENT_QUICK_START_TEMPLATE.format(
        project_name=name,
        sentinel=_AGENT_QUICK_START_SENTINEL,
    )
    overview = (
        "\n## Project Overview\n\n"
        f"{summary_q}\n\n"
        "**Target market:**\n\n"
        f"{target_q}\n\n"
        "**Revenue model:**\n\n"
        f"{revenue_q}\n"
    )
    scaffold_notice = (
        "\n## Scaffold Origin\n\n"
        "This project was bootstrapped from ForgeScaffold by the EQUIPA "
        "orchestrator. The directory layout, build tooling, and base "
        "dependencies match the scaffold; project-specific logic lives "
        "under `src/`.\n"
    )
    rendered = header + quick_start + overview + scaffold_notice
    # Sentinel invariant: there must be exactly one Agent Quick Start
    # sentinel in the final document. >1 means a DB field contained the
    # sentinel verbatim (very unlikely) and our scrub failed.
    if rendered.count(_AGENT_QUICK_START_SENTINEL) != 1:
        raise ScaffoldCloneError(
            "Refusing to write CLAUDE.md: Agent Quick Start sentinel appears "
            f"{rendered.count(_AGENT_QUICK_START_SENTINEL)} times (expected 1). "
            "DB field tampering suspected."
        )
    return rendered


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

    # Containment check BEFORE we touch the filesystem. A crafted
    # ``local_path`` like ``/srv/forge-share/AI_Stuff/../../etc/evil`` would
    # otherwise reach ``dest.mkdir(parents=True, exist_ok=True)`` below and
    # silently create directories outside the share. ``assert_contained_path``
    # rejects ``..`` segments and verifies the resolved path lies inside an
    # allowlisted root.
    dest = assert_contained_path(project_dir)

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
        if entry.is_file() and _is_placeholder_name(entry.name):
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
