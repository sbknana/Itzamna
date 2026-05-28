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

   Phase 2 will add a dedicated `--initiative <id>` orchestrator mode.

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

## What is NOT in Phase 1

* `--initiative <id>` orchestrator dispatch mode (Phase 2).
* Drift evaluator sub-tasks (Phase 3).
* Per-initiative lesson retrieval bias (Phase 3).
* State snapshots at task boundaries (Phase 3).
* Human-checkpoint pause markers (Phase 2).
* PreCompact harness hook (Phase 4, separate from EQUIPA repo).
* Post-initiative auto-draft session note (Phase 4).

## Backward compatibility

Tasks with `initiative_id IS NULL` route through the existing
dispatch flow unchanged. Every test that existed before Phase 1
continues to pass. Nothing about the orchestrator's default behaviour
changes for non-initiative tasks.
