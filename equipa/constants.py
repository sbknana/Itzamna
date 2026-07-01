"""EQUIPA constants, defaults, limits, and enums.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
All values are re-exported via equipa/__init__.py for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Core Paths ---

THEFORGE_DB = Path(os.environ.get(
    "THEFORGE_DB",
    Path(__file__).parent.parent / "theforge.db",
))
MCP_CONFIG = Path(__file__).parent.parent / "mcp_config.json"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
STANDING_ORDERS_DIR = Path(__file__).parent.parent / "standing_orders"
SKILLS_BASE_DIR = Path(__file__).parent.parent / "skills"

# Per-role skill directories (loaded via --add-dir when role has skills)
ROLE_SKILLS = {
    "security-reviewer": SKILLS_BASE_DIR / "security",
    "developer": SKILLS_BASE_DIR / "developer",
    "tester": SKILLS_BASE_DIR / "tester",
    "code-reviewer": SKILLS_BASE_DIR / "code-reviewer",
    "debugger": SKILLS_BASE_DIR / "debugger",
    "qiao-developer": SKILLS_BASE_DIR / "qiao-developer",
}

# Role prompt files (prepended with _common.md automatically)
ROLE_PROMPTS = {
    "developer": PROMPTS_DIR / "developer.md",
    "security-reviewer": PROMPTS_DIR / "security-reviewer.md",
    "tester": PROMPTS_DIR / "tester.md",
    "planner": PROMPTS_DIR / "planner.md",
    "evaluator": PROMPTS_DIR / "evaluator.md",
    "frontend-designer": PROMPTS_DIR / "frontend-designer.md",
    "integration-tester": PROMPTS_DIR / "integration-tester.md",
    "debugger": PROMPTS_DIR / "debugger.md",
    "code-reviewer": PROMPTS_DIR / "code-reviewer.md",
    "qa-tester": PROMPTS_DIR / "qa-tester.md",
    "qiao-developer": PROMPTS_DIR / "qiao-developer.md",
    "qiao-tester": PROMPTS_DIR / "qiao-tester.md",
    "qiao-benchmarker": PROMPTS_DIR / "qiao-benchmarker.md",
    "qiao-researcher": PROMPTS_DIR / "qiao-researcher.md",
}

# --- Agent Defaults ---

DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TURNS = 25
DEFAULT_MAX_RETRIES = 3
PROCESS_TIMEOUT = 3600  # 60 minutes

# Default timeout (seconds) for git/gh subprocess calls.
# Single source of truth for git command timeouts across the codebase.
# Long-running operations (worktree add, push, merge) override this explicitly.
GIT_DEFAULT_TIMEOUT = 30

# Per-role turn limits (used when dispatch config or CLI doesn't specify)
DEFAULT_ROLE_TURNS = {
    "developer": 40,
    "tester": 15,
    "security-reviewer": 30,
    "planner": 20,
    "evaluator": 15,
    "frontend-designer": 35,
    "integration-tester": 20,
    "debugger": 30,
    "code-reviewer": 20,
    "qa-tester": 25,
    "qiao-developer": 40,
    "qiao-tester": 25,
    "qiao-benchmarker": 30,
    "qiao-researcher": 35,
}

# Checkpoint/Resume: save agent output on timeout for continuation
CHECKPOINT_DIR = Path(__file__).parent.parent / ".forge-checkpoints"

# Complexity multipliers applied to per-role turn limits
COMPLEXITY_MULTIPLIERS = {
    "simple": 0.5,
    "medium": 1.0,
    "complex": 1.5,
    "epic": 2.0,
}

# Dynamic turn budget settings
DYNAMIC_BUDGET_START_RATIO = 0.8   # Start agents at 80% of their max_turns budget
DYNAMIC_BUDGET_MIN_TURNS = 15      # Minimum starting budget regardless of ratio
DYNAMIC_BUDGET_EXTEND_TURNS = 10   # Extra turns granted when agent reports FILES_CHANGED
DYNAMIC_BUDGET_BLOCKED_RATIO = 0.5  # Reduce remaining budget by 50% on RESULT: blocked

# Effort multipliers applied to dynamic turn budgets. Mirrors the --effort flag
# passed to the Claude CLI (see agent_runner.py): higher effort = more thoughtful
# turns, but each turn still counts the same against the budget. To prevent
# high-effort agents from running out of turns mid-task, scale the budget too.
EFFORT_BUDGET_MULTIPLIERS = {
    "low": 0.7,
    "default": 1.0,
    "high": 1.5,
    "xhigh": 2.0,
    "max": 2.5,
}

# Default model per role (overridden by dispatch_config per-role or per-complexity keys)
DEFAULT_ROLE_MODELS = {
    "developer": "opus",
    "tester": "sonnet",
    "security-reviewer": "opus",
    "planner": "opus",
    "evaluator": "sonnet",
    "frontend-designer": "opus",
    "integration-tester": "sonnet",
    "debugger": "opus",
    "code-reviewer": "sonnet",
    "qa-tester": "sonnet",
}

# Dev+Tester loop constants
MAX_DEV_TEST_CYCLES = 5
DEV_COMPACTION_THRESHOLD = 10    # turns before compacting developer
TESTER_COMPACTION_THRESHOLD = 6  # turns before compacting tester
NO_PROGRESS_LIMIT = 2            # consecutive no-change runs before blocking
MAX_CONTINUATIONS = 3            # auto-retries when developer runs out of turns/timeout

# Task #2604 wedge fix: hard caps for the FINAL-WARNING → kill → retry loop.
# Without these, an agent can spin for MAX_DEV_TEST_CYCLES × agent_duration
# (observed: 50 min, PID 343414, task #2602) with zero commits.
# PARALYSIS_CYCLE_HARD_CAP: max consecutive paralysis-killed cycles before
# the loop bails out as "no_progress" instead of spawning yet another agent.
# WEDGE_WALL_CLOCK_CAP_SECS: absolute wall-clock ceiling for the dev-test
# loop when no file changes have been produced across ALL cycles.
PARALYSIS_CYCLE_HARD_CAP: int = 3      # bail after 3 back-to-back paralysis cycles
WEDGE_WALL_CLOCK_CAP_SECS: int = 1800  # 30-min wall-clock cap (no-progress only)

# --- Early Termination ---

# Detect stuck agents mid-run and kill before wasting turns
# Escalating warnings: first warning -> final warning -> kill
# Aggressive thresholds to prevent analysis paralysis on large codebases.
# Agents were ignoring warnings and burning 12+ turns reading before kill.
# Kill at 6 turns (not 7) — combined with 1.25x scaling cap in agent_runner,
# max effective kill is 7 turns. Warn at 2 to catch early, final warn at 4.
# On paralysis retries, threshold drops further (see loops.py reduced_kill).
EARLY_TERM_WARN_TURNS = 12       # turns with no Edit/Write before first warning (loosened 2026-05-02 for 4.7 retest)
EARLY_TERM_FINAL_WARN_TURNS = 20 # turns with no Edit/Write before final warning (loosened 2026-05-02 for 4.7 retest)
EARLY_TERM_KILL_TURNS = 25       # turns with no Edit/Write before killing agent (loosened 2026-05-02 for 4.7 retest)
EARLY_TERM_STUCK_PHRASES = [
    "i am unable to",
    "i cannot",
    "i'm unable to",
    "i'm not able to",
    "i don't have access",
    "i do not have access",
    "this is beyond my capabilities",
    "i cannot complete this task",
    "i'm stuck",
    "i am stuck",
]
# Roles that legitimately produce no file changes (research/planning tasks)
EARLY_TERM_EXEMPT_ROLES = {
    "planner", "evaluator", "security-reviewer", "code-reviewer", "researcher",
}

# Monologue detection: consecutive assistant messages with zero tool calls
MONOLOGUE_THRESHOLD = 3      # terminate after this many text-only turns in a row
MONOLOGUE_EXEMPT_TURNS = 5   # do not trigger during first N turns (agent may be planning)

# --- System Prompt Caching ---

# Boundary marker separating static (globally cacheable) content from dynamic content.
# Everything BEFORE this marker in the system prompt can be cached across tasks.
# Everything AFTER contains per-task content (lessons, episodes, task description).
#
# Ported from Claude Code: nirholas-claude-code/src/constants/prompts.ts
# Static = _common.md + role prompt (same for every task with a given role)
# Dynamic = lessons, episodes, task description, language guidance, budget visibility
#
# WARNING: Do not remove or reorder this marker without updating cache logic in:
# - equipa/prompts.py (build_system_prompt, PromptResult)
# - equipa/agent_runner.py (build_cli_command)
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

# --- Budget Visibility ---

# Based on BATS research — budget visibility reduces wasted turns by ~40%
BUDGET_CHECK_INTERVAL = 5    # inject budget message every N turns
BUDGET_HALFWAY_THRESHOLD = 0.5   # fraction of budget used to trigger HALFWAY warning
BUDGET_CRITICAL_THRESHOLD = 0.75  # fraction of budget used to trigger CRITICAL warning

# --- Cost-Based Circuit Breaker ---

# Default limits per complexity tier (in USD). Configurable via dispatch_config "cost_limits".
COST_LIMITS = {
    "simple": 3.0,
    "medium": 5.0,
    "complex": 10.0,
    "epic": 20.0,
}
COST_ESTIMATE_PER_TURN = 0.15  # estimated cost per turn when actual cost is None

# Skill integrity verification: SHA-256 manifest of all prompt and skill files
SKILL_MANIFEST_FILE = Path(__file__).parent.parent / "skill_manifest.json"

# Pre-flight build check: detect build failures before agent starts
PREFLIGHT_TIMEOUT = 60  # max seconds to wait for build check
PREFLIGHT_SKIP_KEYWORDS = frozenset({
    "fix", "build", "compile", "broken", "compilation", "error",
})

# Auto-fix: dispatch debugger agent when preflight build check fails
AUTOFIX_MAX_DEBUGGER_CYCLES = 2   # max debugger attempts before escalating to planner
AUTOFIX_PLANNER_BUDGET = 15       # turns for planner to analyze build failure
AUTOFIX_DEBUGGER_BUDGET = 25      # turns for debugger to fix the build
AUTOFIX_COST_LIMIT = 8.0          # max USD to spend on auto-fix before giving up

# Manager mode constants (Phase 3)
MAX_MANAGER_ROUNDS = 3       # max plan-execute-evaluate rounds
MAX_TASKS_PER_PLAN = 8       # planner can't create more than this
MAX_FOLLOWUP_TASKS = 4       # evaluator can't create more than this per round

# Project codenames mapped to their local directories
# Populate via forge_config.json or dispatch_config.json project_dirs
PROJECT_DIRS = {}

# For sorting text-based priority values
PRIORITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# --- RLM Decompose Mode ---
RLM_TOKEN_THRESHOLD = 100_000  # context tokens to trigger REPL decomposition
RLM_MAX_SUB_QUERIES = 20       # max sub-queries per decompose session
RLM_SUB_QUERY_TIMEOUT = 120    # seconds per sub-query

# Default GitHub owner for --setup-repos
GITHUB_OWNER = ""

# --- Gitignore Templates ---

GITIGNORE_TEMPLATES = {
    "python": "\n".join([
        "__pycache__/", "*.pyc", "*.pyo", "*.egg-info/", "dist/", "build/",
        ".eggs/", "*.egg", ".venv/", "venv/", ".env", "*.db",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "dotnet": "\n".join([
        "bin/", "obj/", ".vs/", "*.user", "*.suo", "*.cache",
        "packages/", "*.nupkg", ".env", "*.db",
        "publish/", "**/Debug/", "**/Release/",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "node": "\n".join([
        "node_modules/", "dist/", ".env", "*.db",
        "npm-debug.log*", "yarn-error.log*",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
    "default": "\n".join([
        "__pycache__/", "*.pyc", "*.pyo",
        "bin/", "obj/", ".vs/", "*.user",
        "node_modules/", "dist/", "build/",
        ".env", "*.db", ".venv/", "venv/",
        "nul", "tmpclaude-*", "~$*", "desktop.ini", ".DS_Store", "Thumbs.db",
    ]),
}

# --- Manager cost limit ---
# Aggregate cost ceiling for run_manager_loop. Bootstrap-added 2026-05-03
# to unblock M4 test (#2113); enforcement still TODO in manager.py.
MANAGER_COST_LIMIT = 30.0

# --- Parallel Task Parsing ---
# Maximum number of IDs a single "start-end" range can expand to in
# parse_task_ids(). Prevents memory exhaustion DoS from inputs like
# "--tasks 1-999999999" (security finding EP-01).
MAX_TASK_RANGE = 1000

# --- Reviewer Finding Extraction ---
# Severity tokens + the suffixes that distinguish a real finding header from
# prose mentioning the word. Used by equipa.loops._extract_findings to scan
# reviewer output for actionable items. Two severity vocabularies:
#   - SECURITY: CRITICAL/HIGH (case-insensitive — security reviewers shout)
#   - CODE_REVIEW: Critical/Important (case-sensitive — avoids "critically
#     important" prose false-positives)
# Each map: severity-label -> tuple of suffix patterns to look for after the
# label. The match function checks that the label appears AND at least one
# suffix follows it on the same line.
SECURITY_SEVERITY_PATTERNS = {
    "CRITICAL": ("CRITICAL:", "CRITICAL**", "[CRITICAL]", "CRITICAL —", "CRITICAL -"),
    "HIGH": ("HIGH:", "HIGH**", "[HIGH]", "HIGH —", "HIGH -"),
}

CODE_REVIEW_SEVERITY_PATTERNS = {
    "Critical": ("Critical:", "**Critical", "[Critical]", "Critical —", "Critical -"),
    "Important": ("Important:", "**Important", "[Important]", "Important —", "Important -"),
}

# --- Prompt Budget Limits ---
# Caps on chars / words for slices injected into agent prompts. Centralised
# here so prompt budget is tunable in one place rather than scattered through
# loops.py / dispatch.py as bare literals.

# Tester context: cap for the per-cycle git diff appended to tester prompts.
# Diffs larger than this are truncated with a "[...chars omitted...]" footer.
TESTER_GIT_DIFF_MAX_CHARS = 8000

# Cross-attempt reflections: cap for the PREVIOUS ATTEMPTS block appended to a
# task description before re-dispatch. ~500 tokens ≈ ~2000 chars.
ATTEMPT_REFLECTIONS_MAX_CHARS = 2000

# Compaction consolidation: cap (in words) for cycle 2+ "Previous Attempts"
# block injected into the developer's extra_context.
COMPACTION_CONSOLIDATION_MAX_WORDS = 400

# Per-attempt summary sections: cap for FILES_CHANGED / BLOCKERS / REFLECTION
# snippets quoted in the cross-attempt reflection blob.
ATTEMPT_SECTION_TRIM_CHARS = 200

# Extracted security / code-review findings: cap for the description text
# stored as a developer lesson. Longer descriptions are truncated with an
# ellipsis (the truncation reserves 3 chars for "...").
FINDING_DESCRIPTION_MAX_CHARS = 500
