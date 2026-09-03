# /forge-end

Session end protocol for TheForge. Run this before ending a work session.

## What This Skill Does

1. Prompts for what was accomplished
2. Updates task statuses in TheForge
3. Logs any decisions made
4. Records blockers/questions
5. Creates session summary with next steps

## Usage

```
/forge-end                 # Interactive - will ask for project
/forge-end YouTubeDownloader
/forge-end 4               # by project_id
```

## Instructions

When this skill is invoked:

### Step 1: Identify the project

If no project specified, ask: "Which project did you work on?"

Find the project:
```sql
SELECT id, name, codename FROM projects
WHERE codename LIKE '%{arg}%' OR name LIKE '%{arg}%' LIMIT 1;
```

### Step 2: Review current tasks

```sql
SELECT id, title, status FROM tasks
WHERE project_id = ? AND status IN ('todo', 'in_progress')
ORDER BY id;
```

Ask: "Which tasks should I update? (e.g., 'task 42 done, task 43 in_progress')"

### Step 3: Update tasks

For each task update (note: `tasks` has no `updated_at` column — dormancy is
measured per-project for exactly this reason):
```sql
UPDATE tasks SET status = '{new_status}' WHERE id = {task_id};
-- for completed tasks, stamp completion:
UPDATE tasks SET status = 'done', completed_at = CURRENT_TIMESTAMP WHERE id = {task_id};
```

### Step 4: Log decisions (if any)

Ask: "Did you make any decisions this session that should be recorded?"

If yes, prefer the sanitizing MCP tool when the `equipa` server is up:
`equipa_decision_add` (requires auth_token, project_id, topic, decision).
SQL fallback — `topic` is NOT NULL, never omit it:
```sql
INSERT INTO decisions (project_id, topic, decision, rationale, alternatives_considered, decision_type, status)
VALUES (?, '{topic}', '{decision}', '{rationale}', '{alternatives}', 'general', 'open');
```
Valid decision_type (per the equipa MCP server): architectural, architecture,
design, docs-architecture, general, ip_finding, packaging, regulatory_finding,
resolution, security_finding, strategy, technical, trade_off.
Valid status: accepted, active, decided, failed_resolution, open, resolved,
superseded, wont_fix.

### Step 5: Record blockers/questions (if any)

Ask: "Any blockers or open questions to record?"

If yes (the timestamp column is `asked_at`, and it defaults):
```sql
INSERT INTO open_questions (project_id, question, context, priority, resolved)
VALUES (?, '{question}', '{context}', 'medium', 0);
```

### Step 6: Create session summary

Ask: "Brief summary of what was accomplished?"

Prefer the sanitizing MCP tool when the `equipa` server is up:
`equipa_session_note_add` (sanitizes server-side; requires auth_token,
project_id, summary). SQL fallback below.

**CRITICAL (SQL path): Sanitize summary and next_steps through lesson_sanitizer.py before DB write.**
Use `sanitize_session_note()` — NOT `sanitize_lesson_content()`. Session notes are
narrative records; the 500-char lesson cap would silently gut them (Equipa task #100027).
`sanitize_session_note()` applies the generous `MAX_SESSION_NOTE_LENGTH` cap and logs
loudly if it ever truncates.

```python
from lesson_sanitizer import sanitize_session_note

# Sanitize user input (preserves multi-thousand-char summaries; no silent truncation)
sanitized_summary = sanitize_session_note(summary)
sanitized_next_steps = sanitize_session_note(next_steps)
```

```sql
INSERT INTO session_notes (project_id, summary, next_steps, session_date)
VALUES (?, '{sanitized_summary}', '{sanitized_next_steps}', CURRENT_TIMESTAMP);
```

### Step 7: Confirm completion

Display:
```
Session logged for {Project Name}:
- Tasks updated: {count}
- Decisions logged: {count}
- Questions recorded: {count}
- Session summary saved

Next steps recorded:
{next_steps}
```

## Quick Mode

If user provides all info at once, skip the prompts:

```
/forge-end YouTubeDownloader --summary "Fixed auth bug" --next "Add tests" --done 42,43
```
