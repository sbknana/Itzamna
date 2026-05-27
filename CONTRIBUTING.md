# Contributing to EQUIPA

## Local setup

After cloning the repository, configure git to use the repo-tracked hooks
directory so the pre-commit checks run on your machine:

```bash
git config core.hooksPath .githooks
```

This enables the pre-commit hook that prevents EQUIPA agent-output artifacts
(`SECURITY-REVIEW-*.md`, `CODE-REVIEW-*.md`, `PLAN-*.md`,
`RETRY-IMPLEMENTATION-*.md`, `GEPA-EPISODE-CHECK-*.md`, `LEAN-CTX-PORT.md`)
from being committed at the repository root. These transient files belong in
`.equipa-artifacts/` (gitignored) instead.

The setting only needs to be applied once per clone. It is local to your
working copy and is not stored in the repository itself.

## Repository hygiene

- Agent-generated review/plan files live in `.equipa-artifacts/` (untracked).
- Do not commit files matching the patterns above at the repo root.
- Files inside subdirectories (e.g. `docs/PLAN-architecture.md`) are
  unaffected and may be committed normally.
