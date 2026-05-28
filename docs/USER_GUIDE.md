# EQUIPA User Guide

Complete reference for installing, configuring, and using EQUIPA — a multi-agent orchestration system for AI-assisted software development.

---

## Table of Contents

1. [Installation](#installation)
2. [Configuration Reference](#configuration-reference)
3. [CLI Modes](#cli-modes)
4. [Agent Roles](#agent-roles)
5. [Smart Features](#smart-features)
6. [Project Management](#project-management)
7. [MCP Setup](#mcp-setup)
8. [Database Structure](#database-structure)
9. [Autoresearch — Automated Prompt Optimization](#autoresearch--automated-prompt-optimization)
10. [Troubleshooting](#troubleshooting)

---

## Installation

### Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| Python 3.10+ | Runs the orchestrator | [python.org](https://www.python.org/downloads/) |
| git | Version control | [git-scm.com](https://git-scm.com/) |
| gh (GitHub CLI) | GitHub repo management | [cli.github.com](https://cli.github.com/) |
| Claude Code CLI | Agent runtime | `npm install -g @anthropic-ai/claude-code` |
| uvx / uv | MCP server runner | `pip install uv` |

### Running the Installer

```bash
cd EQUIPA
python equipa_setup.py
```

The wizard guides you through:
1. Prerequisite verification
2. Install path selection
3. Database creation (empty, with full schema)
4. File copying (orchestrator, prompts, skills)
5. Config file generation (`forge_config.json`, `mcp_config.json`)
6. Claude Code MCP integration (`.mcp.json`)
7. Claude Code context generation (`CLAUDE.md`)
8. Installation verification (9 automated checks)

After setup, open Claude Code in the install directory and it has full MCP access to the database plus context about all commands.

### Manual Installation

If you prefer manual setup:

1. Copy `forge_orchestrator.py`, `dispatch_config.json`, `prompts/`, and `skills/` to your install directory
2. Create a database: `python -c "import sqlite3; sqlite3.connect('theforge.db').executescript(open('schema.sql').read())"`
3. Create `forge_config.json` (see Configuration Reference below)
4. Create `mcp_config.json` pointing to your database
5. Create `.mcp.json` (same format as `mcp_config.json`) for Claude Code MCP access
6. Create `CLAUDE.md` with project context (or copy from an existing installation)

---

## Configuration Reference

### forge_config.json

This file overrides all hardcoded paths in the orchestrator. If absent, the orchestrator falls back to its built-in defaults (backward compatible).

```json
{
    "theforge_db": "/path/to/theforge.db",
    "project_dirs": {
        "myproject": "/path/to/myproject",
        "webapp": "/path/to/webapp"
    },
    "github_owner": "YourGitHubUsername",
    "mcp_config": "/path/to/mcp_config.json",
    "prompts_dir": "/path/to/prompts"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `theforge_db` | string | Absolute path to the SQLite database |
| `project_dirs` | object | Map of project codenames to directory paths |
| `github_owner` | string | GitHub username for repo creation |
| `mcp_config` | string | Path to the MCP configuration file |
| `prompts_dir` | string | Path to the directory containing agent prompt `.md` files |

### dispatch_config.json

Controls agent behavior across all modes — model selection, turn limits, concurrency, and more. Loaded globally at startup.

```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "max_turns": 25,
    "max_turns_developer": 50,
    "max_turns_tester": 20,
    "max_turns_security_reviewer": 40,
    "max_tasks_per_project": 5,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    "security_review": true,
    "model_tester": "haiku",
    "model_epic": "opus"
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `max_concurrent` | 4 | Max projects/tasks running in parallel |
| `model` | sonnet | Default Claude model for all agents |
| `max_turns` | 25 | Default max turns per agent (base, before complexity scaling) |
| `max_turns_developer` | 50 | Turn limit for Developer agents |
| `max_turns_tester` | 20 | Turn limit for Tester agents |
| `max_turns_security_reviewer` | 40 | Turn limit for Security Reviewer agents |
| `max_tasks_per_project` | 5 | Cap tasks per project per auto-run |
| `skip_projects` | [] | Project IDs/codenames to exclude from auto-run |
| `priority_boost` | {} | Manual priority overrides: `{"myproject": 100}` |
| `only_projects` | [] | Whitelist (empty = all projects) |
| `security_review` | false | Auto-run security review after successful dev-test |
| `security_review_tools` | "all" | Which ClaudeStick tools security reviewer can use |
| `model_{role}` | — | Per-role model override (e.g. `model_tester: "haiku"`) |
| `model_{complexity}` | — | Per-complexity model override (e.g. `model_epic: "opus"`) |

See [Model Tiering](#model-tiering) and [Adaptive Complexity](#adaptive-complexity) for details.

### mcp_config.json

MCP server configuration passed to each agent.

```json
{
    "mcpServers": {
        "theforge": {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "mcp-server-sqlite",
                "--db-path",
                "/path/to/theforge.db"
            ]
        }
    }
}
```

---

## CLI Modes

### Single-Agent Mode

Run one agent on one task.

```bash
# Developer agent on a specific task
python forge_orchestrator.py --task 42 -y

# Tester agent
python forge_orchestrator.py --task 42 --role tester -y

# Security reviewer
python forge_orchestrator.py --task 42 --role security-reviewer -y

# Auto-pick next todo task for a project
python forge_orchestrator.py --project 1 -y
```

### Dev+Test Loop Mode

Developer writes code, Tester validates, loop until tests pass (max 3 cycles).

```bash
python forge_orchestrator.py --task 42 --dev-test -y
```

Flow per cycle:
1. Developer agent writes/fixes code
2. Tester agent runs tests (read-only)
3. If tests pass → done. If tests fail → feed failures back to Developer
4. After 3 cycles or no progress → mark task blocked

The orchestrator manages task status — it marks tasks `done` or `blocked` based on test outcomes. Agents don't need to update the database themselves.

### Parallel Tasks Mode

Run multiple independent tasks concurrently within a project.

```bash
# Comma-separated IDs
python forge_orchestrator.py --tasks 109,110,111 --dev-test -y

# Range syntax
python forge_orchestrator.py --tasks 109-114 --dev-test -y

# Preview first
python forge_orchestrator.py --tasks 109-114 --dev-test --dry-run
```

All tasks must belong to the same project. Each runs a full Dev+Test loop. Concurrency is controlled by `max_concurrent` (default 4). Per-task output is buffered and printed when each task completes.

### Manager Mode (Goal-Driven)

Provide a natural-language goal. The system autonomously plans, executes, and evaluates.

```bash
python forge_orchestrator.py --goal "Add user authentication" --goal-project 1 -y
```

Flow per round:
1. **Planner** agent explores codebase, creates 2-8 tasks
2. **Dev+Test** loops execute each task sequentially
3. **Evaluator** agent reviews results: `complete`, `needs_more`, or `blocked`
4. If `needs_more`, evaluator creates follow-up tasks and loop continues

Options:
- `--max-rounds N` — Max plan-execute-evaluate rounds (default: 3)
- `--model opus` — Use a more capable model

### Parallel Goals Mode

Run multiple goals across different projects concurrently.

```bash
python forge_orchestrator.py --parallel-goals goals.json -y
```

Goals file format:
```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "goals": [
        {"goal": "Add dark mode", "project_id": 4},
        {"goal": "Fix login bug", "project_id": 9, "model": "opus"}
    ]
}
```

Each goal runs a full Manager loop. No two goals can target the same project.

### Auto-Run Mode

Scan all projects for pending work, prioritize, and dispatch automatically.

```bash
# See what would run (dry run)
python forge_orchestrator.py --auto-run --dry-run

# Run with confirmation
python forge_orchestrator.py --auto-run

# Skip confirmation
python forge_orchestrator.py --auto-run -y

# Only run one project
python forge_orchestrator.py --auto-run --only-project 1 -y

# Cap tasks per project
python forge_orchestrator.py --auto-run --max-tasks-per-project 2 -y
```

Priority scoring:
```
score = (critical * 10) + (high * 5) + (medium * 2) + (low * 1)
      + 3 if project is 'active'
      + priority_boost from config
```

### Add Project Mode

Register a new project in the database and config.

```bash
python forge_orchestrator.py --add-project "MyApp" --project-dir "/path/to/myapp"
```

This:
1. Creates a row in the `projects` table with status `active`
2. Adds the project directory to `forge_config.json`

### Setup Repos Mode

Initialize git and create GitHub private repos.

```bash
# All projects
python forge_orchestrator.py --setup-repos --dry-run
python forge_orchestrator.py --setup-repos -y

# Single project
python forge_orchestrator.py --setup-repos-project 1 -y
```

### Common Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | sonnet | Claude model (sonnet, opus, haiku) |
| `--max-turns` | 25 | Max agent conversation turns |
| `--retries` | 3 | Max retry attempts on failure |
| `--tasks` | — | Comma-separated or range of task IDs for parallel execution |
| `--dev-test` | — | Enable Dev+Tester iteration loop |
| `--security-review` | — | Run security review after dev-test (overrides config) |
| `--dispatch-config` | dispatch_config.json | Path to custom dispatch config |
| `--dry-run` | — | Show plan without executing |
| `--yes` / `-y` | — | Skip confirmation prompts (auto-enabled for non-TTY) |

---

## Agent Roles

### Built-in Roles

| Role | File | Job | File Access | Bash Access | DB Access |
|------|------|-----|-------------|-------------|-----------|
| Developer | `developer.md` | Write code, fix bugs, implement features | Read/Write/Edit | Build tools, git | Read + Write |
| Tester | `tester.md` | Run tests, report failures | Read only | Test runners only | Read only |
| Planner | `planner.md` | Break goals into 2-8 ordered tasks | Read only | Explore only (ls, git log) | Read + Write (tasks) |
| Evaluator | `evaluator.md` | Verify goal completion, create follow-ups | Read only | Explore only | Read + Write (tasks) |
| SecurityReviewer | `security-reviewer.md` | 4-phase code security review | Read only | Scanning tools only | Read only |

Agents run with `--permission-mode bypassPermissions`. The access restrictions above are enforced via prompt instructions and MCP configuration (read-only DB for testers, no write tools for planners, etc.), not by the CLI permission system.

### Shared Rules (`_common.md`)

All agents receive the `_common.md` rules prepended to their role prompt. This includes:
- Branding (Forgeborn attribution)
- Windows-specific instructions
- Database interaction patterns
- Output format requirements (JSON with `RESULT:` marker)

### Custom Agents

Drop a `.md` file in `prompts/` and it's automatically discovered by `_discover_roles()` at startup. The filename stem becomes the role name. See [CUSTOM_AGENTS.md](CUSTOM_AGENTS.md) for details.

---

## Smart Features

### Checkpoint/Resume

When an agent times out or hits its turn limit, EQUIPA saves the agent's output to `.forge-checkpoints/`. The next time you run that task, the orchestrator loads the checkpoint and injects it as context — the new agent picks up where the last one left off.

```
  [Checkpoint] Loaded checkpoint from attempt #1 (4200 chars). Agent will continue from there.
```

- Checkpoints are saved automatically on `PROCESS_TIMEOUT` (1200s) or `error_max_turns`
- Checkpoints are cleared automatically when a task succeeds (tests pass or no-tests accepted)
- Large checkpoints are truncated to 8000 chars to avoid prompt bloat
- Multiple checkpoint attempts chain: attempt 1 → timeout → attempt 2 → timeout → attempt 3

### Adaptive Complexity

Tasks can have a `complexity` level that scales turn limits:

| Complexity | Turn Multiplier | Example |
|:----------:|:---------------:|---------|
| `simple` | 0.5x | Fix a typo, update a config |
| `medium` | 1.0x | Add a new endpoint, write tests |
| `complex` | 1.5x | Refactor auth system, add WebSocket support |
| `epic` | 2.0x | Build an ML pipeline, full-stack feature |

Set complexity explicitly:
```sql
UPDATE tasks SET complexity = 'epic' WHERE id = 42;
```

Or leave it unset — EQUIPA infers from description length:
- Under 100 chars → `simple`
- 100-400 chars → `medium`
- 400-800 chars → `complex`
- Over 800 chars → `epic`

The multiplier is applied to the role's base turns. A developer (base 50) on an `epic` task gets 100 turns. A tester (base 20) on a `simple` task gets 10 (minimum 10).

### Model Tiering

Assign different Claude models per agent role or per task complexity via `dispatch_config.json`:

```json
{
    "model": "sonnet",
    "model_tester": "haiku",
    "model_epic": "opus"
}
```

Priority chain (first match wins):
1. Per-complexity: `model_epic`, `model_complex`, `model_simple`
2. Per-role: `model_developer`, `model_tester`, `model_security_reviewer`
3. CLI `--model` flag
4. Global `model` in dispatch config
5. Default (`sonnet`)

This lets you save money on simple tasks (Haiku for testers) while throwing compute at complex ones (Opus for epic tasks).

### Auto Dependency Install

Before the first Dev+Test cycle, the orchestrator checks for:
- `pyproject.toml` or `requirements.txt` → creates a venv and runs `pip install`
- `package.json` → runs `npm install`

This prevents agents from wasting turns on dependency setup.

### Auto-Yes for Non-Interactive

When stdin is not a TTY (e.g., `nohup`, SSH pipes, cron), `--yes` is automatically enabled. No more hung processes waiting for confirmation.

---

## ForgeSmith — Prompt Optimization

ForgeSmith is EQUIPA's self-learning pipeline. It runs nightly (via cron) and optimizes agent performance through a multi-stage process.

### Pipeline

```
COLLECT → ANALYZE → LESSONS → SIMBA → RUBRICS → APPLY → GEPA → LOG
```

### SIMBA (Targeted Rule Generation)

SIMBA (**S**ystematic **I**dentification of **M**istakes and **B**ehavioral **A**djustments) analyzes high-variance tasks — roles with both successes and failures — and uses Claude to generate specific improvement rules.

**How it works:**
1. Finds roles with mixed outcomes (contrast what worked vs what didn't)
2. Identifies "hardest cases" (Q-value < 0.3, early-terminated)
3. Claude generates up to 3 rules per role (max 200 chars each)
4. Rules are validated for length, uniqueness (>60% overlap = rejected), and error type
5. After 10+ injections, rules are effectiveness-scored (before vs after success rate)
6. After 50+ injections with no improvement, rules are pruned

**CLI:**
```bash
python forgesmith.py --simba              # All roles
python forgesmith.py --simba developer    # Specific role
python3 scripts/forgesmith_simba.py --prune        # Prune stale rules
```

### GEPA (Automatic Prompt Evolution)

GEPA (**G**eneralized **E**fficient **P**rompt **A**daptation) uses DSPy to evolve entire role prompts based on agent episode history.

**How it works:**
1. Collects 60 days of episodes (minimum 20 required)
2. DSPy reflects on failure traces and proposes instruction improvements
3. Evolved prompts are validated (max 20% text change, protected sections preserved)
4. Version-stamped files created (e.g., `developer_v2.md`)
5. 50/50 A/B test: evolved vs baseline prompt
6. After 10+ tasks per version, success rates compared
7. Underperformers automatically rolled back

**Safety rails:** Max 20% change per cycle. Protected sections (Output Format, RESULT block, Git Commit) never removed. Max 1 evolution per role per week.

**Default model:** `ollama_chat/devstral-small-2:24b` (free, local). Set `ANTHROPIC_API_KEY` for Claude.

**CLI:**
```bash
python forgesmith.py --gepa               # All roles
python forgesmith.py --gepa developer     # Specific role
python forgesmith_gepa.py --status        # A/B test results
python forgesmith_gepa.py --dry-run       # Preview without applying
```

### Context Engineering

The orchestrator assembles agent prompts with token-budget awareness:

| Metric | Value |
|--------|-------|
| Target prompt size | 8,000 tokens |
| Hard limit | 10,000 tokens |
| Episode reduction threshold | 6,000 tokens |

**What gets injected (priority order):**
1. Common rules + role prompt (never trimmed)
2. A/B prompt version (GEPA evolved if available)
3. Lessons (max 5, deduplicated at 60% word overlap)
4. Relevant episodes (2-3, scored by keyword overlap + recency)
5. Task-type guidance (bug_fix, feature, refactor, test)
6. Task description (never trimmed)
7. Extra context (checkpoints, test failures)

**When over budget, trimmed in order:** old episodes → generic lessons → extra context.

### Rubric Scoring & Evolution

Agent runs are scored against role-specific criteria. Weights evolve based on correlation with success:

| Role | Key Criteria |
|------|-------------|
| Developer | result_success, files_changed, tests_written, turns_efficiency |
| Tester | tests_pass, edge_cases, coverage, false_positives (negative) |
| Security Reviewer | vulns_found, severity_accuracy, false_alarms (negative) |

Weight evolution: max ±10% per criterion per cycle, requires 10+ scored runs, 30-day lookback.

### ForgeSmith CLI Reference

```bash
python forgesmith.py --auto               # Full pipeline (nightly cron)
python forgesmith.py --dry-run            # Preview changes
python forgesmith.py --report             # JSON analysis report
python forgesmith.py --simba [ROLE]       # SIMBA rule generation
python forgesmith.py --gepa [ROLE]        # GEPA prompt evolution
python forgesmith.py --propose            # OPRO proposals
python forgesmith.py --lessons [ROLE]     # Show active lessons
python forgesmith.py --rubrics [ROLE]     # Show rubric scores
python forgesmith.py --rollback RUN_ID    # Revert a run's changes
```

### forgesmith_config.json Reference

| Key | Default | Description |
|-----|---------|-------------|
| `lookback_days` | 7 | Days of history to analyze |
| `min_sample_size` | 5 | Minimum runs before making changes |
| `max_changes_per_run` | 5 | Cap on changes per run |
| `max_prompt_patches_per_run` | 2 | Cap on prompt modifications |
| `rollback_threshold` | -0.3 | Score triggering auto-revert |
| `suppression_cooldown_days` | 14 | Days before retrying suppressed changes |
| `thresholds.max_turns_hit_rate` | 0.3 | Rate above which turns are increased |
| `thresholds.turn_underuse_rate` | 0.4 | Rate below which turns are decreased |
| `thresholds.repeat_error_count` | 3 | Occurrences before flagging an error pattern |
| `limits.max_turns_ceiling` | 75 | Maximum turn limit |
| `limits.max_turns_floor` | 10 | Minimum turn limit |
| `rubric_definitions` | (per-role) | Scoring criteria and weights |
| `rubric_evolution` | (object) | Weight evolution settings |
| `protected_files` | (list) | Files ForgeSmith never modifies |

---

## Project Management

### Adding Projects

```bash
python forge_orchestrator.py --add-project "ProjectName" --project-dir "/path/to/code"
```

This:
1. Creates a row in the `projects` table with status `active`
2. Adds the project directory to `forge_config.json`

### Removing Projects

Projects aren't deleted — set their status to `archived`:

```sql
UPDATE projects SET status = 'archived' WHERE id = 42;
```

Remove the entry from `forge_config.json`'s `project_dirs` manually.

### Organizing Work

Tasks have four statuses:
- `todo` — Ready to be picked up
- `in_progress` — Currently being worked on
- `done` — Completed (set by the orchestrator on success)
- `blocked` — Stuck, needs intervention (set by the orchestrator on failure)

Priorities: `critical`, `high`, `medium`, `low`

Complexity (optional): `simple`, `medium`, `complex`, `epic`

---

## MCP Setup

EQUIPA uses MCP (Model Context Protocol) in two ways:

1. **Agent MCP** (`mcp_config.json`) — passed to orchestrator-spawned agents so they can access the database
2. **User MCP** (`.mcp.json`) — gives your own Claude Code sessions direct database access

Both are generated automatically by the EQUIPA setup wizard.

### How Agent MCP Works

1. The orchestrator passes `--mcp-config mcp_config.json` to each `claude -p` invocation
2. Claude Code starts the MCP server (`uvx mcp-server-sqlite`)
3. The agent can read/write the database using MCP tools: `read_query`, `write_query`, `list_tables`, etc.

### How User MCP Works (`.mcp.json`)

The setup wizard generates `.mcp.json` in your install directory. When you run `claude` from that directory, Claude Code auto-detects it and connects to the database. You can then:

- Query projects, tasks, decisions, and session notes
- Create tasks and update statuses
- Log decisions and session summaries
- Run orchestrator commands via Bash

The wizard also generates a `CLAUDE.md` in the install directory that gives Claude full context about available commands, common SQL queries, agent roles, and database tables.

### `.mcp.json` Format

```json
{
    "mcpServers": {
        "equipa": {
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-sqlite", "--db-path", "/path/to/theforge.db"]
        }
    }
}
```

### Verifying MCP

Test the MCP server manually:
```bash
uvx mcp-server-sqlite --db-path /path/to/theforge.db
```

If this command starts without error, MCP is working.

---

## Database Structure

The database has 28 tables organized into these groups:

### Core Tables

| Table | Purpose |
|-------|---------|
| `projects` | Project metadata (name, codename, status, summary) |
| `tasks` | Work items (title, status, priority, complexity, blocked_by) |
| `decisions` | Architectural decisions with rationale |
| `open_questions` | Unresolved questions and blockers |
| `session_notes` | Session summaries and next steps |

### Content & Marketing

| Table | Purpose |
|-------|---------|
| `social_media_posts` | Social media content library |
| `posting_schedule` | Content calendar |
| `content_tickler` | Content inventory alerts |
| `writing_style` | Per-project writing style guides |

### Research & Analysis

| Table | Purpose |
|-------|---------|
| `research` | Research findings and sources |
| `competitors` | Competitive analysis |
| `product_opportunities` | Market opportunities |

### Assets & Build

| Table | Purpose |
|-------|---------|
| `code_artifacts` | Important code references |
| `documents` | Project documentation |
| `project_assets` | Logos, icons, images |
| `components` | Hardware/software components |
| `build_info` | Build commands and output paths |

### System

| Table | Purpose |
|-------|---------|
| `cross_references` | Links between any two records |
| `reminders` | Scheduled reminders |
| `agent_runs` | Agent execution log (role, model, cost, duration, outcome) |

### ForgeSmith (Prompt Optimization)

| Table | Purpose |
|-------|---------|
| `lessons_learned` | Extracted lessons and SIMBA rules with effectiveness scores |
| `agent_episodes` | Agent execution traces with reflections and Q-values |
| `forgesmith_runs` | ForgeSmith execution log (run ID, timestamp, changes made) |
| `forgesmith_changes` | History of all applied changes with effectiveness scoring |
| `rubric_scores` | Per-run rubric evaluation with criteria breakdown |
| `rubric_evolution_history` | Audit trail of rubric weight changes |

### Views

| View | Purpose |
|------|---------|
| `v_project_dashboard` | Active projects with task counts |
| `v_stale_tasks` | In-progress tasks older than 3 days |
| `v_stale_questions` | Unresolved questions older than 7 days |
| `v_upcoming_reminders` | Reminders due within 7 days |
| `v_content_alerts` | Content inventory below threshold |
| `v_cost_by_project` | Total agent cost per project |
| `v_cost_by_role` | Total agent cost per role |

---

## Troubleshooting

### "Database not found"

The orchestrator can't find the SQLite database.

**Fix:** Check `forge_config.json` has the correct `theforge_db` path. Or verify the default path in `forge_orchestrator.py` matches your setup.

### "Could not find project directory"

The project's codename doesn't match any entry in `PROJECT_DIRS` or `forge_config.json`.

**Fix:** Register the project with `--add-project` or add it manually to `forge_config.json`'s `project_dirs`.

### Agent times out (1200s)

The task took longer than the 20-minute wall-clock timeout.

**Fix:** EQUIPA automatically saves a checkpoint on timeout. Simply re-run the same task — the agent will resume from where it left off. For recurring timeouts, tag the task as `epic` complexity to give it more turns, or break it into smaller tasks.

### "No todo tasks found"

Auto-run or `--project` mode found no tasks with status `todo`.

**Fix:** Create tasks in the database. Use MCP, direct SQL, or the Manager mode (`--goal`).

### MCP server won't start

`uvx mcp-server-sqlite` fails.

**Fix:**
1. Ensure `uv` is installed: `pip install uv`
2. Test manually: `uvx mcp-server-sqlite --db-path /path/to/theforge.db`
3. Check `mcp_config.json` has the correct database path
4. On Linux, use the absolute path to `uvx` (e.g., set `UVX_PATH` env var or update `mcp_config.json`)

### Tests fail but code looks correct

The Tester agent is read-only — it can't modify code.

**Fix:** Check the test framework auto-detection. The Tester looks for `pytest`, `jest`, `dotnet test`, etc. If your project uses a non-standard test setup, add instructions to the task description.

### "forge_config.json not found" warning

This is informational, not an error. Without a config file, the orchestrator uses its hardcoded defaults.

**Fix:** Run `equipa_setup.py` to generate the config file, or create one manually (see Configuration Reference above).

### Agent produces no output

The agent may have hit a context limit or encountered an unexpected error.

**Fix:**
1. Try a more capable model: `--model opus`
2. Increase max turns: `--max-turns 50`
3. Check the task description — vague tasks produce poor results
4. Run with `--dry-run` to inspect the system prompt size

---

## Autoresearch — Automated Prompt Optimization

Autoresearch is the automated system for improving agent prompts. It mutates prompts using Opus, benchmarks them against real tasks, and keeps changes that improve success rates.

### Checking Current Performance

```bash
# Show success rates for all roles (sliding window of last 15 runs)
python3 scripts/autoresearch_loop.py --status
```

Output looks like:
```
Role                    Runs  Success  Avg Turns   Target
----------------------------------------------------------
developer                 15   100.0%        8.2      80%
tester                    15   100.0%        5.1      85%
frontend-designer         15   100.0%        7.5      80%
security-reviewer         15   100.0%       12.3      85%
economy-tester            15   100.0%        6.8      80%
story-tester              15   100.0%        5.9      80%
debugger                  15    83.4%        9.1      80%
```

### Running Optimization

```bash
# Optimize a single role
python3 scripts/autoresearch_loop.py --role debugger --target 80

# Optimize all roles below their targets
python3 scripts/autoresearch_loop.py --all --target 80

# Limit to 5 rounds (default is 10)
python3 scripts/autoresearch_loop.py --role debugger --max-rounds 5
```

Each round takes 10-30 minutes depending on task complexity and agent turn counts. The loop prints progress as it goes — which round, which tasks passed/failed, whether the mutation was kept or reverted.

### Generating Prompt Mutations Without Benchmarking

If you just want to generate improved prompts without running the full benchmark loop:

```bash
# Generate mutations for all underperforming roles (via local Ollama)
python3 scripts/autoresearch_prompts.py

# Single role
python3 scripts/autoresearch_prompts.py --role developer

# Use Anthropic Sonnet instead of local Ollama
python3 scripts/autoresearch_prompts.py --tier 2

# Use Opus for highest quality mutations
python3 scripts/autoresearch_prompts.py --tier 3

# Preview without writing files
python3 scripts/autoresearch_prompts.py --dry-run
```

### Rolling Back

If a prompt mutation makes things worse:

```bash
# Rollback a single role to its last backup
python3 scripts/autoresearch_loop.py --rollback developer

# Rollback all roles (autoresearch_prompts.py version)
python3 scripts/autoresearch_prompts.py --rollback
```

Backups are stored in `.autoresearch-backups/` with timestamps.

### How It Fits Together

| System | What It Does | When To Use |
|--------|-------------|-------------|
| **Autoresearch** | Aggressive prompt search. Large rewrites, benchmarked in hours. | A role is underperforming and needs improvement now |
| **ForgeSmith/GEPA** | Incremental prompt evolution. Small changes, A/B tested over weeks. | Maintenance — keeping prompts tuned as projects evolve |
| **ForgeSmith/SIMBA** | Generates behavioral rules injected alongside prompts. | Complements both — targets specific failure patterns |
| **QLoRA Training** | Fine-tunes a local model on successful agent runs. | Want to run agents locally at zero API cost |

---

## Initiatives

Multi-task long-horizon work uses an **initiative** — a first-class
table with a per-initiative plan file (`.equipa/initiative-<id>.md`)
checked into the target repo. The plan is shared working memory across
sibling tasks: each dispatch reads the file at start, and the
orchestrator appends a structured summary after every completion.

Create one with `forge_orchestrator.py --create-initiative "<name>"
--project <codename> --goal "<text>"`, list active initiatives with
`--list-initiatives`. Tasks join an initiative by setting their
`initiative_id` column; tasks without one behave exactly as today.
See [INITIATIVES.md](./INITIATIVES.md) for the full operator guide and
[INITIATIVE_PLAN_FORMAT.md](./INITIATIVE_PLAN_FORMAT.md) for the file
format spec.

---

## Troubleshooting

### synced storage sync corruption

Git doesn't work well with cloud sync services. The `.git/index` file can corrupt.

**Fix:** Use GitHub as your backup. If corruption occurs, delete the local `.git` directory and re-clone from GitHub.

