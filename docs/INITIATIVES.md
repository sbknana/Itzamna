# EQUIPA Initiatives

An **initiative** is a long-horizon piece of work composed of multiple
EQUIPA-dispatched tasks. Phase 1 introduces shared working memory across
those tasks so each successive dispatch can build on what its siblings
already learned.

## When to use one

Use an initiative when:

* The work spans 3+ tasks aimed at one overarching goal.
* Earlier tasks make decisions that later tasks must respect (e.g.
  "we picked Postgres over Mongo and migrated the schema in #2400 —
  don't propose Mongo again").
* You want one chronological history per goal, not scattered task
  descriptions and decisions in TheForge.

Do NOT use an initiative for one-off bug fixes or single-task features.
Adding initiative overhead to a single task is pure cost.

## Lifecycle

1. **Create** the initiative from the orchestrator:

   ```
   python forge_orchestrator.py \
       --create-initiative "Initiative concept" \
       --initiative-project equipa \
       --initiative-goal "Phased rollout of multi-task initiatives"
   ```

   The command prints the new initiative ID. (The flags
   `--initiative-project` / `--initiative-goal` are dedicated to this
   sub-command to avoid colliding with the dispatch-side `--project`
   integer arg and `--goal` Manager-mode flag.)

2. **List** active initiatives:

   ```
   python forge_orchestrator.py --list-initiatives
   python forge_orchestrator.py --list-initiatives --initiative-project equipa
   ```

3. **Attach tasks** to the initiative by setting `tasks.initiative_id`
   when you file the task. Tasks without `initiative_id` continue to
   dispatch exactly as before — initiatives are strictly opt-in.

4. **Dispatch** as usual. When the task has an `initiative_id`, the
   orchestrator:
   * Ensures `.equipa/initiative-<id>.md` exists in the target repo
     (creating it from the DB row if needed).
   * Injects the plan content into the agent's system prompt under
     `## INITIATIVE CONTEXT`.
   * Adds an instruction telling the agent to emit a structured
     `ORCHESTRATOR_OUTPUT` block at completion.
   * After the dev/test loop, parses that block and appends a sub-task
     entry to the plan file (atomic + file-locked).
   * Commits the plan file change to the task's branch so it merges
     with the rest of the task's commits.

5. **Run the whole initiative** with Phase 2 orchestrator mode (see
   [Orchestrator mode](#orchestrator-mode-phase-2) below). One command
   walks the sub-task DAG, dispatches in waves, halts on the first
   failure or pause marker, and tracks cost.

## Orchestrator mode (Phase 2)

Once an initiative has sub-tasks attached (`tasks.initiative_id` set and
each task's `tasks.blocked_by` listing its prerequisite task IDs), run
the entire thing end-to-end:

```
python forge_orchestrator.py --initiative 42
```

What happens:

1. **Sub-task selection** — every task with `initiative_id = 42` whose
   status is `todo`, `blocked`, or `in_progress` is collected. Tasks
   already `done`/`cancelled` are skipped (this is what makes resume
   idempotent).
2. **DAG + waves** — `tasks.blocked_by` (comma-separated task IDs) builds
   a dependency graph. Tasks with no remaining in-initiative dependency
   form **wave 1**; tasks whose deps are all in wave 1 form **wave 2**;
   and so on. A dependency on a task *outside* the initiative is treated
   as already satisfied. A **cycle** (e.g. A→B→A) aborts with a clear
   error naming the offending path — nothing is dispatched.
3. **Wave dispatch** — each wave's tasks dispatch in parallel through the
   same worktree-isolated, security-gated machinery as `--tasks` mode
   (respecting `--max-concurrent`). The orchestrator waits for the whole
   wave before advancing.
4. **Halt-on-failure** — if any task in a wave ends `blocked`/`failed`
   (or is gate-blocked by the security review), the initiative is marked
   `paused`, an `open_question` is filed, and remaining waves are NOT
   dispatched. A paused initiative is the **expected** outcome of a
   failure, not a crash — the orchestrator exits 0.
5. **Cost** — after each sub-task, its cost accumulates into
   `initiatives.total_cost`. Cost is tracked even when the run halts.
6. **Completion** — when every wave succeeds, the initiative is marked
   `done` with `completed_at` set.

Flags:

```
python forge_orchestrator.py --initiative 42 --dry-run    # print wave plan, dispatch nothing
python forge_orchestrator.py --initiative 42 --max-waves 3 # safety cap on wave count
```

`--list-initiatives` now includes a `COST` column showing
`initiatives.total_cost` per initiative.

### Status transitions

| Event                                   | status        |
| --------------------------------------- | ------------- |
| `--create-initiative`                   | `active`      |
| first `--initiative <id>` run           | `in_progress` (sets `started_at`) |
| all waves succeed                       | `done` (sets `completed_at`) |
| any sub-task failure / gate-block       | `paused` (sets `paused_at` + `pause_reason`) |
| operator pause marker hit               | `paused`      |
| operator cancels manually (`UPDATE` SQL)| `cancelled`   |

### Pause markers (operator-only)

To stop an initiative at a checkpoint of your choosing, hand-edit a
marker into the **human-editable** section of the plan file (anywhere
*above* the `BEGIN ORCHESTRATOR-MANAGED` marker):

```html
<!-- pause-for-review reason="wait for design sign-off on the API shape" -->
```

Before each wave, the orchestrator scans that section. A marker it has
not seen yet halts the run: status → `paused`, `pause_reason` set to the
marker's `reason` (or `operator-set pause marker` if omitted), and an
`open_question` titled *"Initiative #N paused at marker: …"* is filed.
Markers inside the orchestrator-managed block are ignored.

> Pause markers are **operator-only** in Phase 2. Planner-agent insertion
> of markers is deferred to a future phase.

**How to add a pause marker to an existing initiative:**

1. Open `<target-repo>/.equipa/initiative-<id>.md`.
2. Add the `<!-- pause-for-review reason="…" -->` line above
   `<!-- BEGIN ORCHESTRATOR-MANAGED -->`.
3. Commit it (the plan file is committed in the target repo).
4. The next `--initiative <id>` run halts before the wave that would
   have run past the marker.

### How to resume after a halt

A halt (failure or pause marker) leaves the initiative `paused` and an
`open_question` describing why.

1. **Read the open_question** to see which task blocked or which marker
   fired.
2. **Fix the blocker** — fix the failing task and set it back to `todo`,
   or **remove/resolve the pause marker** in the plan file.
3. **Re-run** the same command:

   ```
   python forge_orchestrator.py --initiative 42
   ```

   Resume is idempotent: already-`done` sub-tasks are skipped, only the
   remaining `todo` tasks whose deps are met are dispatched, and the
   status transitions back to `in_progress`. Re-running a `done`
   initiative is a no-op with an informational log line.

## The plan file

The plan file lives in the **target repo** (committed) at:

```
<target-repo>/.equipa/initiative-<id>.md
```

The full format is documented in
[`INITIATIVE_PLAN_FORMAT.md`](./INITIATIVE_PLAN_FORMAT.md). The
practical rules:

* The **top of the file** (above the `BEGIN ORCHESTRATOR-MANAGED`
  marker) is yours to edit. Original plan, design notes, links.
* The **sub-task section** (between the BEGIN and END markers) is
  orchestrator-exclusive. **Do not hand-edit it** — your edits will
  be preserved across appends, but you risk breaking the parser if
  you ever remove the markers.

## What is NOT in Phase 2 (yet)

* Drift evaluator sub-tasks (Phase 3).
* Per-initiative lesson retrieval bias (Phase 3).
* State snapshots at task boundaries (Phase 3).
* Planner-agent pause-marker insertion (markers are operator-only).
* Initiative cost cap with auto-halt (Phase 2 tracks cost only — no cap).
* PreCompact harness hook (Phase 4, separate from EQUIPA repo).
* Post-initiative auto-draft session note (Phase 4).

## Backward compatibility

Tasks with `initiative_id IS NULL` route through the existing
dispatch flow unchanged. Every test that existed before Phase 1
continues to pass. Nothing about the orchestrator's default behaviour
changes for non-initiative tasks.
