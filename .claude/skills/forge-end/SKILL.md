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

For each task update:
```sql
UPDATE tasks SET status = '{new_status}', updated_at = CURRENT_TIMESTAMP WHERE id = {task_id};
```

### Step 4: Log decisions (if any)

Ask: "Did you make any decisions this session that should be recorded?"

If yes:
```sql
INSERT INTO decisions (project_id, decision, rationale, alternatives_considered, decision_type, status)
VALUES (?, '{decision}', '{rationale}', '{alternatives}', 'general', 'open');
```
Valid decision_type: general, security_finding, architectural, trade_off, resolution.
Valid status: open, resolved, superseded, wont_fix, failed_resolution.

### Step 5: Record blockers/questions (if any)

Ask: "Any blockers or open questions to record?"

If yes:
```sql
INSERT INTO open_questions (project_id, question, context, created_at)
VALUES (?, '{question}', '{context}', CURRENT_TIMESTAMP);
```

### Step 6: Create session summary

Ask: "Brief summary of what was accomplished?"

**CRITICAL: Sanitize summary and next_steps through lesson_sanitizer.py before DB write.**
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
