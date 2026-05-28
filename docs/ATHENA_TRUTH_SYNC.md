# Athena Weekly Truth-Sync

<!-- drift-ignore -->
<!--
  All paths in this document are written relative to the repo root (e.g.
  `scripts/athena_truth_sync.sh`), not relative to docs/.  The drift
  checker resolves backtick paths against the containing doc's directory,
  which would falsely flag every path on this page.  The drift-ignore
  marker above tells check_docs_drift.py to skip path validation for the
  remainder of this file.
-->

EQUIPA's repo-consistency workflow has four layers; this document covers
Layer 2.

| Layer | What                              | Where it lives                       |
|-------|-----------------------------------|--------------------------------------|
| 0     | Hygiene (agent artifacts off root) | `.equipa-artifacts/`, gitignore      |
| 1     | Drift fence (CI)                   | `scripts/check_docs_drift.py` + GH Action |
| **2** | **Weekly Athena truth-sync (cron)** | **`scripts/athena_truth_sync.sh`**   |
| 3     | Manual `/repo-audit` skill         | `skills/repo-audit/`                 |

Layer 1 detects drift; Layer 2 fixes it.

## What the cron does

Every Sunday at 05:00 UTC, the cron runs `scripts/athena_truth_sync.sh`,
which:

1. Fast-forwards the local checkout to `origin/<default-branch>`.
2. Calls Athena (`generate-docs --scan --model quality`) to regenerate
   `docs/README.md`, `docs/ARCHITECTURE.md`, etc. from current code.
3. Runs `scripts/athena_sync_readme.py` to reconcile the root `README.md`
   against the Athena output and live counts:
   * Badges (`tests-N`, `modules-N`, `lines-N`) are rewritten from
     `pytest --collect-only -q`, an `equipa/**/*.py` file count, and a
     stdlib line count.
   * The `## Architecture` section is replaced with Athena's regenerated
     copy when present.
   * The hand-written intro (lines 1-29 / everything up to the first
     `## What It Actually Does` heading) and every section after
     Architecture (Features, Quick Start, Documentation, License,
     Credits, ...) are **preserved verbatim**.
4. Re-runs `scripts/check_docs_drift.py` to verify the fence is clean.
   If drift remains, the cron files a TheForge `open_question` and
   skips the push.
5. Commits `[athena-audit] weekly README truth-sync (<DATE>)` and pushes
   directly to the default branch. No PR.
6. If the diff is non-trivial (>50 line README delta), files an
   `open_question` summarizing what changed for human review.

## When it runs

```cron
0 5 * * 0 /srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh \
    >> /var/log/athena-truth-sync.log 2>&1
```

Installed on Claudinator as user `user`. The schedule is weekly (not
daily) because drift accumulates slowly and noisy cron commits add no
value.

## Manual force-run

```bash
/srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh
```

The script reads its configuration from environment variables, so a
dry-run against a scratch checkout looks like:

```bash
EQUIPA_REPO=/tmp/equipa-test \
ATHENA_DIR=/srv/forge-share/AI_Stuff/Athena \
THEFORGE_DB=/tmp/forge.db \
/srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh
```

Override env vars:

| Variable                 | Default                                                   |
|--------------------------|-----------------------------------------------------------|
| `EQUIPA_REPO`            | `/srv/forge-share/AI_Stuff/Equipa-repo`                   |
| `ATHENA_DIR`             | `/srv/forge-share/AI_Stuff/Athena`                        |
| `ATHENA_CLI`             | `${ATHENA_DIR}/dist/cli.js`                               |
| `ATHENA_MODEL`           | `quality`                                                 |
| `PYTHON_BIN`             | `python3`                                                 |
| `THEFORGE_DB`            | `/srv/forge-share/AI_Stuff/TheForge/theforge.db`          |
| `EQUIPA_PROJECT_ID`      | `23`                                                      |
| `BADGE_DELTA_THRESHOLD`  | `50` (lines changed in README to count as "non-trivial")  |

## Disabling temporarily

Comment out the crontab line on Claudinator:

```bash
crontab -e
# Prefix the line with `#`:
# 0 5 * * 0 /srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh ...
```

To remove it entirely:

```bash
crontab -l | grep -v athena_truth_sync | crontab -
crontab -l | grep athena_truth_sync   # should print nothing
```

## Installation (one-time)

```bash
# 1. Ensure the logfile is writable by `user`.
sudo touch /var/log/athena-truth-sync.log
sudo chown user:user /var/log/athena-truth-sync.log

# 2. Add the cron entry.
crontab -l > /tmp/cron.bak
( crontab -l; \
  echo '0 5 * * 0 /srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh >> /var/log/athena-truth-sync.log 2>&1' \
) | crontab -

# 3. Verify.
crontab -l | grep athena_truth_sync
```

If `sudo` is unavailable on the cron host, redirect the log into `/tmp`
instead (`>> /tmp/athena-truth-sync.log 2>&1`).

## Safety guards

* **Drift-checker preflight** -- if `scripts/check_docs_drift.py` is
  missing or fails to import, the cron aborts before making any
  changes. "Better silent than wrong."
* **Preserve-boundary check** -- the sync helper refuses to operate on
  a README that has lost its `## What It Actually Does` heading.
* **Post-sync drift verification** -- if drift remains after the sync
  the cron files an `open_question` and skips the push.
* **No PR review** -- the workflow is "Athena is authoritative for the
  machine-verifiable sections." Human review happens via the
  `open_question` filed on non-trivial repairs.

## How to debug a failed run

The cron tees stdout/stderr to `/var/log/athena-truth-sync.log` (or
`/tmp/athena-truth-sync.log` when no sudo). Each line is prefixed with
`[athena-truth-sync <UTC timestamp>]`. The most common failure modes:

* `scripts/check_docs_drift.py failed to import` -- the drift checker
  has a syntax error or missing dependency; fix the checker first.
* `drift remains after sync` -- the README references files that no
  longer exist or claims that don't match reality. Check the TheForge
  `open_questions` table for the captured drift output, then either
  patch by hand or extend the sync helper.
* `Athena exited non-zero` -- Athena's `generate-docs` failed (commonly
  `ANTHROPIC_API_KEY` not in scope for the cron's environment); the
  cron continues with the sync against any pre-existing
  `docs/README.md` and the live counts. Re-run after fixing the env.
