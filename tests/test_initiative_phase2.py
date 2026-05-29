"""Tests for Initiative Phase 2 — orchestrator mode, pause markers, cost.

Covers the acceptance criteria for task #2489:

* Schema additions (total_cost/started_at/paused_at/pause_reason) are
  added idempotently by the migration.
* DAG -> wave layering for linear and diamond shapes.
* Cycle detection raises a clear, path-bearing error.
* Halt-on-failure: a blocked sub-task pauses the initiative and files an
  open_question.
* Halt-on-pause-marker: an operator marker pauses the initiative before
  the next wave.
* Idempotent resume: after a halt, re-running skips done sub-tasks.
* Cost accumulates across sub-tasks (even on halt).
* Empty initiative completes gracefully.

The wave dispatcher is exercised through an injected ``dispatch_fn`` seam
so no real agents are spawned.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.migrate_initiative_schema import apply_migration  # noqa: E402
from equipa import initiative_runner as ir  # noqa: E402
from equipa.initiative import BEGIN_MARKER, END_MARKER, HUMAN_EDIT_HINT  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BASE_SCHEMA = """
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
    status TEXT DEFAULT 'todo',
    blocked_by TEXT,
    role TEXT,
    initiative_id INTEGER
);
CREATE TABLE open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    context TEXT,
    priority TEXT,
    resolved INTEGER DEFAULT 0
);
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    cost_usd REAL
);
"""


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated DB with one project and the initiatives table."""
    db = sqlite3.connect(str(tmp_path / "forge.db"))
    db.row_factory = sqlite3.Row
    db.executescript(BASE_SCHEMA)
    # Phase 1 initiatives table (pre-Phase-2 shape) so the migration's
    # ADD COLUMN path is exercised end to end.
    db.executescript(
        """
        CREATE TABLE initiatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            CHECK (length(name) <= 200 AND length(goal) <= 8192)
        );
        """
    )
    db.execute("INSERT INTO projects (id, name, codename) VALUES (1, 'P', 'p')")
    db.commit()
    apply_migration(db)
    return db


def _new_initiative(conn: sqlite3.Connection, status: str = "active") -> int:
    cur = conn.execute(
        "INSERT INTO initiatives (project_id, name, goal, status) "
        "VALUES (1, 'I', 'goal', ?)",
        (status,),
    )
    conn.commit()
    return int(cur.lastrowid)


def _add_task(
    conn: sqlite3.Connection,
    initiative_id: int,
    task_id: int,
    blocked_by: str | None = None,
    status: str = "todo",
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, status, blocked_by, "
        "initiative_id) VALUES (?, 1, ?, ?, ?, ?)",
        (task_id, f"task {task_id}", status, blocked_by, initiative_id),
    )
    conn.commit()


_FIXED_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


def make_dispatch(outcomes: dict[int, str], costs: dict[int, float] | None = None):
    """Build a dispatch_fn that returns scripted outcomes/costs per task.

    Also flips the task's DB status the way a real dispatch would, so the
    idempotent-resume test sees ``done`` tasks excluded on the next run.
    """
    costs = costs or {}

    async def _dispatch(task_ids, args, _conn_holder=[]):  # noqa: B006
        results = []
        for tid in task_ids:
            outcome = outcomes.get(tid, "tests_passed")
            results.append(
                ir.WaveTaskResult(
                    task_id=tid, outcome=outcome, cost=costs.get(tid, 0.0)
                )
            )
        return results

    return _dispatch


# ---------------------------------------------------------------------------
# 1. Schema additions are idempotent
# ---------------------------------------------------------------------------

def test_phase2_columns_present_and_idempotent(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(initiatives)")}
    assert {"total_cost", "started_at", "paused_at", "pause_reason"} <= cols
    # Re-running adds nothing.
    report = apply_migration(conn)
    assert report["initiatives_phase2_columns_added"] == []
    # total_cost defaults to 0.0
    iid = _new_initiative(conn)
    row = ir.fetch_initiative(conn, iid)
    assert row["total_cost"] == 0.0


# ---------------------------------------------------------------------------
# 2. DAG -> waves
# ---------------------------------------------------------------------------

def test_linear_dag_three_waves() -> None:
    tasks = [
        {"id": 1, "blocked_by": None},
        {"id": 2, "blocked_by": "1"},
        {"id": 3, "blocked_by": "2"},
    ]
    assert ir.compute_waves(tasks) == [[1], [2], [3]]


def test_diamond_dag_three_waves() -> None:
    # A -> B + C -> D
    tasks = [
        {"id": 1, "blocked_by": None},
        {"id": 2, "blocked_by": "1"},
        {"id": 3, "blocked_by": "1"},
        {"id": 4, "blocked_by": "2,3"},
    ]
    assert ir.compute_waves(tasks) == [[1], [2, 3], [4]]


def test_external_dependency_ignored() -> None:
    # blocked_by references #99 which is NOT in the initiative -> treated
    # as satisfied so #1 still lands in wave 1.
    tasks = [{"id": 1, "blocked_by": "99"}, {"id": 2, "blocked_by": "1"}]
    assert ir.compute_waves(tasks) == [[1], [2]]


# ---------------------------------------------------------------------------
# 3. Cycle detection
# ---------------------------------------------------------------------------

def test_cycle_detection_raises() -> None:
    tasks = [{"id": 1, "blocked_by": "2"}, {"id": 2, "blocked_by": "1"}]
    with pytest.raises(ir.CycleError) as exc:
        ir.compute_waves(tasks)
    # Error message names the offending tasks.
    assert "#1" in str(exc.value) and "#2" in str(exc.value)
    assert 1 in exc.value.cycle and 2 in exc.value.cycle


# ---------------------------------------------------------------------------
# 4. Halt on sub-task failure
# ---------------------------------------------------------------------------

def test_halt_on_failure(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")
    _add_task(conn, iid, 3, blocked_by="2")

    # Task 2 is blocked.
    dispatch = make_dispatch({1: "tests_passed", 2: "blocked", 3: "tests_passed"})
    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW,
            log_fn=lambda *_: None,
        )
    )

    assert result.halted is True
    assert result.status == "paused"
    assert result.tasks_failed == [2]
    # Wave 3 (#3) never dispatched.
    assert result.waves_dispatched == 2

    row = ir.fetch_initiative(conn, iid)
    assert row["status"] == "paused"
    assert row["paused_at"] is not None
    assert "task #2" in (row["pause_reason"] or "")

    oq = conn.execute(
        "SELECT question FROM open_questions WHERE id = ?",
        (result.open_question_id,),
    ).fetchone()
    assert oq is not None
    assert "halted" in oq["question"].lower()


# ---------------------------------------------------------------------------
# 5. Halt on pause marker
# ---------------------------------------------------------------------------

def _write_plan(repo: Path, iid: int, *, pause_reason: str | None) -> None:
    plan = repo / ".equipa" / f"initiative-{iid}.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    human = "# Initiative\n\n## Sub-tasks\n\n"
    if pause_reason is not None:
        human += f'<!-- pause-for-review reason="{pause_reason}" -->\n\n'
    plan.write_text(
        human + f"{HUMAN_EDIT_HINT}\n{BEGIN_MARKER}\n\n{END_MARKER}\n"
    )


def test_halt_on_pause_marker(conn: sqlite3.Connection, tmp_path: Path) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")
    _write_plan(tmp_path, iid, pause_reason="wait for sign-off")

    dispatch = make_dispatch({1: "tests_passed", 2: "tests_passed"})
    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=str(tmp_path), now_fn=lambda: _FIXED_NOW,
            log_fn=lambda *_: None,
        )
    )

    # Marker is active before wave 1 -> halt immediately, nothing dispatched.
    assert result.halted is True
    assert result.status == "paused"
    assert result.waves_dispatched == 0
    row = ir.fetch_initiative(conn, iid)
    assert "wait for sign-off" in (row["pause_reason"] or "")


def test_pause_marker_parsing_ignores_managed_section() -> None:
    text = (
        "# I\n\n"
        '<!-- pause-for-review reason="human marker" -->\n'
        f"{BEGIN_MARKER}\n"
        '<!-- pause-for-review reason="should be ignored" -->\n'
        f"{END_MARKER}\n"
    )
    markers = ir.parse_pause_markers(text)
    assert len(markers) == 1
    assert markers[0].reason == "human marker"


def test_pause_marker_no_reason_gets_default() -> None:
    markers = ir.parse_pause_markers("# I\n<!-- pause-for-review -->\n")
    assert len(markers) == 1
    assert markers[0].reason == "operator-set pause marker"


# ---------------------------------------------------------------------------
# 6. Idempotent resume
# ---------------------------------------------------------------------------

def test_idempotent_resume(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")
    _add_task(conn, iid, 3, blocked_by="2")

    # First run: #2 blocked. Mark #1 done (as a real dispatch would).
    dispatch1 = make_dispatch({1: "tests_passed", 2: "blocked"})
    _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch1, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    conn.execute("UPDATE tasks SET status='done' WHERE id=1")
    conn.commit()

    # Operator fixes #2; resume. #1 is done -> excluded; only #2,#3 remain.
    seen_waves: list[list[int]] = []

    async def dispatch2(task_ids, args):
        seen_waves.append(list(task_ids))
        return [ir.WaveTaskResult(t, "tests_passed", 0.0) for t in task_ids]

    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch2, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )

    # #1 was skipped; resume dispatched #2 then #3.
    assert seen_waves == [[2], [3]]
    assert result.status == "done"
    row = ir.fetch_initiative(conn, iid)
    assert row["status"] == "done"
    assert row["completed_at"] is not None


def test_rerun_done_initiative_is_noop(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn, status="done")
    called = []

    async def dispatch(task_ids, args):
        called.append(task_ids)
        return []

    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    assert result.status == "done"
    assert called == []  # nothing dispatched


# ---------------------------------------------------------------------------
# 7. Cost accumulation
# ---------------------------------------------------------------------------

def test_cost_accumulates(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")
    _add_task(conn, iid, 3, blocked_by="2")

    dispatch = make_dispatch(
        {1: "tests_passed", 2: "tests_passed", 3: "tests_passed"},
        costs={1: 1.0, 2: 2.0, 3: 3.0},
    )
    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    assert result.status == "done"
    row = ir.fetch_initiative(conn, iid)
    assert row["total_cost"] == pytest.approx(6.0)


def test_cost_tracked_even_on_halt(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")

    dispatch = make_dispatch(
        {1: "tests_passed", 2: "blocked"}, costs={1: 1.5, 2: 0.5}
    )
    _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    row = ir.fetch_initiative(conn, iid)
    # Wave 1 (#1) cost recorded; wave 2 (#2) cost recorded even though it
    # failed and halted the initiative.
    assert row["total_cost"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 8. Empty initiative
# ---------------------------------------------------------------------------

def test_empty_initiative_completes(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    called = []

    async def dispatch(task_ids, args):
        called.append(task_ids)
        return []

    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn,
            repo_path=None, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    assert result.status == "done"
    assert called == []
    row = ir.fetch_initiative(conn, iid)
    assert row["status"] == "done"
    assert row["completed_at"] is not None


# ---------------------------------------------------------------------------
# Extra: max-waves cap halts safely
# ---------------------------------------------------------------------------

def test_max_waves_cap(conn: sqlite3.Connection) -> None:
    iid = _new_initiative(conn)
    _add_task(conn, iid, 1)
    _add_task(conn, iid, 2, blocked_by="1")
    _add_task(conn, iid, 3, blocked_by="2")

    dispatch = make_dispatch({1: "tests_passed", 2: "tests_passed", 3: "tests_passed"})
    result = _run(
        ir.run_initiative(
            iid, object(), dispatch_fn=dispatch, conn=conn, repo_path=None,
            max_waves=2, now_fn=lambda: _FIXED_NOW, log_fn=lambda *_: None,
        )
    )
    assert result.waves_dispatched == 2
    assert result.halted is True
    assert result.status == "paused"
    assert "max-waves" in (result.halt_reason or "")
