"""Phase 1 initiative tests: schema, plan file, parser, prompt section.

These tests use isolated tmp_path-scoped SQLite databases to avoid
touching the real TheForge DB. The intent is to lock in the Phase 1
contract: schema migration is idempotent, plan-file appends preserve
human edits and survive concurrent writers, the parser tolerates
malformed agent output, and tasks without an initiative route through
the existing dispatch flow unchanged.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Import under test
from equipa.initiative import (  # noqa: E402
    AGENT_INSTRUCTION_BLOCK,
    BEGIN_MARKER,
    END_MARKER,
    InitiativePlan,
    PROMPT_HEADER,
    parse_orchestrator_output,
    record_task_completion,
)
from scripts.migrate_initiative_schema import (  # noqa: E402
    apply_migration,
    describe_table,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """A fresh sqlite DB with the minimal base schema for our migration."""
    db_path = tmp_path / "theforge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            codename TEXT
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'todo'
        );
        """
    )
    conn.execute(
        "INSERT INTO projects (id, name, codename) VALUES (1, 'Equipa', 'equipa')"
    )
    conn.commit()
    return conn


@pytest.fixture
def migrated_db(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    apply_migration(db_conn)
    return db_conn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A mock 'target repo' directory with no pre-existing .equipa/."""
    return tmp_path / "target-repo"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_migration_creates_initiatives_table(db_conn):
    report = apply_migration(db_conn)
    assert report["initiatives_table_created"] is True
    assert report["initiative_id_column_added"] is True

    cols = {row[1] for row in describe_table(db_conn, "initiatives")}
    assert cols == {
        "id", "project_id", "name", "goal", "status",
        "created_at", "completed_at",
    }

    task_cols = {row[1] for row in describe_table(db_conn, "tasks")}
    assert "initiative_id" in task_cols


def test_migration_is_idempotent(db_conn):
    first = apply_migration(db_conn)
    second = apply_migration(db_conn)
    assert first["initiatives_table_created"] is True
    assert second["initiatives_table_created"] is False
    assert second["initiative_id_column_added"] is False
    # Schema unchanged on the second run.
    cols = {row[1] for row in describe_table(db_conn, "tasks")}
    assert "initiative_id" in cols


def test_existing_task_rows_survive_migration(migrated_db):
    cur = migrated_db.execute(
        "INSERT INTO tasks (project_id, title, description) VALUES (1, 'Pre-existing', 'x')"
    )
    task_id = cur.lastrowid
    migrated_db.commit()

    # Re-running the migration must not delete or break this row.
    apply_migration(migrated_db)
    row = migrated_db.execute(
        "SELECT title, initiative_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row[0] == "Pre-existing"
    assert row[1] is None  # Backward-compat: null is the default.


# ---------------------------------------------------------------------------
# Plan file lifecycle
# ---------------------------------------------------------------------------

def _seed_initiative(conn: sqlite3.Connection, name: str = "Demo") -> int:
    cur = conn.execute(
        "INSERT INTO initiatives (project_id, name, goal) "
        "VALUES (1, ?, 'Build something useful')",
        (name,),
    )
    conn.commit()
    return cur.lastrowid


def test_ensure_exists_creates_from_template(migrated_db, repo):
    iid = _seed_initiative(migrated_db, name="Initiative Concept")
    path = InitiativePlan.ensure_exists(repo, iid, migrated_db)
    assert path.exists()
    content = path.read_text()
    assert "# Initiative: Initiative Concept" in content
    assert "**Goal:** Build something useful" in content
    assert BEGIN_MARKER in content
    assert END_MARKER in content


def test_ensure_exists_is_idempotent(migrated_db, repo):
    iid = _seed_initiative(migrated_db)
    path1 = InitiativePlan.ensure_exists(repo, iid, migrated_db)
    # Operator-edited content above the BEGIN marker:
    path1.write_text(path1.read_text().replace(
        "<!-- Operator: write the initiative's original plan here. -->",
        "OPERATOR CONTENT — must survive.",
    ))
    path2 = InitiativePlan.ensure_exists(repo, iid, migrated_db)
    assert path1 == path2
    assert "OPERATOR CONTENT — must survive." in path2.read_text()


def test_append_subtask_preserves_human_section(migrated_db, repo):
    iid = _seed_initiative(migrated_db)
    InitiativePlan.ensure_exists(repo, iid, migrated_db)
    plan = InitiativePlan.load(repo, iid)
    # Edit the human section.
    text = plan.path.read_text().replace(
        "<!-- Operator: write the initiative's original plan here. -->",
        "OPERATOR CONTENT — must survive many appends.",
    )
    plan.path.write_text(text)
    plan = InitiativePlan.load(repo, iid)

    # Append 5 sub-task entries.
    for i in range(1, 6):
        plan.append_subtask(
            task_id=2000 + i,
            title=f"sub-task {i}",
            status="done",
            branch=f"forge-task-{2000 + i}",
            completed_at="2026-05-28 12:00",
            summary=f"summary {i}",
            decisions=[("decision %d" % i, "rationale %d" % i)],
        )

    final = plan.path.read_text()
    # Human-editable section survives unchanged across appends.
    assert final.count("OPERATOR CONTENT — must survive many appends.") == 1
    # All 5 entries land in the managed region in order.
    for i in range(1, 6):
        assert f"### #{2000 + i}: sub-task {i}" in final
    head, _, _ = final.partition(BEGIN_MARKER)
    assert "OPERATOR CONTENT" in head
    # Each entry sits between BEGIN and END.
    body = final.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0]
    for i in range(1, 6):
        assert f"### #{2000 + i}:" in body


def test_append_subtask_atomic_under_concurrent_writers(migrated_db, repo):
    iid = _seed_initiative(migrated_db)
    InitiativePlan.ensure_exists(repo, iid, migrated_db)

    errors: list[BaseException] = []

    def worker(task_id: int) -> None:
        try:
            plan = InitiativePlan.load(repo, iid)
            plan.append_subtask(
                task_id=task_id,
                title=f"concurrent {task_id}",
                status="done",
                branch=f"forge-task-{task_id}",
                completed_at="2026-05-28 12:00",
                summary="ok",
                decisions=[],
            )
        except BaseException as exc:  # noqa: BLE001 - test must report all
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(3000 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = (repo / ".equipa" / f"initiative-{iid}.md").read_text()
    for i in range(8):
        assert f"### #{3000 + i}: concurrent {3000 + i}" in final
    # Markers still intact — no double-marker corruption from race writes.
    assert final.count(BEGIN_MARKER) == 1
    assert final.count(END_MARKER) == 1


def test_to_prompt_context_returns_full_plan_with_header(migrated_db, repo):
    iid = _seed_initiative(migrated_db)
    InitiativePlan.ensure_exists(repo, iid, migrated_db)
    plan = InitiativePlan.load(repo, iid)
    rendered = plan.to_prompt_context()
    assert rendered.startswith(PROMPT_HEADER)
    assert "# Initiative:" in rendered
    assert BEGIN_MARKER in rendered


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_wellformed_block():
    raw = (
        "preamble text\n"
        "<!-- ORCHESTRATOR_OUTPUT -->\n"
        "SUMMARY: Did the thing successfully.\n"
        "DECISIONS:\n"
        "- Used Postgres | predictable transactional semantics\n"
        "- Skipped Redis cache | not on the hot path yet\n"
        "<!-- /ORCHESTRATOR_OUTPUT -->\n"
    )
    parsed = parse_orchestrator_output(raw)
    assert parsed is not None
    assert parsed.summary == "Did the thing successfully."
    assert parsed.decisions == [
        ("Used Postgres", "predictable transactional semantics"),
        ("Skipped Redis cache", "not on the hot path yet"),
    ]
    assert parsed.malformed is False


def test_parse_missing_block_returns_none():
    parsed = parse_orchestrator_output("no block here\nat all.\n")
    assert parsed is None


def test_parse_malformed_block_captures_best_effort():
    raw = (
        "<!-- ORCHESTRATOR_OUTPUT -->\n"
        "DECISIONS:\n"
        "- Decision without rationale separator\n"
        "<!-- /ORCHESTRATOR_OUTPUT -->\n"
    )
    parsed = parse_orchestrator_output(raw)
    assert parsed is not None
    assert parsed.malformed is True  # missing summary + missing pipe
    assert parsed.decisions == [("Decision without rationale separator", "")]


def test_parse_uses_last_block_when_multiple():
    raw = (
        "<!-- ORCHESTRATOR_OUTPUT -->\n"
        "SUMMARY: first draft\n"
        "DECISIONS:\n"
        "<!-- /ORCHESTRATOR_OUTPUT -->\n"
        "\nsome text\n\n"
        "<!-- ORCHESTRATOR_OUTPUT -->\n"
        "SUMMARY: revised final draft\n"
        "DECISIONS:\n"
        "- X | because Y\n"
        "<!-- /ORCHESTRATOR_OUTPUT -->\n"
    )
    parsed = parse_orchestrator_output(raw)
    assert parsed is not None
    assert parsed.summary == "revised final draft"
    assert parsed.decisions == [("X", "because Y")]


# ---------------------------------------------------------------------------
# Completion hook end-to-end (sub-task entry propagates into next prompt)
# ---------------------------------------------------------------------------

def test_record_task_completion_appends_and_next_dispatch_sees_it(
    migrated_db, repo,
):
    iid = _seed_initiative(migrated_db, name="E2E flow")
    InitiativePlan.ensure_exists(repo, iid, migrated_db)

    agent_output_task_a = (
        "<!-- ORCHESTRATOR_OUTPUT -->\n"
        "SUMMARY: Task A established the schema migration shape.\n"
        "DECISIONS:\n"
        "- Idempotent ALTER TABLE | re-runnable in CI\n"
        "<!-- /ORCHESTRATOR_OUTPUT -->\n"
    )
    ok = record_task_completion(
        repo_path=repo,
        initiative_id=iid,
        task_id=2484,
        title="Phase 1 schema + plan file format",
        status="done",
        branch="forge-task-2484",
        agent_output=agent_output_task_a,
    )
    assert ok is True

    # Now a SECOND task in the same initiative loads the plan and sees the
    # prior entry in its prompt context — this is the entire point of Phase 1.
    plan = InitiativePlan.load(repo, iid)
    next_prompt = plan.to_prompt_context()
    assert "### #2484: Phase 1 schema + plan file format" in next_prompt
    assert "Idempotent ALTER TABLE" in next_prompt


def test_record_task_completion_missing_block_uses_placeholder(
    migrated_db, repo,
):
    iid = _seed_initiative(migrated_db)
    InitiativePlan.ensure_exists(repo, iid, migrated_db)
    ok = record_task_completion(
        repo_path=repo,
        initiative_id=iid,
        task_id=9999,
        title="No-block task",
        status="done",
        branch="forge-task-9999",
        agent_output="agent forgot to emit the block",
    )
    assert ok is True
    body = (repo / ".equipa" / f"initiative-{iid}.md").read_text()
    assert "### #9999: No-block task" in body
    assert "(no orchestrator output block emitted by agent)" in body


# ---------------------------------------------------------------------------
# Backward compat: tasks without initiative_id are untouched
# ---------------------------------------------------------------------------

def test_null_initiative_id_skips_completion_hook(migrated_db, repo):
    """Phase 1 backward-compat guarantee: NULL initiative_id is a no-op."""
    from equipa.dispatch import _maybe_record_initiative_completion

    # No plan file exists for this task — and none should be created.
    task = {"id": 5555, "title": "No initiative", "initiative_id": None}
    _maybe_record_initiative_completion(
        task=task,
        project_dir=str(repo),
        outcome="tests_passed",
        result={"result_text": "irrelevant"},
        output=[],
    )
    assert not (repo / ".equipa").exists()


def test_agent_instruction_block_contains_protocol():
    # Anchors the public contract the agent prompt teaches.
    assert "<!-- ORCHESTRATOR_OUTPUT -->" in AGENT_INSTRUCTION_BLOCK
    assert "SUMMARY:" in AGENT_INSTRUCTION_BLOCK
    assert "DECISIONS:" in AGENT_INSTRUCTION_BLOCK
