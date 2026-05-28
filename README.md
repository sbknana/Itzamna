<h1 align="center">EQUIPA</h1>

<p align="center">
  <strong>Your AI development team.</strong>
</p>

<p align="center">
  <em>European Portuguese for "team" — a self-improving AI agent orchestrator that builds, reviews, tests, and secures your code.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/license-Apache%202.0-orange" alt="Apache 2.0">
  <img src="https://img.shields.io/badge/tests-518-success" alt="518 Tests">
</p>

---

Software development takes dedication, perseverance, and knowledge. No tool changes that. What EQUIPA does is multiply your productivity — it handles the repetitive, parallelizable parts of the workflow so you can focus on the hard problems that actually need a human brain.

You talk to Claude. Describe what you want in plain English — "fix the login bug", "add search to the dashboard", "run a security review" — and Claude handles the rest. It creates tasks, dispatches specialized AI agents, monitors their work, retries on failure, and reports results back to you.

Then it gets better at its job. A three-layer self-improvement system benchmarks agent performance, evolves prompts using genetic optimization, and auto-rolls back changes that hurt results. Your agents tomorrow are measurably better than your agents today.

This is a productivity tool, not a magic wand. You still need to review the output, understand your codebase, and make the real decisions. EQUIPA just means you're not doing the grunt work alone.

---

## What It Actually Does

```
You: "Build user authentication with Google OAuth"

EQUIPA:
  Planning: broke feature into 5 tasks with dependency graph
  Tasks 1-3 dispatched in parallel (no dependencies)
  Task 4 waiting on task 2 (needs routes)
  Task 5 waiting on all (integration tests)

  Developer agent -> wrote OAuth config, routes, middleware, UI
  Tester agent -> 8 integration tests, all passing
  Security reviewer -> flagged session token rotation issue
  Done. 3 tasks passed first try, 2 needed one retry.
```

You direct. EQUIPA executes. You review and decide what ships.

---

## Features

### Dev-Test Iteration Loop
Every coding task runs through a developer -> tester cycle. If tests fail, the developer gets the failure context and tries again — up to 5 cycles. No human babysitting required.

### Self-Improving Agents
Three systems work together:
- **ForgeSmith** extracts lessons from failures and tunes configuration
- **GEPA** evolves agent prompts through CMA-ES genetic optimization
- **SIMBA** synthesizes behavioral rules from recurring failure patterns

Bad changes get auto-rolled back when effectiveness scores drop below threshold.

### Semantic Memory
Lessons and past experiences are embedded as vectors (via Ollama) and retrieved by semantic similarity — not just keyword matching. When a task resembles something EQUIPA has seen before, relevant lessons get injected into the agent's context automatically. A knowledge graph tracks which lessons are most connected and influential, prioritizing the most useful ones via PageRank.

Falls back to keyword matching if Ollama is not available. Works fine either way.

### Cost-Based Model Routing
EQUIPA analyzes task descriptions and automatically routes simple tasks to cheaper models and complex tasks to more capable ones. A circuit breaker degrades gracefully when a model has consecutive failures. Manual model overrides still take priority — auto-routing only kicks in as a fallback.

### MCP Server
EQUIPA exposes itself as an MCP (Model Context Protocol) server. Any IDE that supports MCP — Claude Code, VS Code, Cursor, JetBrains — can dispatch tasks, check status, query lessons, and read project context without touching the CLI. Pure Python, JSON-RPC over stdio, zero dependencies.

```bash
# Register in any Claude Code session
claude mcp add equipa python3 /path/to/equipa/equipa/mcp_server.py
```

### 15 Specialized Agent Roles

| Role | What It Does |
|------|-------------|
| Developer | Writes code, navigates codebases, plans implementations |
| Tester | Writes and runs tests, validates developer output |
| Security Reviewer | Deep audit with 7 security skills, static analysis, variant analysis |
| Code Reviewer | Quality, patterns, best practices, architecture feedback |
| Debugger | Hypothesis-driven bug investigation, traces root causes |
| Planner | Breaks features into task lists with dependency graphs |
| Frontend Designer | UI/UX focused development |
| Evaluator | Assesses implementations against requirements |
| Integration Tester | Tests cross-boundary component interactions |
| QA Tester | End-to-end quality assurance |
| Researcher | Deep-dives into technologies and approaches |
| Economy Tester | Game economy balance testing |
| Multiplayer Tester | Multiplayer game flow testing |
| Story Tester | Narrative and story flow validation |
| World Builder | Game world and lore construction |

### Git Worktree Isolation
Parallel tasks (`--tasks N,M,...`) each get their own `git worktree` rooted at
`<repo>/.forge-worktrees/task-<id>` on a fresh `forge-task-<id>` branch.
Working-tree changes are filesystem-isolated — one task cannot see or break
another. Successful work merges back to the default branch automatically; the
worktree is torn down on completion. Single-task `--dev-test` mode does NOT
spawn a separate worktree — it commits on a `forge-task-<id>` branch in the
main checkout, so concurrent single-task dispatches against the same repo are
not supported.

Defensive invariants (task #2488, hardened in #2490):
- Worktree creation refuses to reuse an existing `forge-task-<id>` branch
  (`WorktreeBranchConflictError`) instead of silently committing onto the
  prior task's branch.
- The merge step raises `MissingTaskBranchError` (and logs an explicit ERROR
  in the parallel path) when the expected branch is missing, rather than
  reporting "no commits ahead of HEAD" — that silent skip is exactly how
  branch-reuse bugs masked themselves historically.
- Conflict-handling policy (unified across `git_ops.create_task_worktree`
  and `dispatch._create_isolation_worktrees`): on a branch-name conflict
  the default behaviour is to **raise** `WorktreeBranchConflictError` and
  route the task to the shared-dir fallback. Unmerged commit SHAs ahead
  of the default branch are logged before raising so the operator can
  recover via `git reflog`. The legacy "delete-and-retry" behaviour is
  only available when the caller passes `force=True` explicitly — never
  the default. Destroying unmerged commits silently is worse than the
  branch-reuse bug it was trying to mask (task #2488 regression
  hardening).

### Cost Controls
Per-task budgets scale by complexity (simple/medium/complex/epic). Agents that waste turns reading without writing get warned and then killed. You set the limits, EQUIPA enforces them.

### Language Detection
Detects your project language (Python, TypeScript, Go, C#, Java, Rust, JavaScript) and injects language-specific best practices. Agents write idiomatic code for your stack.

### Zero Dependencies
Pure Python standard library. No pip install, no virtualenv, no supply chain risk. Copy the folder, run the script. Works on any machine with Python 3.10+.

---

## Architecture

```
equipa/                    # 21 modules, ~11,500 lines
|-- cli.py                 # Entry point and argument parsing
|-- dispatch.py            # Task scanning, scoring, parallel dispatch
|-- loops.py               # Dev-test iteration loop
|-- agent_runner.py        # Agent subprocess management and streaming
|-- prompts.py             # System prompt construction with token budgeting
|-- monitoring.py          # Stuck detection, loop detection, cost tracking
|-- embeddings.py          # Ollama vector embeddings + cosine similarity
|-- routing.py             # Complexity scoring + cost-based model routing
|-- graph.py               # Knowledge graph, PageRank, community detection
|-- mcp_server.py          # MCP server (JSON-RPC over stdio)
|-- db.py                  # Database connection and schema management
|-- tasks.py               # Task fetching and project context
|-- lessons.py             # Episodic memory, Q-values, vector retrieval
|-- parsing.py             # Agent output parsing and compaction
|-- security.py            # Skill integrity verification
|-- preflight.py           # Build checking, dependency installation
|-- checkpoints.py         # Task checkpointing for crash recovery
|-- messages.py            # Inter-agent messaging
|-- reflexion.py           # Post-task self-reflection
|-- roles.py               # Role configuration and model selection
|-- constants.py           # Configuration constants
+-- git_ops.py             # Git operations and language detection
```

Self-improvement lives outside the package:
- `forgesmith.py` — Lesson extraction and configuration tuning
- `forgesmith_gepa.py` — CMA-ES prompt evolution with A/B testing
- `scripts/forgesmith_simba.py` — Behavioral rule synthesis from failure patterns
- `scripts/autoresearch_loop.py` — Nightly benchmarking and optimization
- `scripts/equipa_harness_sweep.py` — One-knob-at-a-time model/prompt tuning sweeps

---

## Quick Start

```bash
# Clone
git clone https://github.com/sbknana/equipa.git
cd equipa

# Setup
python equipa_setup.py

# Run a task
python forge_orchestrator.py --task 42 --dev-test -y

# Run tasks in parallel
python forge_orchestrator.py --tasks 42-50 --dev-test -y

# Auto-dispatch pending work
python forge_orchestrator.py --dispatch -y

# Start MCP server (for IDE integration)
python -m equipa.mcp_server

# Run self-improvement
python forgesmith.py --auto
```

### Requirements

- Python 3.10+ (no pip install needed)
- Claude Code CLI (`claude`) or Ollama for local LLM
- Git (for worktree isolation)

### Configuration

```bash
cp dispatch_config.example.json dispatch_config.json
cp forge_config.example.json forge_config.json
```

Key settings in `dispatch_config.json`:
- `model` — default model (sonnet/opus/haiku)
- `features.vector_memory` — semantic lesson retrieval via Ollama
- `features.auto_model_routing` — cost-based model selection
- `features.knowledge_graph` — PageRank lesson prioritization

---

## Honest Limitations

- **Agents still get stuck.** Complex tasks can trigger analysis paralysis. The early termination system catches this, but some tasks need multiple attempts.
- **Git merges are not perfect.** Parallel task merges occasionally need manual intervention.
- **Self-improvement needs data.** ForgeSmith needs 20-30 task completions before patterns emerge.
- **Tests required.** The dev-test loop only works if your project has a working test suite.
- **Context limits are real.** Very long tasks can exhaust the LLM context window. Checkpointing helps but does not eliminate the problem.
- **Vector memory needs Ollama.** Without it, falls back to keyword matching — still works, just less smart.

---

## Production Use

EQUIPA has been running in production since January 2026, building real software across multiple projects. It is not a demo or proof of concept — it is a tool we use every day.

---

## Documentation

- [Quick Start](docs/QUICKSTART.md) — Get running in 5 minutes
- [User Guide](docs/USER_GUIDE.md) — Day-to-day usage
- [Architecture](docs/ARCHITECTURE.md) — How the pieces fit together
- [API Reference](docs/API.md) — Module and function reference
- [Custom Agents](docs/CUSTOM_AGENTS.md) — Adding your own agent roles
- [Local LLM Support](docs/LOCAL_LLM.md) — Using Ollama instead of Claude
- [Deployment](docs/DEPLOYMENT.md) — Server and CI/CD setup
- [Contributing](docs/CONTRIBUTING.md) — How to contribute

## License

[Apache 2.0](LICENSE)

## Credits

Built by [Forgeborn](https://forgeborn.dev). Vibe coded with Claude.
