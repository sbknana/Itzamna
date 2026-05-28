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

1. Acquires an exclusive `flock` on `/tmp/athena-truth-sync.lock`. If
   another run already holds the lock, the script logs and quietly
   exits 0 (no concurrent runs).
2. Verifies the default branch is `master` or `main`. Any other branch
   aborts the run loudly.
3. Refuses to start when the local checkout has uncommitted changes.
4. Fast-forwards the local checkout to `origin/<default-branch>`.
5. Calls Athena (`generate-docs --scan --model quality`) to regenerate
   `docs/README.md`, `docs/ARCHITECTURE.md`, etc. from current code. A
   non-zero Athena exit is **fatal** -- the working tree is reset and
   the run aborts.
6. Asserts `git status --porcelain` mentions only `README.md` and
   `docs/`. Anything else is reverted with `git checkout --` and the
   run aborts (Athena's output domain is documentation only).
7. Runs `scripts/athena_sync_readme.py` to reconcile the root
   `README.md` against the Athena output and live counts. The helper
   applies a **markdown-only content allowlist** to the Athena-supplied
   Architecture block (see [Content allowlist](#content-allowlist)
   below). A poisoned Athena output causes a fail-closed abort before
   the splice writes to `README.md`.
8. Re-runs `scripts/check_docs_drift.py` to verify the fence is clean.
   If drift remains, the cron files a TheForge `open_question` (via a
   parameterized Python helper) and skips the push.
9. Creates a **GPG-signed** commit
   `[athena-audit] weekly README truth-sync (<DATE>)` using a dedicated
   bot identity (`EQUIPA truth-sync bot <athena-bot@forgeborn.dev>`)
   and pushes directly to the default branch. No PR. If signing fails
   for any reason (missing key, expired key, gpg unavailable) the run
   aborts -- there is no unsigned fallback.
10. If the diff is non-trivial (>50 line README delta), files an
    `open_question` summarizing what changed for human review.

## Content allowlist

Athena's output domain is **markdown documentation only**. Athena must
never emit code files, raw HTML (other than `<!-- comments -->`),
JavaScript, executable URIs, event handlers, or non-printable bytes.

`scripts/athena_sync_readme.py:validate_athena_content()` is the
fail-closed gate. It is a pure function `(content: str) -> (ok, reason)`
so each attack vector is unit-tested in isolation (see
`tests/test_athena_truth_sync.py`).

Rejected patterns:

* HTML tags matching `<\s*(script|iframe|object|embed|svg|style|link|
  meta|frame|frameset|applet|form|input|button|on[a-z]+)\b`.
* Any other inline HTML tag (e.g. `<a>`, `<div>`) outside of fenced
  code blocks. Markdown comments `<!-- ... -->` are permitted.
* `javascript:`, `vbscript:`, `data:text/html`, `file://` URIs inside
  markdown link/image targets.
* Event-handler attributes (`onclick=`, `onload=`, ...).
* Non-printable control characters other than `\n` and `\t`.
* Architecture blocks larger than 50KB (`MAX_ATHENA_BLOCK_BYTES`).

A rejected Athena block exits the sync script with rc=3, which the
bash runner translates into a fatal abort.

## Commit signing

The truth-sync cron commits with `git commit -S` against a dedicated
GPG key. The key is **never shared** with developer commits.

* Identity: `EQUIPA truth-sync bot <athena-bot@forgeborn.dev>`
* Fingerprint: documented at
  `/srv/forge-share/AI_Stuff/Athena/keys/athena-bot.fingerprint` on
  Claudinator.
* Imported under user `user`'s GPG keyring (`gpg --list-secret-keys
  athena-bot@forgeborn.dev` must list it; the cron inherits `user`'s
  environment).

The script does **not** generate the key. Generation/installation is
operator-driven:

```bash
# One-time, on Claudinator, as `user`:
gpg --quick-gen-key 'EQUIPA truth-sync bot <athena-bot@forgeborn.dev>' \
    ed25519 sign 2y
gpg --list-secret-keys --keyid-format=long athena-bot@forgeborn.dev \
    | tee /srv/forge-share/AI_Stuff/Athena/keys/athena-bot.fingerprint

# Verify git can sign:
echo "test" | gpg --clearsign -u athena-bot@forgeborn.dev >/dev/null

# Upload the public key to GitHub under the bot account.
gpg --armor --export athena-bot@forgeborn.dev | xclip -selection clipboard
```

If `gpg` is unavailable, the key is expired, or the keyring is
inaccessible, the cron aborts with a clear log line -- it never falls
back to an unsigned commit.

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
ATHENA_LOCKFILE=/tmp/athena-test.lock \
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
| `ATHENA_SIGNING_KEY`     | `athena-bot@forgeborn.dev`                                |
| `ATHENA_BOT_NAME`        | `EQUIPA truth-sync bot`                                   |
| `ATHENA_BOT_EMAIL`       | `athena-bot@forgeborn.dev`                                |
| `ATHENA_LOCKFILE`        | `/tmp/athena-truth-sync.lock`                             |
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
# 1. Ensure the logfile is writable by `user` and has restrictive mode.
#    0640 keeps the log readable by user+group but not world (Athena
#    drift output can contain repo file paths and reviewer summaries).
sudo install -m 0640 -o user -g user /dev/null /var/log/athena-truth-sync.log

# 2. Install logrotate so the log does not grow unbounded.
sudo tee /etc/logrotate.d/athena-truth-sync <<'EOF'
/var/log/athena-truth-sync.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0640 user user
}
EOF

# 3. Verify the GPG bot key is installed (see "Commit signing" above).
gpg --list-secret-keys athena-bot@forgeborn.dev || exit 1

# 4. Add the cron entry.
crontab -l > /tmp/cron.bak
( crontab -l; \
  echo '0 5 * * 0 /srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh >> /var/log/athena-truth-sync.log 2>&1' \
) | crontab -

# 5. Verify.
crontab -l | grep athena_truth_sync
```

If `sudo` is unavailable on the cron host, redirect the log into `/tmp`
with restrictive umask (`umask 027 && touch /tmp/athena-truth-sync.log`)
instead.

## Safety guards

* **Single-instance flock** -- `/tmp/athena-truth-sync.lock` prevents
  overlapping runs. The script re-execs itself under `flock -n` and
  exits 0 quietly if the lock is already held.
* **Default-branch allowlist** -- only `master` and `main` are accepted;
  any other value aborts the run.
* **Clean-checkout guard** -- the run aborts if `git diff --quiet HEAD`
  reports uncommitted changes.
* **Drift-checker preflight** -- `check_docs_drift.py --self-test`
  must succeed before the run continues.
* **Markdown-only content allowlist** -- `validate_athena_content()`
  rejects any HTML/JS injection vector before the README splice.
* **Preserve-boundary check** -- the sync helper refuses to operate on
  a README that has lost its `## What It Actually Does` heading.
* **Athena exit is fatal** -- a non-zero Athena exit resets the working
  tree and aborts; no partial commits.
* **Scope guard** -- `git status --porcelain` is asserted to mention
  only `README.md` and `docs/`. Anything else is reverted and the
  run aborts.
* **Post-sync drift verification** -- if drift remains after the sync
  the cron files an `open_question` and skips the push.
* **GPG-signed commits** -- mandatory; no unsigned fallback.
* **Parameterized SQL** -- `open_questions` inserts go through
  `scripts/athena_open_question.py` (sqlite3 placeholders), never
  through shell-quoted strings.
* **No PR review** -- the workflow is "Athena is authoritative for the
  machine-verifiable sections." Human review happens via the
  `open_question` filed on non-trivial repairs.

## How to debug a failed run

The cron tees stdout/stderr to `/var/log/athena-truth-sync.log` (or
`/tmp/athena-truth-sync.log` when no sudo). Each line is prefixed with
`[athena-truth-sync <UTC timestamp>]`. The most common failure modes:

* `another truth-sync run holds /tmp/athena-truth-sync.lock` -- a
  prior run is still in flight. Investigate before forcing a manual run.
* `uncommitted changes detected` -- the local checkout has dirty
  files. Stash or revert before re-running.
* `unexpected default branch` -- the repo HEAD is not `master`/`main`.
  Verify the upstream rename was not partial.
* `scripts/check_docs_drift.py --self-test failed` -- the drift
  checker has a syntax error or missing dependency; fix the checker
  first.
* `Athena output rejected by content allowlist` -- Athena emitted
  HTML / JS / oversize content. Inspect `docs/README.md` for the
  payload and investigate the Athena run.
* `Athena produced unexpected file changes` -- Athena wrote outside
  `README.md`/`docs/`. Inspect git status; this is a strong signal
  that the Athena CLI was misconfigured.
* `signed commit failed` -- the GPG bot key is missing, expired, or
  the GPG agent is not reachable. Verify with
  `echo test | gpg --clearsign -u athena-bot@forgeborn.dev`.
* `drift remains after sync` -- the README references files that no
  longer exist or claims that don't match reality. Check the TheForge
  `open_questions` table for the captured drift output, then either
  patch by hand or extend the sync helper.
