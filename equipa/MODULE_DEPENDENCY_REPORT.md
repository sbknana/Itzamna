# EQUIPA Module Dependency Report

> **Auto-generated — do not edit by hand.**
> Regenerate with `python scripts/gen_module_report.py`. The `docs-drift-check`
> CI job fails if this file is stale (see `scripts/check_docs_drift.py`).

**Modules analyzed:** 52 (equipa/ package, recursive)

## Summary

The `equipa/` package contains **52 Python modules** totaling **30,988 lines** of code across **11 dependency layers** (L0–L10). 27 module(s) use late (deferred) imports to break circular dependencies; there are no top-level circular imports.

## Module Dependency Table

| Module | Lines | Layer | Imports (equipa) | Late Imports | Exports |
|---|---:|:---:|---|---|---:|
| `abort_controller.py` | 218 | L0 | — | — | 3 |
| `bash_security.py` | 2347 | L0 | — | — | 4 |
| `classifier.py` | 167 | L0 | — | — | 3 |
| `constants.py` | 332 | L0 | — | — | 67 |
| `env_loader.py` | 98 | L0 | — | — | 1 |
| `heartbeat.py` | 855 | L0 | — | `config.py`, `config_versions.py`, `db.py`, `sessions.py` | 18 |
| `hooks/classifier_retry.py` | 105 | L0 | — | — | 3 |
| `hooks/dispatcher.py` | 151 | L0 | — | — | 5 |
| `hooks/security_review_gate.py` | 94 | L0 | — | — | 4 |
| `hooks/vacuous_pass.py` | 308 | L0 | — | `monitoring.py` | 3 |
| `initiative.py` | 763 | L0 | — | `security.py` | 19 |
| `integration_test.py` | 194 | L0 | — | `git_ops.py` | 5 |
| `mcp_health.py` | 112 | L0 | — | — | 5 |
| `plugins.py` | 69 | L0 | — | — | 3 |
| `scaffold.py` | 633 | L0 | — | `db.py` | 8 |
| `single_agent_guard.py` | 576 | L0 | — | `role_resolver.py` | 5 |
| `tool_result_storage.py` | 247 | L0 | — | — | 15 |
| `checkpoints.py` | 308 | L1 | `constants.py` | — | 8 |
| `config.py` | 177 | L1 | `constants.py` | — | 5 |
| `db.py` | 768 | L1 | `constants.py` | `config.py`, `output.py`, `prompts.py`, `tasks.py` | 13 |
| `git_ops.py` | 870 | L1 | `constants.py` | `tasks.py` | 15 |
| `hooks/__init__.py` | 427 | L1 | `hooks/dispatcher.py` | — | 17 |
| `output.py` | 292 | L1 | `constants.py` | `monitoring.py` | 7 |
| `parsing.py` | 908 | L1 | `constants.py` | `tool_result_storage.py` | 20 |
| `role_resolver.py` | 181 | L1 | `constants.py` | — | 7 |
| `routing.py` | 538 | L1 | `constants.py` | — | 27 |
| `security.py` | 137 | L1 | `constants.py` | — | 4 |
| `config_versions.py` | 480 | L2 | `constants.py`, `db.py` | — | 8 |
| `embeddings.py` | 233 | L2 | `config.py`, `constants.py` | `db.py`, `graph.py` | 6 |
| `flows.py` | 762 | L2 | `config.py`, `db.py` | `sessions.py` | 25 |
| `graph.py` | 321 | L2 | `db.py` | — | 7 |
| `initiative_runner.py` | 1367 | L2 | `initiative.py`, `security.py` | `db.py`, `dispatch.py`, `git_ops.py` | 29 |
| `lessons.py` | 635 | L2 | `config.py`, `constants.py`, `db.py`, `parsing.py` | `embeddings.py`, `graph.py`, `output.py`, `security.py` | 10 |
| `messages.py` | 124 | L2 | `db.py` | `security.py` | 4 |
| `monitoring.py` | 922 | L2 | `constants.py`, `hooks/__init__.py` | `parsing.py` | 12 |
| `rlm_decompose.py` | 687 | L2 | `output.py`, `parsing.py` | — | 20 |
| `roles.py` | 252 | L2 | `config.py`, `constants.py` | `output.py`, `role_resolver.py`, `routing.py`, `tasks.py` | 3 |
| `security_gate.py` | 375 | L2 | `git_ops.py` | `db.py` | 7 |
| `sessions.py` | 335 | L2 | `checkpoints.py`, `db.py` | `agent_runner.py` | 7 |
| `tasks.py` | 335 | L2 | `constants.py`, `db.py` | — | 8 |
| `templates.py` | 943 | L2 | `db.py` | `constants.py`, `embeddings.py` | 8 |
| `mcp_server.py` | 888 | L3 | `constants.py`, `tasks.py` | — | 12 |
| `prompts.py` | 799 | L3 | `config.py`, `constants.py`, `lessons.py`, `parsing.py`, `security.py` | `db.py`, `git_ops.py`, `graph.py`, `initiative.py`, `role_resolver.py` | 8 |
| `reflexion.py` | 143 | L3 | `db.py`, `lessons.py`, `output.py`, `parsing.py` | `agent_runner.py` | 4 |
| `agent_runner.py` | 1952 | L4 | `abort_controller.py`, `bash_security.py`, `checkpoints.py`, `config.py`, `constants.py`, `db.py`, `monitoring.py`, `output.py`, `parsing.py`, `prompts.py`, `security.py`, `tasks.py` | `cli.py`, `git_ops.py`, `rlm_decompose.py`, `role_resolver.py` | 21 |
| `preflight.py` | 311 | L5 | `agent_runner.py`, `constants.py`, `output.py` | `prompts.py`, `roles.py` | 2 |
| `loops.py` | 2269 | L6 | `agent_runner.py`, `checkpoints.py`, `classifier.py`, `config.py`, `constants.py`, `db.py`, `git_ops.py`, `hooks/__init__.py`, `messages.py`, `monitoring.py`, `output.py`, `parsing.py`, `preflight.py`, `prompts.py`, `roles.py`, `sessions.py`, `tasks.py` | `role_resolver.py` | 11 |
| `manager.py` | 312 | L7 | `agent_runner.py`, `constants.py`, `db.py`, `loops.py`, `output.py`, `prompts.py`, `roles.py`, `tasks.py` | — | 5 |
| `dispatch.py` | 2520 | L8 | `config.py`, `constants.py`, `db.py`, `git_ops.py`, `hooks/__init__.py`, `lessons.py`, `loops.py`, `manager.py`, `output.py`, `parsing.py`, `prompts.py`, `reflexion.py`, `roles.py`, `routing.py`, `security_gate.py`, `single_agent_guard.py`, `tasks.py` | `flows.py`, `initiative.py`, `scaffold.py` | 15 |
| `cli.py` | 2074 | L9 | `agent_runner.py`, `checkpoints.py`, `config.py`, `constants.py`, `db.py`, `dispatch.py`, `git_ops.py`, `hooks/__init__.py`, `lessons.py`, `loops.py`, `manager.py`, `mcp_server.py`, `monitoring.py`, `output.py`, `parsing.py`, `plugins.py`, `prompts.py`, `reflexion.py`, `roles.py`, `routing.py`, `security.py`, `security_gate.py`, `tasks.py`, `templates.py` | `config_versions.py`, `initiative_runner.py`, `role_resolver.py`, `scaffold.py`, `single_agent_guard.py` | 21 |
| `__init__.py` | 58 | L10 | `cli.py`, `dispatch.py`, `loops.py`, `manager.py`, `mcp_server.py`, `monitoring.py`, `prompts.py` | — | 14 |
| `__main__.py` | 16 | L10 | `cli.py` | — | 0 |

**Total:** 30,988 lines | 561 public exports

## Modules by Layer

Layer *N* is the longest chain of top-level (non-deferred) imports from a leaf module. Late imports are excluded so the graph stays acyclic.

- **L0**: `abort_controller.py`, `bash_security.py`, `classifier.py`, `constants.py`, `env_loader.py`, `heartbeat.py`, `hooks/classifier_retry.py`, `hooks/dispatcher.py`, `hooks/security_review_gate.py`, `hooks/vacuous_pass.py`, `initiative.py`, `integration_test.py`, `mcp_health.py`, `plugins.py`, `scaffold.py`, `single_agent_guard.py`, `tool_result_storage.py`
- **L1**: `checkpoints.py`, `config.py`, `db.py`, `git_ops.py`, `hooks/__init__.py`, `output.py`, `parsing.py`, `role_resolver.py`, `routing.py`, `security.py`
- **L2**: `config_versions.py`, `embeddings.py`, `flows.py`, `graph.py`, `initiative_runner.py`, `lessons.py`, `messages.py`, `monitoring.py`, `rlm_decompose.py`, `roles.py`, `security_gate.py`, `sessions.py`, `tasks.py`, `templates.py`
- **L3**: `mcp_server.py`, `prompts.py`, `reflexion.py`
- **L4**: `agent_runner.py`
- **L5**: `preflight.py`
- **L6**: `loops.py`
- **L7**: `manager.py`
- **L8**: `dispatch.py`
- **L9**: `cli.py`
- **L10**: `__init__.py`, `__main__.py`

## Late Import Inventory

27 module(s) defer intra-package imports inside function bodies to avoid import cycles:

| Module | Late Imports |
|---|---|
| `agent_runner.py` | `cli.py`, `git_ops.py`, `rlm_decompose.py`, `role_resolver.py` |
| `cli.py` | `config_versions.py`, `initiative_runner.py`, `role_resolver.py`, `scaffold.py`, `single_agent_guard.py` |
| `db.py` | `config.py`, `output.py`, `prompts.py`, `tasks.py` |
| `dispatch.py` | `flows.py`, `initiative.py`, `scaffold.py` |
| `embeddings.py` | `db.py`, `graph.py` |
| `flows.py` | `sessions.py` |
| `git_ops.py` | `tasks.py` |
| `heartbeat.py` | `config.py`, `config_versions.py`, `db.py`, `sessions.py` |
| `hooks/vacuous_pass.py` | `monitoring.py` |
| `initiative.py` | `security.py` |
| `initiative_runner.py` | `db.py`, `dispatch.py`, `git_ops.py` |
| `integration_test.py` | `git_ops.py` |
| `lessons.py` | `embeddings.py`, `graph.py`, `output.py`, `security.py` |
| `loops.py` | `role_resolver.py` |
| `messages.py` | `security.py` |
| `monitoring.py` | `parsing.py` |
| `output.py` | `monitoring.py` |
| `parsing.py` | `tool_result_storage.py` |
| `preflight.py` | `prompts.py`, `roles.py` |
| `prompts.py` | `db.py`, `git_ops.py`, `graph.py`, `initiative.py`, `role_resolver.py` |
| `reflexion.py` | `agent_runner.py` |
| `roles.py` | `output.py`, `role_resolver.py`, `routing.py`, `tasks.py` |
| `scaffold.py` | `db.py` |
| `security_gate.py` | `db.py` |
| `sessions.py` | `agent_runner.py` |
| `single_agent_guard.py` | `role_resolver.py` |
| `templates.py` | `constants.py`, `embeddings.py` |

## Import Count per Module

How many other `equipa` modules each module imports.

| Module | Top-level | Late | Total |
|---|---:|---:|---:|
| `__init__.py` | 7 | 0 | **7** |
| `__main__.py` | 1 | 0 | **1** |
| `abort_controller.py` | 0 | 0 | **0** |
| `agent_runner.py` | 12 | 4 | **16** |
| `bash_security.py` | 0 | 0 | **0** |
| `checkpoints.py` | 1 | 0 | **1** |
| `classifier.py` | 0 | 0 | **0** |
| `cli.py` | 24 | 5 | **29** |
| `config.py` | 1 | 0 | **1** |
| `config_versions.py` | 2 | 0 | **2** |
| `constants.py` | 0 | 0 | **0** |
| `db.py` | 1 | 4 | **5** |
| `dispatch.py` | 17 | 3 | **20** |
| `embeddings.py` | 2 | 2 | **4** |
| `env_loader.py` | 0 | 0 | **0** |
| `flows.py` | 2 | 1 | **3** |
| `git_ops.py` | 1 | 1 | **2** |
| `graph.py` | 1 | 0 | **1** |
| `heartbeat.py` | 0 | 4 | **4** |
| `hooks/__init__.py` | 1 | 0 | **1** |
| `hooks/classifier_retry.py` | 0 | 0 | **0** |
| `hooks/dispatcher.py` | 0 | 0 | **0** |
| `hooks/security_review_gate.py` | 0 | 0 | **0** |
| `hooks/vacuous_pass.py` | 0 | 1 | **1** |
| `initiative.py` | 0 | 1 | **1** |
| `initiative_runner.py` | 2 | 3 | **5** |
| `integration_test.py` | 0 | 1 | **1** |
| `lessons.py` | 4 | 4 | **8** |
| `loops.py` | 17 | 1 | **18** |
| `manager.py` | 8 | 0 | **8** |
| `mcp_health.py` | 0 | 0 | **0** |
| `mcp_server.py` | 2 | 0 | **2** |
| `messages.py` | 1 | 1 | **2** |
| `monitoring.py` | 2 | 1 | **3** |
| `output.py` | 1 | 1 | **2** |
| `parsing.py` | 1 | 1 | **2** |
| `plugins.py` | 0 | 0 | **0** |
| `preflight.py` | 3 | 2 | **5** |
| `prompts.py` | 5 | 5 | **10** |
| `reflexion.py` | 4 | 1 | **5** |
| `rlm_decompose.py` | 2 | 0 | **2** |
| `role_resolver.py` | 1 | 0 | **1** |
| `roles.py` | 2 | 4 | **6** |
| `routing.py` | 1 | 0 | **1** |
| `scaffold.py` | 0 | 1 | **1** |
| `security.py` | 1 | 0 | **1** |
| `security_gate.py` | 1 | 1 | **2** |
| `sessions.py` | 2 | 1 | **3** |
| `single_agent_guard.py` | 0 | 1 | **1** |
| `tasks.py` | 2 | 0 | **2** |
| `templates.py` | 1 | 2 | **3** |
| `tool_result_storage.py` | 0 | 0 | **0** |
