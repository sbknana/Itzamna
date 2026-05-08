"""EQUIPA tasks module — task fetching, project context, and status queries.

Layer 2: Imports from equipa.constants and equipa.db.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from equipa.constants import (
    COMPLEXITY_MULTIPLIERS,
    PRIORITY_ORDER,
    PROJECT_DIRS,
    THEFORGE_DB,
)
from equipa.db import db_conn, get_db_connection

# Ordered weakest -> strongest so we can compare/escalate complexities.
_COMPLEXITY_RANK = {"simple": 0, "medium": 1, "complex": 2, "epic": 3}
_RANK_TO_COMPLEXITY = {v: k for k, v in _COMPLEXITY_RANK.items()}

# Patterns that indicate scope by site/file/call-site count.
# Each captures one or more digit groups; the largest match wins.
_SITE_COUNT_PATTERNS = (
    re.compile(
        r"\b(\d+)\s+(?:sites?|places?|files?|pairs?|call\s?sites?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bacross\s+(\d+)\s+files?\b", re.IGNORECASE),
    re.compile(
        r"\b(\d+)\+\s+(?:git\s+)?(?:calls?|call\s?sites?)\b",
        re.IGNORECASE,
    ),
)

# Wave-prefix nomenclature: P1..P5, M1..M9, D1..D9 followed by space.
_WAVE_PREFIX_RE = re.compile(r"^(?:P[1-5]|M[1-9]|D[1-9])\s")


def _max_site_count(description: str) -> int:
    """Return the largest digit-prefixed scope count found in the description."""
    if not description:
        return 0
    best = 0
    for pattern in _SITE_COUNT_PATTERNS:
        for match in pattern.finditer(description):
            try:
                value = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if value > best:
                best = value
    return best


def _site_count_floor(site_count: int) -> str | None:
    """Map an effective site count to a minimum complexity label.

    Returns None when the count is too small (<= 3) to justify escalation.
    """
    if site_count >= 51:
        return "epic"
    if site_count >= 16:
        return "complex"
    if site_count >= 4:
        return "medium"
    return None


def _escalate(current: str, floor: str | None) -> str:
    """Return the higher-ranked of `current` and `floor`."""
    if floor is None:
        return current
    return _RANK_TO_COMPLEXITY[max(_COMPLEXITY_RANK[current], _COMPLEXITY_RANK[floor])]


def _classify_by_length(desc_len: int) -> str:
    if desc_len < 100:
        return "simple"
    if desc_len < 400:
        return "medium"
    if desc_len < 800:
        return "complex"
    return "epic"


def fetch_task(task_id: int) -> dict | None:
    """Fetch a specific task by ID, including project info."""
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row else None


def fetch_next_todo(project_id: int) -> dict | None:
    """Find the highest-priority todo task for a project."""
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.project_id = ? AND t.status = 'todo'
            ORDER BY t.created_at ASC
            """,
            (project_id,),
        ).fetchall()

    if not rows:
        return None

    # Sort by priority text mapping (critical > high > medium > low)
    tasks = [dict(r) for r in rows]
    tasks.sort(
        key=lambda t: PRIORITY_ORDER.get(
            str(t.get("priority", "low")).lower(), 0
        ),
        reverse=True,
    )
    return tasks[0]


def fetch_project_context(project_id: int) -> dict:
    """Get recent project context: last session, open questions, recent decisions."""
    with db_conn() as conn:
        context: dict = {}

        # Last session notes
        row = conn.execute(
            """
            SELECT summary, next_steps, session_date
            FROM session_notes
            WHERE project_id = ?
            ORDER BY session_date DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        context["last_session"] = dict(row) if row else None

        # Open questions
        rows = conn.execute(
            """
            SELECT question, context
            FROM open_questions
            WHERE project_id = ? AND resolved = 0
            """,
            (project_id,),
        ).fetchall()
        context["open_questions"] = [dict(r) for r in rows]

        # Recent decisions
        rows = conn.execute(
            """
            SELECT decision, rationale, decided_at
            FROM decisions
            WHERE project_id = ?
            ORDER BY decided_at DESC
            LIMIT 5
            """,
            (project_id,),
        ).fetchall()
        context["recent_decisions"] = [dict(r) for r in rows]

        return context


def _get_task_status(task_id: int) -> str | None:
    """Quick read of task status string from DB."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return row["status"] if row else None


def fetch_project_info(project_id: int) -> dict | None:
    """Get project name and codename from project_id."""
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, name,
                   COALESCE(codename, LOWER(REPLACE(name, ' ', ''))) as codename
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        return dict(row) if row else None


def fetch_tasks_by_ids(task_ids: list[int]) -> list[dict]:
    """Fetch multiple tasks by their IDs.

    Returns a list of task dicts in the same order as the input IDs.
    Missing tasks are skipped.
    """
    if not task_ids:
        return []

    with db_conn() as conn:
        placeholders = ", ".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id IN ({placeholders})
            """,
            task_ids,
        ).fetchall()

        # Build a dict keyed by ID for ordering
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[tid] for tid in task_ids if tid in by_id]


def get_task_complexity(task: dict | None) -> str:
    """Resolve task complexity.

    Resolution order:
      1. Start from the explicit DB `complexity` field if present, else from a
         description-length heuristic.
      2. Escalate based on a SITE-COUNT scan of the description (digit-prefixed
         scope hints like "78 files", "across 11 files", "15+ git calls").
      3. Apply a Wave-prefix override: titles like "P1 ", "M3 ", "D2 " (Wave 3
         nomenclature) require a minimum complexity of "medium".

    Returns one of: 'simple', 'medium', 'complex', 'epic'.
    """
    task = task or {}
    description = task.get("description") or ""
    title = (task.get("title") or "").strip()

    explicit = (task.get("complexity") or "").strip().lower()
    if explicit in COMPLEXITY_MULTIPLIERS:
        complexity = explicit
    else:
        complexity = _classify_by_length(len(description))

    # Escalate based on observed scope count in the description. Applied
    # whether or not an explicit value is present — a "simple" record with
    # 78 sites in its description is not actually simple.
    site_count = _max_site_count(description)
    complexity = _escalate(complexity, _site_count_floor(site_count))

    # Wave-prefix override: precision-described refactor tasks must not run
    # under the "simple" 0.5x turn multiplier.
    if _WAVE_PREFIX_RE.match(title):
        complexity = _escalate(complexity, "medium")

    return complexity


def verify_task_updated(task_id: int) -> tuple[bool, str]:
    """Check if the agent updated the task status in TheForge."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

        if not row:
            return False, f"Task {task_id} not found in database"

        status = row["status"]
        if status == "done":
            return True, f"Task {task_id} marked as DONE"
        elif status == "blocked":
            return True, f"Task {task_id} marked as BLOCKED (agent reported blocker)"
        elif status == "in_progress":
            return False, f"Task {task_id} still IN_PROGRESS (agent may not have finished)"
        else:
            return False, f"Task {task_id} status is '{status}' (expected done or blocked)"
    finally:
        conn.close()


def resolve_project_dir(task: dict) -> str | None:
    """Find the project directory for a task's project.

    Resolution order:
    1. dispatch_config/forge_config project_dirs (exact match)
    2. TheForge DB local_path (source of truth)

    Uses exact match only — no partial/substring matching to prevent
    path traversal via crafted project codenames (security finding #3).
    """
    codename = task.get("project_codename", "").lower().strip()
    project_name = task.get("project_name", "").lower().strip()

    # 1. Check config file overrides first (exact match)
    if codename and codename in PROJECT_DIRS:
        return PROJECT_DIRS[codename]
    if project_name and project_name in PROJECT_DIRS:
        return PROJECT_DIRS[project_name]

    # 2. Fall back to TheForge DB local_path (source of truth)
    project_id = task.get("project_id")
    if project_id and THEFORGE_DB:
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT local_path FROM projects WHERE id = ? AND local_path IS NOT NULL",
                    (project_id,)
                ).fetchone()
            if row and row["local_path"]:
                db_path = row["local_path"].rstrip("/").rstrip("\\")
                # Translate Windows paths to Samba mount
                if db_path.startswith(("Z:\\AI_Stuff", "Z:/AI_Stuff")):
                    db_path = "/srv/forge-share/AI_Stuff" + db_path[len("Z:\\AI_Stuff"):].replace("\\", "/")
                if Path(db_path).exists():
                    return db_path
        except Exception as e:
            print(f"WARNING: DB lookup for project dir failed: {e}")

    return None
