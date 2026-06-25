# Project-specific roles

Equipa resolves an agent role by checking a project's private overlay **before** the shared
base role set. This lets a project define its own roles without editing base Equipa, and keeps
two projects' same-named roles fully isolated.

## Where roles live (precedence)

For a role named `<role>` dispatched against a project, resolution is:

1. `<project_dir>/.equipa/roles/<role>.md` — the project's private overlay (**wins**)
2. `<equipa>/prompts/<role>.md` — the shared base role (fallback)

A project overlay **shadows** a base role of the same name, but only for that project.

## Isolation guarantee

Two projects can each define a `cybersecurity-engineer` (or any same-named role) with totally
different prompts and config. They never interfere:

- Resolution is keyed on the dispatching project's directory.
- It holds **no process-global mutable role state** — the resolver reads files fresh per call.

This is what makes per-project roles safe under `--auto-run`, which dispatches multiple
projects concurrently in a single process. Project A's role can never leak into project B.

## Self-describing roles (frontmatter)

A role `.md` may begin with optional frontmatter declaring its own config. Absent frontmatter,
behavior is identical to the legacy global role set — fully backward-compatible, no base
`constants.py` edit required.

```markdown
---
model: opus              # model tier (else dispatch-config / CLI / defaults)
turns: 35                # turn budget (else DEFAULT_ROLE_TURNS / DEFAULT_MAX_TURNS)
effort: high             # reasoning effort hint
early_term_exempt: true  # exempt from the no-file-change early-termination kill
skills: [security]       # role skill dirs
---
## CRITICAL: Bias for Action
...the prompt body begins here (frontmatter is stripped before use)...
```

### Config precedence

For `model` and `turns`, explicit operator overrides still win over a role's own frontmatter:

```
dispatch-config per-role/complexity  >  CLI --model/--max-turns  >  frontmatter  >  base defaults
```

For `early_term_exempt`, a role file's frontmatter value (when set) wins over the base
`EARLY_TERM_EXEMPT_ROLES` set.

## Adding a project role

1. `mkdir -p <project_dir>/.equipa/roles`
2. Write `<project_dir>/.equipa/roles/<role>.md` (optionally with frontmatter).
3. Dispatch it: `python forge_orchestrator.py --task <ID> --role <role> -y`.

No base-code changes, no merge conflicts on Equipa updates. See
[`examples/roles/`](../examples/roles/) for ready-to-copy starting points.

## Dispatching by stored role (`tasks.role`)

A task can carry its own dispatch role in the `tasks.role` column. When set, it
drives dispatch wherever `--role` is not given explicitly:

- **Single task:** `--task <ID>` (no `--role`) uses the task's role; an explicit
  `--role X` still overrides it.
- **Autonomous / scan modes:** `--auto-run` and `--project <ID>` dispatch each
  task with its stored role, so a project can fan work out to specialist or
  project-overlay roles instead of always running the dev/test loop.

Role-selection precedence: explicit `--role` → `tasks.role` → `developer`.

Report-writer roles (frontmatter `early_term_exempt: true`) skip the tester
phase when they produce no code diff — the same treatment as the built-in
reviewer roles.

Set a task's role via the DB / MCP, e.g.:

```sql
UPDATE tasks SET role = 'scilab-engineer' WHERE id = 100028;
```

The `tasks.role` column is added automatically on startup (idempotent, additive)
for existing databases, and is part of `schema.sql` for fresh installs.
