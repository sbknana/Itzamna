# Example roles (opt-in)

These are **optional** agent roles — they are *not* part of base Equipa's role set and are
not auto-loaded. They demonstrate the project-scoped role mechanism (see
[`docs/project-roles.md`](../../docs/project-roles.md)) and are useful starting points for
hardware / regulated-product work.

| Role | Purpose |
|------|---------|
| `design-engineer` | Architecture, trade studies, interface specs for safety-critical/regulated hardware+firmware. |
| `ip-analyst` | Freedom-to-operate, prior-art, patent-landscape analysis (attorney-grade rigor, **not** legal advice). |
| `regulatory-analyst` | Standards/compliance applicability, gap analysis, certification-path mapping (**not** legal/PE advice). |

## Using one

Copy the role into either location — the same precedence rules apply as any role:

- **One project only:** drop it in `<project_dir>/.equipa/roles/`. It is then private to that
  project and isolated from any same-named role in another project.
- **All projects:** drop it in base `prompts/`. It becomes a globally-available role.

Then dispatch it like any role:

```bash
python forge_orchestrator.py --task <ID> --role design-engineer -y
```

## Frontmatter

Each file carries optional frontmatter declaring its own config, so no `constants.py` edit is
needed:

```markdown
---
model: opus            # model tier; falls back to defaults if omitted
turns: 35              # turn budget
early_term_exempt: true  # report-writers shouldn't be killed for "no code by turn 3"
---
```

> The IP and regulatory roles target professional rigor but are **not** a lawyer, PE, or
> certification body. Each emits a mandatory not-legal-advice disclaimer and routes final
> determinations to licensed counsel / an accredited test lab / the relevant authority.
