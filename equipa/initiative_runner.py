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

import contextlib
import fcntl
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator, Sequence

from equipa.initiative import BEGIN_MARKER, InitiativePlan, _sanitize_agent_text
from equipa.security import _make_untrusted_delimiter, wrap_untrusted

logger = logging.getLogger(__name__)


# Task statuses the runner treats as "still needs work" when selecting the
# sub-tasks of an initiative on a NORMAL resume. ``in_progress`` is
# deliberately excluded (S3): a task left ``in_progress`` is almost always a
# crashed or still-live concurrent run, and re-selecting it would re-dispatch
# the same wave against the same repo — the documented worktree-contamination
# foot-gun. ``--force`` (see :data:`FORCE_PENDING_STATUSES`) opts back in.
PENDING_STATUSES = ("todo", "blocked")

# With ``--force`` we also re-select ``in_progress`` tasks (operator has
# confirmed no other run is live and wants to reclaim a stuck task).
FORCE_PENDING_STATUSES = ("todo", "blocked", "in_progress")

# Default safety ceiling on the number of waves dispatched in one run (S7).
# The accepted Phase 2 design defers a cost cap to Phase 3, so this wave cap
# is the only automatic backstop against a pathological DAG dispatching an
# unbounded number of waves (and unbounded spend). It is a halt-with-pause,
# not a crash, and the operator can raise it explicitly via ``--max-waves``.
DEFAULT_MAX_WAVES = 25

# Max chars of an operator pause-marker reason that we persist / inject. The
# reason is untrusted repo content (see :func:`_fence_untrusted_reason`).
PAUSE_REASON_MAX_CHARS = 500

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
    """A parsed operator pause marker from the plan file.

    ``position`` is the marker's character offset within the human-editable
    section. It is part of the de-dup identity (S4) so that two distinct
    pause points sharing identical ``reason`` text (or both defaulting) are
    treated as separate halts rather than collapsing to one.
    """

    reason: str
    position: int = 0

    @property
    def key(self) -> tuple[int, str]:
        """Identity used to de-dup "already seen" markers within a run."""
        return (self.position, self.reason)


def parse_pause_markers(plan_text: str) -> list[PauseMarker]:
    """Extract operator pause markers from the plan file's human section.

    Only the region ABOVE the ``BEGIN ORCHESTRATOR-MANAGED`` marker is
    scanned — markers inside the orchestrator-managed block (which the
    orchestrator itself writes) are ignored. Markers are returned in
    document order. A marker with no ``reason`` attribute yields a default
    reason string so the pause is still self-describing. Each marker carries
    its character offset so identical-reason markers at different positions
    remain distinct halts (S4).
    """
    if not plan_text:
        return []
    human_section = plan_text.split(BEGIN_MARKER, 1)[0]
    markers: list[PauseMarker] = []
    for m in PAUSE_MARKER_RE.finditer(human_section):
        reason = (m.group("reason") or "").strip()
        markers.append(
            PauseMarker(
                reason=reason or "operator-set pause marker",
                position=m.start(),
            )
        )
    return markers


def _fence_untrusted_reason(reason: str) -> str:
    """Sanitise + fence an untrusted pause-marker reason (S1).

    The pause-marker ``reason`` is free text from the plan file, which lives
    INSIDE the target repo and is therefore writable by any sub-task agent
    that gets a worktree. Phase 1 fences ALL plan-file content injected into
    prompts behind an unpredictable ``<<<UNTRUSTED_...>>>`` delimiter because
    it is a cross-task prompt-injection channel. The reason text lands in
    ``initiatives.pause_reason`` and in ``open_questions`` rows, both of
    which are later surfaced into orchestrator/agent prompts, so it must get
    the same treatment: strip the Unicode bidi / zero-width / control chars
    (via Phase 1's :func:`_sanitize_agent_text`) and wrap the result in a
    fresh per-call untrusted fence so injected instructions cannot escape
    into a downstream model's context as trusted text.
    """
    safe = _sanitize_agent_text(reason, PAUSE_REASON_MAX_CHARS)
    return wrap_untrusted(safe, _make_untrusted_delimiter())


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
    conn: sqlite3.Connection, initiative_id: int, *, force: bool = False
) -> list[dict]:
    """Return all unfinished sub-tasks of an initiative.

    Selects tasks where ``initiative_id`` matches and status is one of
    :data:`PENDING_STATUSES` (``todo`` / ``blocked``). ``done`` / ``cancelled``
    tasks are excluded so a resume run naturally skips already-completed work
    (idempotency). ``in_progress`` tasks are excluded too unless ``force`` is
    set (S3): a lingering ``in_progress`` task usually means a crashed or
    still-live concurrent run, and re-selecting it would double-dispatch the
    same wave. ``--force`` lets the operator reclaim such tasks deliberately.
    """
    statuses = FORCE_PENDING_STATUSES if force else PENDING_STATUSES
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT id, project_id, title, status, blocked_by, role, "
        f"initiative_id FROM tasks "
        f"WHERE initiative_id = ? AND status IN ({placeholders}) "
        f"ORDER BY id",
        (initiative_id, *statuses),
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

class InitiativeLockError(InitiativeError):
    """Raised when another live ``--initiative <id>`` run holds the lock (S3)."""


def _initiative_lock_dir(repo_path: str | Path) -> Path:
    """Return a private, per-user directory to hold initiative lock files (N3).

    Preference order:
      1. ``$XDG_RUNTIME_DIR/equipa`` — the standard per-user runtime dir,
         already 0700 and owned by the current user on systemd hosts.
      2. ``<repo>/.git/equipa-locks`` — inside the repo's git dir, which is
         not part of the working tree (so ``git add`` cannot stage it) and is
         owned by the user who owns the checkout.
      3. ``<tmp>/equipa-locks-<uid>`` — last resort, but namespaced by uid and
         created 0700 so it is NOT a world-shared path another user can
         pre-create or hijack.

    The returned directory is created with mode 0700 if absent.
    """
    import os
    import tempfile

    candidates: list[Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(Path(xdg) / "equipa")
    candidates.append(Path(repo_path) / ".git" / "equipa-locks")
    candidates.append(
        Path(tempfile.gettempdir()) / f"equipa-locks-{os.getuid()}"
    )

    for candidate in candidates:
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Re-assert restrictive perms in case the dir pre-existed with a
            # looser mode (mkdir's mode is ignored when the dir already exists).
            candidate.chmod(0o700)
            return candidate
        except OSError:
            logger.debug("Lock dir candidate unusable: %s", candidate, exc_info=True)
            continue
    # Every candidate failed — surface it so the caller fails closed.
    raise OSError(
        f"No usable per-user lock directory for repo {repo_path} "
        f"(tried: {', '.join(str(c) for c in candidates)})"
    )


@contextlib.contextmanager
def _initiative_lock(
    repo_path: str | Path | None, initiative_id: int
) -> Iterator[None]:
    """Hold an advisory lock for the duration of a live initiative run (S3).

    Takes a non-blocking ``flock`` on a per-initiative lock file (the same
    ``flock`` pattern the Athena truth-sync cron uses) so a second concurrent
    ``--initiative <id>`` run on the same host refuses to start rather than
    double-dispatching the same wave against the same repo. If the repo path
    cannot be resolved the lock is a no-op (best-effort — locking must never
    be the reason a legitimate run cannot start), but the in_progress-task
    exclusion in :func:`fetch_initiative_tasks` still guards the common case.

    Raises :class:`InitiativeLockError` if the lock is already held.
    """
    if repo_path is None:
        yield
        return
    # The lock file lives in a PER-USER runtime directory (see
    # :func:`_initiative_lock_dir`), NEVER in the target repo (so a sub-task
    # agent running ``git add`` can never commit it) and NEVER in world-shared
    # /tmp. It is keyed on the resolved repo path + initiative id so two
    # different repos with the same initiative id do not collide.
    import hashlib

    # N2: blake2b (not sha1) for the lock-file name. This is a naming use, not
    # a security use, but semgrep blocks bare sha1 — blake2b is unflagged and
    # equally fine for deriving a short, stable filename token.
    repo_key = hashlib.blake2b(
        str(repo_path).encode("utf-8"), digest_size=8
    ).hexdigest()
    # N3: place the lock in a PER-USER runtime dir, never world-shared /tmp.
    # A world-readable path under /tmp lets another local user pre-hold the
    # flock (DoS the safety control) or own the file so open() raises and the
    # code fails open — silently dropping the S3 concurrency guard.
    try:
        lock_dir = _initiative_lock_dir(repo_path)
        lock_path = (
            lock_dir / f"equipa-initiative-{initiative_id}-{repo_key}.lock"
        )
        fh = open(lock_path, "w")
    except OSError as exc:
        # N3: fail CLOSED. If we cannot create the lock dir or open the lock
        # file we cannot prove no other run holds it, so refuse to dispatch
        # rather than silently running with no concurrency protection.
        raise InitiativeLockError(
            f"Initiative #{initiative_id}: could not acquire lock "
            f"({exc}). Refusing to run without concurrency protection."
        ) from exc
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InitiativeLockError(
                f"Initiative #{initiative_id} already has a live run "
                f"(lock held on {lock_path}). Refusing concurrent dispatch."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


async def run_initiative(
    initiative_id: int,
    args: object,
    *,
    dispatch_fn: DispatchFn | None = None,
    conn: sqlite3.Connection | None = None,
    repo_path: str | Path | None = None,
    max_waves: int | None = None,
    force: bool = False,
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
        max_waves: safety cap on the number of waves dispatched. ``None``
            applies :data:`DEFAULT_MAX_WAVES` (S7); pass an explicit int to
            override.
        force: re-select ``in_progress`` tasks on resume (S3). Off by
            default so a crashed/concurrent run's tasks are not re-dispatched.
        now_fn / log_fn: injectable clock and logger for testing.

    Returns:
        :class:`InitiativeRunResult` describing the final state. A pause is
        a normal (non-exception) outcome with ``halted=True``.

    Raises:
        :class:`CycleError` if the dependency graph contains a cycle.
        :class:`InitiativeError` if the initiative id is unknown.
        :class:`InitiativeLockError` if a concurrent run holds the lock.
    """
    owns_conn = conn is None
    if conn is None:
        from equipa.db import get_db_connection
        conn = get_db_connection(write=True)

    if dispatch_fn is None:
        dispatch_fn = _default_dispatch_wave

    if max_waves is None:
        max_waves = DEFAULT_MAX_WAVES

    # Resolve the repo path once, up front, so we can both lock on it and
    # read plan-file pause markers from it.
    resolved_repo = _resolve_repo_path(
        repo_path, fetch_initiative_tasks(conn, initiative_id, force=force), conn
    )

    try:
        with _initiative_lock(resolved_repo, initiative_id):
            return await _run_initiative_inner(
                initiative_id=initiative_id,
                args=args,
                dispatch_fn=dispatch_fn,
                conn=conn,
                repo_path=resolved_repo,
                max_waves=max_waves,
                force=force,
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
    force: bool = False,
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

    tasks = fetch_initiative_tasks(conn, initiative_id, force=force)

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

    # Pause markers are de-duplicated WITHIN a single run by (position,
    # reason) so the same marker is not re-evaluated on every wave check, but
    # two distinct markers sharing identical reason text remain separate
    # halts (S4). There is NO cross-run baseline: ``seen_markers`` starts
    # empty, so on any run (fresh or resume) the FIRST marker still present in
    # the plan file halts immediately. The operator is expected to remove or
    # edit the marker before re-running ``--initiative`` to get past it.
    seen_markers: set[tuple[int, str]] = set()

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
            # S1: the marker reason is untrusted repo content. Sanitise +
            # fence it before it lands in any prompt-bound column
            # (initiatives.pause_reason, open_questions.question/context).
            fenced_reason = _fence_untrusted_reason(marker.reason)
            reason = f"operator pause marker: {fenced_reason}"
            now = now_fn()
            mark_paused(conn, initiative_id, reason, now)
            result.status = "paused"
            result.halted = True
            result.halt_reason = reason
            qid = file_open_question(
                conn,
                project_id,
                f"Initiative #{initiative_id} paused at operator pause marker",
                f"Wave {wave_index + 1} ({', '.join(f'#{t}' for t in wave)}) "
                f"not yet dispatched. Operator-supplied reason (untrusted "
                f"plan-file content, fenced): {fenced_reason}. Remove/resolve "
                f"the pause-for-review marker in the plan file, then re-run "
                f"--initiative {initiative_id} to resume.",
            )
            result.open_question_id = qid
            log_fn(
                f"[Initiative #{initiative_id}] HALTED before wave "
                f"{wave_index + 1} — operator pause marker"
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
        # S2-INIT-06: sum the wave's valid per-task costs and persist them in a
        # SINGLE add_cost call (one UPDATE + commit) instead of one commit per
        # task, avoiding N+1 commits per wave. add_cost already drops
        # non-positive / non-finite values, so the wave total is the sum of the
        # same per-value filter applied here for the in-memory result tally.
        wave_cost = 0.0
        for tr in wave_results:
            value = _safe_float(tr.cost)
            if value > 0:
                wave_cost += value
        if wave_cost > 0:
            add_cost(conn, initiative_id, wave_cost)
        result.total_cost_added += wave_cost

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
    seen_markers: set[tuple[int, str]],
) -> PauseMarker | None:
    """Return the first not-yet-seen pause marker in the plan file, if any.

    Reads the plan file fresh each wave so an operator who edits the file
    mid-run is respected. Newly-encountered markers (keyed by ``(position,
    reason)`` — S4, so identical reasons at different positions are distinct)
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
        if marker.key not in seen_markers:
            seen_markers.add(marker.key)
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


def _read_task_cost_high_water(conn: sqlite3.Connection, task_id: int) -> int:
    """Return the current max ``agent_runs.id`` for a task (0 if none).

    Captured BEFORE a (re)dispatch so the post-wave cost read can sum only
    the rows this wave actually created. Without this, a retried task whose
    prior attempts already left ``agent_runs`` rows would have its full
    historical cost re-added to ``initiatives.total_cost`` every wave
    (S2-INIT-02 double-count on resume).
    """
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM agent_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def _read_task_cost(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    since_run_id: int = 0,
) -> float:
    """Sum recorded ``agent_runs.cost_usd`` for a task (best-effort).

    When ``since_run_id`` is supplied (a pre-dispatch high-water mark from
    :func:`_read_task_cost_high_water`), only rows created AFTER that mark are
    summed — i.e. the cost delta of the current wave. This prevents a task
    that was retried across waves from re-adding its prior attempts' cost to
    the initiative total each time (S2-INIT-02).
    """
    try:
        if since_run_id > 0:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_runs "
                "WHERE task_id = ? AND id > ?",
                (task_id, since_run_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_runs "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
    except sqlite3.Error:
        return 0.0
    return _safe_float(row[0] if row else 0.0)


def _read_task_gate_outcome(conn: sqlite3.Connection, task_id: int) -> str:
    """Return the most-recent ``agent_runs.outcome`` for a task (S5).

    This is an INDEPENDENT signal from ``tasks.status``: the orchestrator
    records the dispatch outcome (including ``security_review_blocked`` when
    the security gate fired) on the ``agent_runs`` row. Reading it lets the
    runner cross-check that a task whose ``status`` flipped to ``done`` was
    not in fact gate-blocked by the upstream merge-bug class tracked in
    open_question #2451. Best-effort: returns ``""`` if unavailable.
    """
    try:
        row = conn.execute(
            "SELECT outcome FROM agent_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return (row[0] if row and row[0] else "") or ""


def _run_git(repo_path: str, args: list[str]) -> str:
    """Run a read-only ``git`` command in ``repo_path`` and return stdout.

    Raises ``subprocess.CalledProcessError`` on non-zero exit so callers can
    decide whether a missing branch is a violation. Kept tiny and injectable
    so :func:`verify_wave_branch_isolation` is unit-testable without a repo.
    """
    import subprocess

    completed = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def verify_wave_branch_isolation(
    task_ids: list[int],
    repo_path: str | None,
    *,
    wave_base: str | None = None,
    run_git: Callable[[str, list[str]], str] = _run_git,
) -> dict[int, str]:
    """Independently verify per-task worktree/branch isolation (S2).

    The wave dispatcher trusts ``run_parallel_tasks`` to isolate each task on
    its own ``forge-task-<id>`` branch. That isolation has regressed before
    (open_question 2026-05-18: local-branch contamination under concurrent
    same-repo dispatch). Rather than trust it blindly, after each wave we
    assert the invariant the #2488/#2490 worktree fix establishes: every
    dispatched task has its own ``forge-task-<id>`` branch, and no two tasks'
    branches resolve to the IDENTICAL commit (a tell-tale of leaked shared
    working-tree state where one task's commit landed on another's branch).

    ``wave_base`` is the recorded pre-wave HEAD (the commit every task branch
    was forked from). A task that legitimately makes NO commits (doc-only or
    no-op tasks, dispatched regularly by this project) leaves its branch tip
    pointing AT the base. Two such zero-commit tasks in the same wave would
    otherwise collide on an identical HEAD and be spuriously flagged as
    contamination — a false halt of a healthy wave (N1). So a shared HEAD that
    equals ``wave_base`` is NOT contamination: those branches simply committed
    nothing. Only a shared HEAD that has ADVANCED PAST the base (i.e. a real
    commit landed on more than one branch) is a true isolation violation. When
    ``wave_base`` is ``None`` the check falls back to the conservative
    "any shared HEAD is a violation" behaviour.

    Returns ``{task_id: violation_message}`` for each task that fails the
    invariant (empty dict = clean). Best-effort: if the repo path is missing
    or git is unavailable, returns ``{}`` (cannot prove a violation, so do
    not manufacture a halt). The caller downgrades violating tasks so the
    initiative halts instead of advancing on contaminated work.
    """
    if not repo_path or not task_ids:
        return {}

    violations: dict[int, str] = {}
    head_by_task: dict[int, str] = {}
    for tid in task_ids:
        branch = f"forge-task-{tid}"
        try:
            head = run_git(repo_path, ["rev-parse", "--verify", "--quiet", branch])
        except Exception:  # noqa: BLE001 — any git error = unverifiable branch
            violations[tid] = (
                f"isolation check: branch '{branch}' not found after dispatch "
                f"(expected per-task branch from worktree isolation)"
            )
            continue
        head = (head or "").strip()
        if not head:
            violations[tid] = f"isolation check: branch '{branch}' has no HEAD"
            continue
        head_by_task[tid] = head

    # Two distinct tasks resolving to the same commit means a commit landed
    # on the wrong branch (or branches share a tip) — contamination. EXCEPT
    # when that shared commit is the wave base itself: branches that committed
    # nothing (doc-only / no-op tasks) all legitimately point at the base, so
    # an identical-base HEAD is expected, not contamination (N1).
    base = (wave_base or "").strip()
    by_head: dict[str, list[int]] = {}
    for tid, head in head_by_task.items():
        if base and head == base:
            # Zero-commit task: branch tip == base, committed nothing. Not a
            # violation and excluded from collision detection.
            continue
        by_head.setdefault(head, []).append(tid)
    for head, tids in by_head.items():
        if len(tids) > 1:
            collision = ", ".join(f"#{t}" for t in sorted(tids))
            for tid in tids:
                violations[tid] = (
                    f"isolation check: tasks {collision} share identical HEAD "
                    f"{head[:12]} — cross-branch commit contamination"
                )
    return violations


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

    repo_path = _resolve_repo_path(getattr(args, "repo_path", None), [], None)
    if repo_path is None:
        # Best-effort: derive from the first task so the isolation check has
        # a repo to inspect even when args carries no explicit path.
        conn_lookup = get_db_connection(write=False)
        try:
            rows = [
                dict(r)
                for r in conn_lookup.execute(
                    "SELECT id, project_id FROM tasks WHERE id = ?",
                    (task_ids[0],),
                ).fetchall()
            ] if task_ids else []
        finally:
            conn_lookup.close()
        repo_path = _resolve_repo_path(None, rows, None)

    # N1: record the pre-wave HEAD (the commit every task branch forks from)
    # BEFORE dispatch, so the isolation check can tell a zero-commit task
    # (branch tip == base) apart from a real cross-branch collision.
    wave_base: str | None = None
    if repo_path:
        try:
            from equipa.git_ops import get_default_branch

            default_branch = get_default_branch(repo_path)
            wave_base = _run_git(
                repo_path, ["rev-parse", "--verify", "--quiet", default_branch]
            ).strip() or None
        except Exception:  # noqa: BLE001 — base capture is best-effort
            logger.debug("Could not record pre-wave base HEAD", exc_info=True)

    # S2-INIT-02: snapshot each task's agent_runs high-water mark BEFORE
    # dispatch so the post-wave cost read sums only this wave's NEW rows. A
    # retried task already carries prior-attempt rows; without this delta the
    # initiative total would re-absorb that historical cost on every wave.
    pre_wave_high_water: dict[int, int] = {}
    conn_hw = get_db_connection(write=False)
    try:
        for tid in task_ids:
            pre_wave_high_water[tid] = _read_task_cost_high_water(conn_hw, tid)
    finally:
        conn_hw.close()

    await run_parallel_tasks(list(task_ids), args)

    # S2: independently verify per-task branch isolation BEFORE trusting the
    # dispatch results. A violation downgrades the task so the runner halts.
    isolation_violations = verify_wave_branch_isolation(
        list(task_ids), repo_path, wave_base=wave_base
    )

    results: list[WaveTaskResult] = []
    conn = get_db_connection(write=False)
    try:
        for tid in task_ids:
            status = _read_task_status(conn, tid)
            cost = _read_task_cost(
                conn, tid, since_run_id=pre_wave_high_water.get(tid, 0)
            )
            outcome = "done" if status == "done" else (status or "unknown")

            # S5: cross-check the independent gate signal. A task can read
            # back as ``done`` yet have been gate-blocked (open_question
            # #2451). Treat done-but-gate-blocked as a halt, never a success.
            if outcome in SUCCESS_OUTCOMES:
                gate = _read_task_gate_outcome(conn, tid)
                if gate == "security_review_blocked":
                    logger.warning(
                        "Task #%s status=%s but gate outcome=%s — halting",
                        tid, status, gate,
                    )
                    outcome = "security_review_blocked"

            # S2: a branch-isolation violation overrides any success outcome.
            if tid in isolation_violations:
                logger.error(
                    "Task #%s isolation violation: %s",
                    tid, isolation_violations[tid],
                )
                outcome = "isolation_violation"

            results.append(
                WaveTaskResult(task_id=tid, outcome=outcome, cost=cost)
            )
    finally:
        conn.close()
    return results
