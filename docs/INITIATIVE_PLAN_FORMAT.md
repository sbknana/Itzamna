# Initiative Plan File Format

The plan file is the per-initiative shared working memory used by EQUIPA
to coordinate sibling tasks under a long-horizon goal. It lives in the
**target repository** at:

```
.equipa/initiative-<id>.md
```

The plan file IS committed to the target repo — it is the persistent
substrate of an initiative. Only `.equipa-artifacts/` is gitignored.

## Canonical layout

```markdown
# Initiative: <name>

**ID:** <initiative_id>
**Goal:** <goal text — verbatim from DB row, never mutated by orchestrator>
**Status:** <active | paused | done | cancelled>
**Created:** <YYYY-MM-DD>
**Target project:** <project codename>

---

## Original Plan

<optional free-form section the operator writes once at initiative creation>

---

## Sub-tasks

<!-- AUTO-MAINTAINED BY ORCHESTRATOR BELOW THIS LINE — do not edit by hand -->
<!-- BEGIN ORCHESTRATOR-MANAGED -->

### #<task_id>: <task title>
**Status:** <todo | in_progress | done | blocked>
**Branch:** <branch name or "merged <sha>">
**Completed:** <YYYY-MM-DD HH:MM or "—">
**Summary:** <one paragraph, from agent output>
**Decisions:**
- <decision 1 statement> — <rationale>
- <decision 2 statement> — <rationale>

<!-- END ORCHESTRATOR-MANAGED -->
```

## Region rules

* **Above** `<!-- BEGIN ORCHESTRATOR-MANAGED -->` — human-editable.
  The orchestrator never touches this region. Goal, original plan, and
  any operator-authored notes live here.
* **Between** `<!-- BEGIN ORCHESTRATOR-MANAGED -->` and
  `<!-- END ORCHESTRATOR-MANAGED -->` — orchestrator-exclusive,
  append-only. Newest sub-task at the bottom.
* The plan file is the SINGLE WRITER's domain. Agents read it but do
  not write to it. They emit a structured `ORCHESTRATOR_OUTPUT` block;
  the orchestrator parses and appends.

## Sub-task headings

Sub-task headings use the exact format `### #<task_id>: <title>` so
they can be grep-matched (e.g. `grep -n '^### #2484:'`).

## Agent output protocol

For any task with a non-null `initiative_id`, the agent prompt receives
an additional instruction directing it to emit:

```
<!-- ORCHESTRATOR_OUTPUT -->
SUMMARY: <one-paragraph summary>
DECISIONS:
- <decision> | <rationale>
- <decision> | <rationale>
<!-- /ORCHESTRATOR_OUTPUT -->
```

The parser tolerates:

* A missing block → a placeholder entry is appended (with a warning).
* A malformed block → captures what it can; logs a warning.
* Multiple blocks → uses the LAST one.
* A missing `|` separator → treats the whole line as the decision text
  with empty rationale.

Parser failures are NEVER fatal. The task completes either way.

## Atomicity guarantees

* Writes use `tempfile + os.replace()` — readers never see a half-
  written plan file, even across a crash.
* A POSIX file lock (`fcntl.flock` on `.equipa/initiative-<id>.md.lock`)
  serialises concurrent appends from sibling dispatches in the same
  initiative.
* On Windows the orchestrator must serialise appends externally
  (the fcntl-based lock raises a clear error on import; we have not
  yet validated EQUIPA on Windows).
