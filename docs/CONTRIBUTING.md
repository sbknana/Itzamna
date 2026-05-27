# Contributing to EQUIPA

## Table of Contents

- [Contributing to EQUIPA](#contributing-to-equipa)
  - [Welcome](#welcome)
  - [Development Setup](#development-setup)
  - [Code Style](#code-style)
  - [Making Changes](#making-changes)
    - [Branch Naming](#branch-naming)
    - [Commit Messages](#commit-messages)
    - [Working on the Codebase](#working-on-the-codebase)
  - [Testing](#testing)
    - [Running Tests](#running-tests)
- [Run everything](#run-everything)
- [Run a specific test file](#run-a-specific-test-file)
- [Run a specific test class](#run-a-specific-test-class)
- [Run with output visible](#run-with-output-visible)
    - [What to Test](#what-to-test)
    - [Writing Tests](#writing-tests)
  - [Pull Request Process](#pull-request-process)
    - [Before You Submit](#before-you-submit)
    - [PR Description](#pr-description)
    - [Review Expectations](#review-expectations)
    - [What Gets Merged Quickly](#what-gets-merged-quickly)
    - [What Takes Longer](#what-takes-longer)
  - [Issue Reporting](#issue-reporting)
    - [Bugs](#bugs)
    - [Feature Requests](#feature-requests)
    - [Known Limitations (So You Don't File Duplicates)](#known-limitations-so-you-dont-file-duplicates)
  - [Code of Conduct](#code-of-conduct)
  - [Related Documentation](#related-documentation)

## Welcome

Hey, glad you're here. EQUIPA is a multi-agent orchestrator that coordinates AI agents to write, test, review, and secure code. It's built in pure Python with zero dependencies, which means getting started is straightforward.

Whether you're fixing a typo, adding a feature, or improving an agent prompt — contributions are welcome. The codebase is honest about its rough edges (agents still get stuck, worktree merges sometimes need a human touch), and we'd love help smoothing those out.

---

## Development Setup

EQUIPA is pure Python stdlib. No virtualenv dance, no dependency hell.

**Requirements:**
- Python 3.10+
- Git
- SQLite (comes with Python)
- Claude CLI (for running agents, not required for development)

**Clone and go:**

```bash
git clone https://github.com/your-org/equipa.git
cd equipa
```

**Run the setup wizard:**

```bash
python equipa_setup.py
```

This walks you through database creation, config generation, and MCP server setup. It's interactive and tells you what it's doing at each step.

**Run the database migrations:**

```bash
python db_migrate.py
```

The migrator auto-detects your schema version and applies whatever's needed. It backs up your database first.

**Verify everything works:**

```bash
python -m pytest tests/ -x
```

There are 680+ tests. They should all pass. If they don't, open an issue — that's on us, not you.

---

## Code Style

EQUIPA doesn't use a formatter or linter in CI yet. That said, we follow some conventions:

- **Pure Python stdlib only.** No third-party imports in core modules (`equipa/`). If you need `requests`, you're doing it wrong — use `urllib`.
- **Type hints are appreciated** but not enforced. Add them where they help readability.
- **Docstrings:** We don't have a strict format. Just explain what the function does and why. Skip the obvious ones.
- **Naming:** `snake_case` for functions and variables. Classes are `PascalCase`. Constants are `UPPER_SNAKE`.
- **Keep functions focused.** If a function is doing three things, split it up.
- **No bare `except:` blocks.** Catch specific exceptions. We have tests (`test_narrowed_exceptions.py`) that verify this.
- **Security matters.** The bash security filter has 12+ regex checks for a reason. Don't weaken them without understanding why they exist. Read `test_bash_security.py` before touching `bash_security.py`.

---

## Making Changes

### Branch Naming

Use descriptive branch names with a prefix:

```
fix/worktree-merge-conflict-handling
feat/new-agent-role-documenter
test/add-mcp-health-edge-cases
docs/update-contributing-guide
refactor/simplify-episode-retrieval
```

### Commit Messages

Keep them short and useful:

```
fix: handle None cost in early termination check
feat: add PageRank boost to episode retrieval
test: cover circuit breaker half-open state
docs: clarify self-improvement timeline
```

Don't write essays in commit messages. If it needs explanation, put it in the PR description.

### Working on the Codebase

A few things worth knowing before you dive in:

- **The primary usage model is conversational.** Users talk to Claude in plain English, and Claude runs EQUIPA behind the scenes. The CLI (`equipa/cli.py`) exists for automation and scripting, but most users never touch it. Keep this in mind when designing features — if it requires memorizing CLI flags, think about how it would work conversationally instead.
- **The self-improvement loop (ForgeSmith + GEPA + SIMBA) is a closed system.** Changes to one part affect the others. Tread carefully.
- **Agent prompts live in files, not in code.** If you're changing agent behavior, you're probably editing a prompt file, not Python.
- **The knowledge graph uses PageRank.** Episodes that help many tasks score higher and get injected more often. This is in `equipa/graph.py`.

---

## Testing

### Running Tests

```bash
# Run everything
python -m pytest tests/ -x

# Run a specific test file
python -m pytest tests/test_bash_security.py -v

# Run a specific test class
python -m pytest tests/test_early_termination.py::TestCostBreaker -v

# Run with output visible
python -m pytest tests/ -x -s
```

### What to Test

- **If you touch `bash_security.py`:** Run `test_bash_security.py`. All of it. This is security-critical code ported from Claude Code's production security filters. False negatives here are bad.
- **If you change agent prompts:** Run `test_early_termination.py` — it checks that prompts contain required sections like bias-for-action and few-shot examples.
- **If you modify the database schema:** Run `test_db_migration_v5.py`, `test_db_migration_v7.py`, and `tools/benchmark_migrations.py`. Add a new migration test for your schema change.
- **If you change episode/lesson retrieval:** Run `test_episode_injection.py`, `test_lessons_injection.py`, `test_knowledge_graph.py`, and `test_graph_integration.py`.
- **If you modify the MCP server:** Run `test_mcp_server.py` and `test_mcp_health.py`.
- **If you add a new feature flag:** Add it to `DEFAULT_FEATURE_FLAGS` in `equipa/config.py` and add a test in `test_feature_flags.py`.

### Writing Tests

- Tests go in `tests/`.
- Use `pytest` fixtures. Check `tests/conftest.py` for existing ones.
- Use `tmp_path` for anything that touches the filesystem.
- Use `monkeypatch` to mock database connections — don't hit real databases in tests.
- Name tests descriptively: `test_circuit_breaker_recovers_after_60s` is better than `test_cb_3`.

---

## Pull Request Process

### Before You Submit

1. Run the full test suite: `python -m pytest tests/ -x`
2. Make sure you haven't introduced any bare `except:` blocks
3. If you added new functions to `equipa/`, check that `test_public_surface.py` still passes — we track the public API surface
4. If you changed prompts, verify the skill integrity: the tests in `test_skill_integrity.py` check file hashes

### PR Description

Tell us:

- **What** you changed
- **Why** you changed it
- **How** to test it (if it's not obvious from the test suite)
- **What could break** — be honest about risks

### Review Expectations

- Someone will review your PR. It might take a day or two.
- We'll check for: correctness, test coverage, security implications, and whether the change fits the project's direction.
- Prompt changes get extra scrutiny. Agent behavior is sensitive to wording, and small edits can cause surprising downstream effects.
- Don't take feedback personally. We're all trying to make the agents less dumb.

### What Gets Merged Quickly

- Bug fixes with tests
- Test coverage improvements
- Documentation fixes
- Clear, focused PRs that do one thing

### What Takes Longer

- New agent roles (needs design discussion)
- Changes to the self-improvement loop (ForgeSmith/GEPA/SIMBA)
- Anything that touches the bash security filter
- Large refactors

---

## Issue Reporting

### Bugs

Open an issue with:

- **What happened** — the actual behavior
- **What you expected** — the desired behavior
- **How to reproduce** — steps, commands, config
- **Environment** — Python version, OS, relevant config
- **Logs** — if an agent failed, include the checkpoint or log output

### Feature Requests

Open an issue with:

- **What you want** — describe the feature
- **Why it matters** — what problem does it solve?
- **How you'd use it** — concrete example

Don't overthink the format. A clear description beats a fancy template.

### Known Limitations (So You Don't File Duplicates)

These are things we know about and are working on:

- **Agents get stuck on complex tasks.** Analysis paralysis is real — agents sometimes spend 10 turns reading files instead of making changes. We have paralysis detection and retry logic, but it's not perfect.
- **Git worktree merges occasionally need manual intervention.** The isolation system works most of the time, but merge conflicts happen. We're still refining this.
- **Self-improvement needs 20-30 tasks before patterns emerge.** If you just installed EQUIPA, ForgeSmith won't have enough data to do anything useful yet. Give it time.
- **The tester role depends on your project having a working test suite.** If your tests are broken before EQUIPA touches them, the dev-test loop won't help much.
- **Early termination kills agents at 10 turns of reading.** This catches most paralysis cases, but some legitimately complex tasks need more exploration time. We err on the side of killing early.

---

## Code of Conduct

Be kind. Be constructive. Assume good intent.

We're building something that coordinates AI agents to write code. It's genuinely hard, it breaks in weird ways, and nobody has all the answers. If someone's approach is different from yours, that's a conversation, not a conflict.

Harassment, discrimination, and being a jerk are not tolerated. If someone makes you uncomfortable, reach out to a maintainer.

That's it. Go build something.
---

## Documentation Drift Check

CI runs `scripts/check_docs_drift.py` on every push to `master`/`main`
and on every pull request. The check fails if `README.md` or any
`docs/*.md` file references paths that don't exist, or if the claimed
module count or test-count badge has drifted too far from reality (see
the script's docstring for tolerances).

You can run the check locally:

<!-- drift-ignore -->
```
python scripts/check_docs_drift.py
```
<!-- /drift-ignore -->

If you need to reference a path that intentionally doesn't exist (an
example, a future file, a path that lives outside the repo), use one of
the two escape hatches:

1. Wrap the section in HTML comments:

   <!-- drift-ignore -->
   Paths in this block are not checked, e.g. `path/to/imaginary.py`.
   <!-- /drift-ignore -->

2. Use the special `sh-example` fenced code-block language tag (paths
   in such a block are ignored).

Both forms cause the drift checker to skip every reference inside
them. Prefer narrow scoping -- don't wrap the whole document just to
silence one false positive.

---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
