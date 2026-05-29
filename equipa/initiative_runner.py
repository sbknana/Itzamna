"""EQUIPA initiative orchestration runner (Phase 2).

Phase 1 built the *spine*: the ``initiatives`` table, per-initiative plan
files, prompt injection at dispatch, and ``ORCHESTRATOR_OUTPUT`` parsing.
Phase 2 makes the orchestrator actually *orchestrate*: given an initiative
id it walks the sub-task dependency DAG, dispatches the tasks in waves,
halts on the first failure or operator pause marker, tracks accumulated
cost, and supports idempotent resume after a halt.

Design (operator-locked 2026-05-28):

* **Failure handling** — HALT + pause on any sub-task failure or
  gate-block. The initiative is marked ``paused``, an open_question is
  filed, and the operator resumes by re-running ``--initiative <id>``.
* **Pause markers** — operator-only, hand-edited into the human-editable
  section of the plan file (above ``BEGIN ORCHESTRATOR-MANAGED``). A
  newly-seen ``<!-- pause-for-review reason="..." -->`` marker halts the
  run before the next wave.
* **Cost** — accumulated into ``initiatives.total_cost``; no enforced cap.
* **A paused initiative is the EXPECTED outcome of any failure**, not an
  error. The runner returns a structured result and the CLI exits 0 on a
  pause; only unexpected exceptions propagate non-zero.

Testability: the wave loop calls an injectable ``dispatch_fn`` seam, so
the DAG / wave / pause / cost / status-transition logic can be unit
tested without spawning real agents. The default seam dispatches via the
existing parallel-tasks machinery and reads each task's terminal status
and cost back from TheForge.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

from equipa.initiative import BEGIN_MARKER, InitiativePlan

logger = logging.getLogger(__name__)


# Task statuses the runner treats as "still needs work" when selecting the
# sub-tasks of an initiative. A task in any other status (e.g. ``done``,
# ``cancelled``) is considered already satisfied and is skipped on resume.
PENDING_STATUSES = ("todo", "blocked", "in_progress")

# Outcomes that mean a dispatched sub-task SUCCEEDED. Anything else (blocked,
# failed, error, security_review_blocked, cancelled, ...) halts the wave.
SUCCESS_OUTCOMES = ("tests_passed", "no_tests", "done")

# Operator-only pause marker, hand-written into the human-editable section
# of the plan file. ``reason`` is optional.
#   <!-- pause-for-review reason="wait for design sign-off" -->
#   <!-- pause-for-review -->
PAUSE_MARKER_RE = re.compile(
    r"<!--\s*pause-for-review"
    r"(?:\s+reason=\"(?P<reason>[^\"]*)\")?"
    r"\s*-->",
    re.IGNORECASE,
)


class InitiativeError(Exception):
    """Base class for unrecoverable initiative-runner errors."""


class CycleError(InitiativeError):
    """Raised when the sub-task dependency graph contains a cycle.

    ``cycle`` is the offending path as a list of task ids, e.g. ``[2, 3, 2]``.
    """

    def __init__(self, cycle: list[int]) -> None:
        self.cycle = cycle
        path = " -> ".join(f"#{tid}" for tid in cycle)
        super().__init__(f"Dependency cycle detected: {path}")


# ---------------------------------------------------------------------------
# Pure helpers — DAG, waves, pause markers
# ---------------------------------------------------------------------------

def parse_blocked_by(value: str | int | None) -> list[int]:
    """Parse the comma-separated ``tasks.blocked_by`` field into task ids.

    Tolerates ``None``, empty strings, surrounding whitespace, a bare int,
    and stray separators. Non-numeric tokens are ignored (defensive — the
    column is free-text TEXT). Order is preserved; duplicates are removed.
    """
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    seen: set[int] = set()
    out: list[int] = []
    for token in str(value).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            tid = int(token)
        except ValueError:
            logger.debug("Ignoring non-numeric blocked_by token %r", token)
            continue
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def build_dependency_graph(tasks: Sequence[dict]) -> dict[int, list[int]]:
    """Build ``task_id -> [dependency ids within this initiative]``.

    Only dependencies that are themselves part of ``tasks`` count toward
    wave ordering. A ``blocked_by`` reference to a task outside the
    initiative (e.g. an already-merged prerequisite from another
    initiative) is treated as already satisfied and dropped — otherwise a
    completed external dependency would wedge every wave.
    """
    in_set = {int(t["id"]) for t in tasks}
    graph: dict[int, list[int]] = {}
    for t in tasks:
        tid = int(t["id"])
        deps = [d for d in parse_blocked_by(t.get("blocked_by")) if d in in_set]
        graph[tid] = deps
    return graph


def detect_cycle(graph: dict[int, list[int]]) -> list[int] | None:
    """Return a cycle path (e.g. ``[2, 3, 2]``) if the graph has one, else None.

    Iterative depth-first search with a colour map (white/grey/black) so a
    large DAG cannot blow the Python recursion limit.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[int, int] = {node: WHITE for node in graph}

    for start in graph:
        if colour[start] != WHITE:
            continue
        # Stack of (node, iterator over its dependencies) plus a parallel
        # path list so we can reconstruct the cycle when we hit a grey node.
        stack: list[int] = [start]
        path: list[int] = [start]
        colour[start] = GREY
        iters: dict[int, int] = {start: 0}

        while stack:
            node = stack[-1]
            deps = graph.get(node, [])
            idx = iters[node]
            if idx < len(deps):
                iters[node] = idx + 1
                nxt = deps[idx]
                if colour.get(nxt, WHITE) == GREY:
                    # Found a back-edge: slice the path from nxt onward.
                    cut = path.index(nxt)
                    return path[cut:] + [nxt]
                if colour.get(nxt, WHITE) == WHITE:
                    colour[nxt] = GREY
                    stack.append(nxt)
                    path.append(nxt)
                    iters[nxt] = 0
            else:
                colour[node] = BLACK
                stack.pop()
                path.pop()
    return None


def compute_waves(tasks: Sequence[dict]) -> list[list[int]]:
    """Topologically layer the sub-tasks into dispatch waves.

    Wave 1 = tasks with no in-initiative dependencies; wave 2 = tasks whose
    deps are all in wave 1; and so on. Within a wave, ids are sorted
    ascending for deterministic, reproducible dispatch order.

    Raises :class:`CycleError` if the dependency graph contains a cycle.
    """
    graph = build_dependency_graph(tasks)

    cycle = detect_cycle(graph)
    if cycle is not None:
        raise CycleError(cycle)

    remaining = dict(graph)  # task_id -> deps still unsatisfied
    waves: list[list[int]] = []
    placed: set[int] = set()

    while remaining:
        ready = sorted(
            tid for tid, deps in remaining.items()
            if all(d in placed for d in deps)
        )
        if not ready:
            # Should be unreachable after the cycle check, but guard anyway
            # rather than spin forever.
            raise CycleError(sorted(remaining))
        waves.append(ready)
        placed.update(ready)
        for tid in ready:
            remaining.pop(tid)

    return waves


@dataclass(frozen=True)
class PauseMarker:
    """A parsed operator pause marker from the plan file."""

    reason: str


def parse_pause_markers(plan_text: str) -> list[PauseMarker]:
    """Extract operator pause markers from the plan file's human section.

    Only the region ABOVE the ``BEGIN ORCHESTRATOR-MANAGED`` marker is
    scanned — markers inside the orchestrator-managed block (which the
    orchestrator itself writes) are ignored. Markers are returned in
    document order. A marker with no ``reason`` attribute yields a default
    reason string so the pause is still self-describing.
    """
    if not plan_text:
        return []
    human_section = plan_text.split(BEGIN_MARKER, 1)[0]
    markers: list[PauseMarker] = []
    for m in PAUSE_MARKER_RE.finditer(human_section):
        reason = (m.group("reason") or "").strip()
        markers.append(PauseMarker(reason=reason or "operator-set pause marker"))
    return markers


# ---------------------------------------------------------------------------
# DB access — thin, all parameterised
# ---------------------------------------------------------------------------

def fetch_initiative(conn: sqlite3.Connection, initiative_id: int) -> dict | None:
    """Return the initiatives row as a dict, or None if it does not exist."""
    row = conn.execute(
        "SELECT id, project_id, name, goal, status, total_cost, "
        "started_at, paused_at, pause_reason, created_at, completed_at "
        "FROM initiatives WHERE id = ?",
        (initiative_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row) if not isinstance(row, dict) else row


def fetch_initiative_tasks(
    conn: sqlite3.Connection, initiative_id: int
) -> list[dict]:
    """Return all unfinished sub-tasks of an initiative.

    Selects tasks where ``initiative_id`` matches and status is one of
    :data:`PENDING_STATUSES`. ``done`` / ``cancelled`` tasks are excluded
    so a resume run naturally skips already-completed work (idempotency).
    """
    placeholders = ",".join("?" for _ in PENDING_STATUSES)
    rows = conn.execute(
        f"SELECT id, project_id, title, status, blocked_by, role, "
        f"initiative_id FROM tasks "
        f"WHERE initiative_id = ? AND status IN ({placeholders}) "
        f"ORDER BY id",
        (initiative_id, *PENDING_STATUSES),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_all_initiative_task_ids(
    conn: sqlite3.Connection, initiative_id: int
) -> list[int]:
    """Return every task id linked to the initiative, regardless of status."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE initiative_id = ? ORDER BY id",
        (initiative_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def mark_in_progress(
    conn: sqlite3.Connection, initiative_id: int, now: datetime
) -> None:
    """Transition the initiative to ``in_progress``.

    Sets ``started_at`` only if it has not already been set (first dispatch),
    and clears any prior pause state so a resume starts clean.
    """
    conn.execute(
        "UPDATE initiatives SET status = 'in_progress', "
        "started_at = COALESCE(started_at, ?), "
        "paused_at = NULL, pause_reason = NULL "
        "WHERE id = ?",
        (_fmt(now), initiative_id),
    )
    conn.commit()


def mark_paused(
    conn: sqlite3.Connection,
    initiative_id: int,
    reason: str,
    now: datetime,
) -> None:
    """Transition the initiative to ``paused`` with a reason and timestamp."""
    conn.execute(
        "UPDATE initiatives SET status = 'paused', paused_at = ?, "
        "pause_reason = ? WHERE id = ?",
        (_fmt(now), reason[:2000], initiative_id),
    )
    conn.commit()


def mark_done(
    conn: sqlite3.Connection, initiative_id: int, now: datetime
) -> None:
    """Transition the initiative to ``done`` and stamp ``completed_at``."""
    conn.execute(
        "UPDATE initiatives SET status = 'done', completed_at = ?, "
        "paused_at = NULL, pause_reason = NULL WHERE id = ?",
        (_fmt(now), initiative_id),
    )
    conn.commit()


def add_cost(
    conn: sqlite3.Connection, initiative_id: int, amount: float
) -> None:
    """Accumulate ``amount`` (USD) into ``initiatives.total_cost``.

    A non-positive or non-finite amount is a no-op so a missing/garbage
    cost reading from a dispatch cannot corrupt the running total.
    """
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return
    if not value or value != value or value < 0:  # NaN check via self-compare
        return
    conn.execute(
        "UPDATE initiatives SET total_cost = COALESCE(total_cost, 0.0) + ? "
        "WHERE id = ?",
        (value, initiative_id),
    )
    conn.commit()


def file_open_question(
    conn: sqlite3.Connection,
    project_id: int,
    question: str,
    context: str,
) -> int:
    """File an open_question and return its id. Best-effort, parameterised."""
    cursor = conn.execute(
        "INSERT INTO open_questions (project_id, question, context, priority) "
        "VALUES (?, ?, ?, 'high')",
        (project_id, question[:1000], context[:4000]),
    )
    conn.commit()
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Wave dispatch result + runner result
# ---------------------------------------------------------------------------

@dataclass
class WaveTaskResult:
    """Terminal outcome + cost of a single dispatched sub-task."""

    task_id: int
    outcome: str
    cost: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES


@dataclass
class InitiativeRunResult:
    """Structured result of an end-to-end (or halted) initiative run."""

    initiative_id: int
    status: str                       # final initiative status
    waves_planned: list[list[int]] = field(default_factory=list)
    waves_dispatched: int = 0
    tasks_completed: list[int] = field(default_factory=list)
    tasks_failed: list[int] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    total_cost_added: float = 0.0
    open_question_id: int | None = None


# Type of the injectable dispatch seam: given the wave's task ids and the
# parsed CLI args, dispatch them (in parallel) and return one result per
# task. May be sync or async; the runner awaits the result if needed.
DispatchFn = Callable[
    [list[int], object],
    "list[WaveTaskResult] | Awaitable[list[WaveTaskResult]]",
]


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

async def run_initiative(
    initiative_id: int,
    args: object,
    *,
    dispatch_fn: DispatchFn | None = None,
    conn: sqlite3.Connection | None = None,
    repo_path: str | Path | None = None,
    max_waves: int | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
    log_fn: Callable[[str], None] = print,
) -> InitiativeRunResult:
    """Run an initiative end-to-end, halting on failure or pause marker.

    Args:
        initiative_id: the initiative to run.
        args: parsed CLI args, forwarded verbatim to ``dispatch_fn``.
        dispatch_fn: seam that dispatches a wave and returns per-task
            results. Defaults to :func:`_default_dispatch_wave` (real
            parallel dispatch). Tests inject a simulator here.
        conn: an open read-write DB connection. Defaults to a fresh
            ``get_db_connection(write=True)``; caller owns closing it only
            if they pass it in.
        repo_path: target repo root (for plan-file pause-marker reads).
            Defaults to the initiative's resolved project directory.
        max_waves: optional safety cap on the number of waves dispatched.
        now_fn / log_fn: injectable clock and logger for testing.

    Returns:
        :class:`InitiativeRunResult` describing the final state. A pause is
        a normal (non-exception) outcome with ``halted=True``.

    Raises:
        :class:`CycleError` if the dependency graph contains a cycle.
        :class:`InitiativeError` if the initiative id is unknown.
    """
    owns_conn = conn is None
    if conn is None:
        from equipa.db import get_db_connection
        conn = get_db_connection(write=True)

    if dispatch_fn is None:
        dispatch_fn = _default_dispatch_wave

    try:
        return await _run_initiative_inner(
            initiative_id=initiative_id,
            args=args,
            dispatch_fn=dispatch_fn,
            conn=conn,
            repo_path=repo_path,
            max_waves=max_waves,
            now_fn=now_fn,
            log_fn=log_fn,
        )
    finally:
        if owns_conn:
            conn.close()


async def _run_initiative_inner(
    *,
    initiative_id: int,
    args: object,
    dispatch_fn: DispatchFn,
    conn: sqlite3.Connection,
    repo_path: str | Path | None,
    max_waves: int | None,
    now_fn: Callable[[], datetime],
    log_fn: Callable[[str], None],
) -> InitiativeRunResult:
    initiative = fetch_initiative(conn, initiative_id)
    if initiative is None:
        raise InitiativeError(f"No initiative with id={initiative_id}")

    project_id = int(initiative["project_id"])
    result = InitiativeRunResult(initiative_id=initiative_id, status=initiative["status"])

    # Re-running a completed initiative is an informational no-op.
    if initiative["status"] == "done":
        log_fn(f"[Initiative #{initiative_id}] already 'done' — nothing to do.")
        return result

    tasks = fetch_initiative_tasks(conn, initiative_id)

    # Empty initiative (no unfinished sub-tasks) → graceful completion.
    if not tasks:
        now = now_fn()
        mark_done(conn, initiative_id, now)
        result.status = "done"
        log_fn(
            f"[Initiative #{initiative_id}] no pending sub-tasks — "
            f"marked 'done'."
        )
        return result

    # Compute the wave plan up front (raises CycleError on a bad DAG).
    waves = compute_waves(tasks)
    result.waves_planned = waves
    log_fn(
        f"[Initiative #{initiative_id}] {len(tasks)} pending sub-task(s) "
        f"in {len(waves)} wave(s): "
        + " | ".join(
            "wave %d: %s" % (i + 1, ", ".join(f"#{t}" for t in w))
            for i, w in enumerate(waves)
        )
    )

    # Resolve the repo path (for pause-marker reads). Best-effort — a
    # missing plan file simply means "no pause markers".
    resolved_repo = _resolve_repo_path(repo_path, tasks, conn)

    # Transition active -> in_progress (idempotent; sets started_at once).
    now = now_fn()
    mark_in_progress(conn, initiative_id, now)
    result.status = "in_progress"

    # Pause markers already seen in prior runs do not re-trigger. On a fresh
    # run we record the current marker set as a baseline so only NEW markers
    # halt — but on a resume after a marker-pause the operator is expected to
    # have removed/edited the marker, so any marker still present is "new".
    seen_markers: set[str] = set()

    for wave_index, wave in enumerate(waves):
        if max_waves is not None and wave_index >= max_waves:
            reason = (
                f"--max-waves={max_waves} reached before wave "
                f"{wave_index + 1}"
            )
            _halt(conn, result, initiative, reason, project_id, now_fn, log_fn)
            return result

        # (a) Check for an active pause marker BEFORE dispatching the wave.
        marker = _active_pause_marker(resolved_repo, initiative_id, seen_markers)
        if marker is not None:
            reason = f"operator pause marker: {marker.reason}"
            now = now_fn()
            mark_paused(conn, initiative_id, reason, now)
            result.status = "paused"
            result.halted = True
            result.halt_reason = reason
            qid = file_open_question(
                conn,
                project_id,
                f"Initiative #{initiative_id} paused at marker: {marker.reason}",
                f"Wave {wave_index + 1} ({', '.join(f'#{t}' for t in wave)}) "
                f"not yet dispatched. Remove/resolve the pause-for-review "
                f"marker in the plan file, then re-run "
                f"--initiative {initiative_id} to resume.",
            )
            result.open_question_id = qid
            log_fn(
                f"[Initiative #{initiative_id}] HALTED before wave "
                f"{wave_index + 1} — {reason}"
            )
            return result

        # (b) Dispatch the whole wave in parallel.
        log_fn(
            f"[Initiative #{initiative_id}] dispatching wave "
            f"{wave_index + 1}/{len(waves)}: "
            + ", ".join(f"#{t}" for t in wave)
        )
        wave_results = dispatch_fn(list(wave), args)
        if _is_awaitable(wave_results):
            wave_results = await wave_results
        wave_results = list(wave_results or [])
        result.waves_dispatched += 1

        # (c) Accumulate cost for EVERY task in the wave, even on halt.
        for tr in wave_results:
            add_cost(conn, initiative_id, tr.cost)
            result.total_cost_added += max(0.0, _safe_float(tr.cost))

        # (d) Halt if ANY task in the wave failed / was gate-blocked.
        failed = [tr for tr in wave_results if not tr.succeeded]
        completed = [tr for tr in wave_results if tr.succeeded]
        result.tasks_completed.extend(tr.task_id for tr in completed)

        if failed:
            result.tasks_failed.extend(tr.task_id for tr in failed)
            first = failed[0]
            reason = (
                f"task #{first.task_id} blocked/failed "
                f"(outcome={first.outcome})"
            )
            now = now_fn()
            mark_paused(conn, initiative_id, reason, now)
            result.status = "paused"
            result.halted = True
            result.halt_reason = reason
            qid = file_open_question(
                conn,
                project_id,
                f"Initiative #{initiative_id} halted: task "
                f"#{first.task_id} blocked. Review and resume.",
                f"Wave {wave_index + 1} had {len(failed)} failed/blocked "
                f"task(s): "
                + ", ".join(f"#{tr.task_id} ({tr.outcome})" for tr in failed)
                + f". Fix the blocker(s), then re-run "
                f"--initiative {initiative_id} to resume (done sub-tasks "
                f"are skipped).",
            )
            result.open_question_id = qid
            log_fn(
                f"[Initiative #{initiative_id}] HALTED after wave "
                f"{wave_index + 1} — {reason}"
            )
            return result

        log_fn(
            f"[Initiative #{initiative_id}] wave {wave_index + 1} complete: "
            f"{len(completed)} task(s) done."
        )

    # (e) All waves dispatched successfully → done.
    now = now_fn()
    mark_done(conn, initiative_id, now)
    result.status = "done"
    log_fn(
        f"[Initiative #{initiative_id}] COMPLETE — all "
        f"{result.waves_dispatched} wave(s) succeeded."
    )
    return result


def _halt(
    conn: sqlite3.Connection,
    result: InitiativeRunResult,
    initiative: dict,
    reason: str,
    project_id: int,
    now_fn: Callable[[], datetime],
    log_fn: Callable[[str], None],
) -> None:
    """Mark paused + file an open_question for a non-task-failure halt."""
    now = now_fn()
    mark_paused(conn, result.initiative_id, reason, now)
    result.status = "paused"
    result.halted = True
    result.halt_reason = reason
    result.open_question_id = file_open_question(
        conn,
        project_id,
        f"Initiative #{result.initiative_id} halted: {reason}",
        f"Re-run --initiative {result.initiative_id} to resume.",
    )
    log_fn(f"[Initiative #{result.initiative_id}] HALTED — {reason}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe_float(value: object) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # filter NaN


def _is_awaitable(obj: object) -> bool:
    import inspect
    return inspect.isawaitable(obj)


def _active_pause_marker(
    repo_path: str | Path | None,
    initiative_id: int,
    seen_markers: set[str],
) -> PauseMarker | None:
    """Return the first not-yet-seen pause marker in the plan file, if any.

    Reads the plan file fresh each wave so an operator who edits the file
    mid-run is respected. Newly-encountered markers (keyed by reason text)
    trigger a halt; markers already seen this run do not re-fire. Any I/O
    error is swallowed — initiative tracking must never crash dispatch.
    """
    if repo_path is None:
        return None
    try:
        path = InitiativePlan.plan_path(Path(repo_path), initiative_id)
        if not path.exists():
            return None
        markers = parse_pause_markers(path.read_text())
    except OSError:
        logger.exception(
            "Failed to read plan file for pause markers (initiative=%s)",
            initiative_id,
        )
        return None
    for marker in markers:
        if marker.reason not in seen_markers:
            seen_markers.add(marker.reason)
            return marker
    return None


def _resolve_repo_path(
    repo_path: str | Path | None,
    tasks: Sequence[dict],
    conn: sqlite3.Connection,
) -> str | None:
    """Best-effort resolve the target repo directory for plan-file reads."""
    if repo_path is not None:
        return str(repo_path)
    if not tasks:
        return None
    try:
        from equipa.dispatch import resolve_project_dir
        return resolve_project_dir(dict(tasks[0]))
    except Exception:
        logger.exception("Could not resolve project dir for initiative tasks")
        return None


def _read_task_status(conn: sqlite3.Connection, task_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return (row[0] if row else "") or ""


def _read_task_cost(conn: sqlite3.Connection, task_id: int) -> float:
    """Sum recorded ``agent_runs.cost_usd`` for a task (best-effort)."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_runs "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    return _safe_float(row[0] if row else 0.0)


async def _default_dispatch_wave(
    task_ids: list[int], args: object
) -> list[WaveTaskResult]:
    """Default dispatch seam: run the wave via the parallel-tasks machinery.

    Reuses :func:`equipa.dispatch.run_parallel_tasks` so a wave gets the
    SAME worktree-isolation and security-gate guarantees as ``--tasks``
    mode. After dispatch, each task's terminal status and accumulated cost
    are read back from TheForge to build per-task results.

    A task whose terminal status is ``done`` (or which produced a passing
    outcome) counts as succeeded; anything else halts the initiative.
    """
    from equipa.dispatch import run_parallel_tasks
    from equipa.db import get_db_connection

    await run_parallel_tasks(list(task_ids), args)

    results: list[WaveTaskResult] = []
    conn = get_db_connection(write=False)
    try:
        for tid in task_ids:
            status = _read_task_status(conn, tid)
            cost = _read_task_cost(conn, tid)
            outcome = "done" if status == "done" else (status or "unknown")
            results.append(
                WaveTaskResult(task_id=tid, outcome=outcome, cost=cost)
            )
    finally:
        conn.close()
    return results
