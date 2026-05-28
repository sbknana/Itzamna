#!/usr/bin/env bash
# Athena weekly truth-sync cron for the EQUIPA repository
# (tasks #2480 / #2483 hardening).
#
# Layer 2 of the repo-consistency workflow:
#   Layer 0 (#2475): hygiene -- agent artifacts routed to .equipa-artifacts/.
#   Layer 1 (#2477): drift fence GitHub Action.
#   Layer 2 (THIS): weekly Athena regen + README truth-sync (this script).
#   Layer 3:        /repo-audit skill for manual on-demand version.
#
# What it does (in order):
#   1. Acquire an exclusive flock so concurrent runs do not overlap.
#   2. cd to the Equipa-repo working tree (master|main only).
#   3. Refuse to operate if the working tree is dirty.
#   4. git fetch + git pull origin <default-branch>.
#   5. Run Athena `generate-docs --scan --model quality` against the repo.
#      Athena non-zero exit is FATAL -- working tree is reset, run aborts.
#   6. Reject the run if Athena modified any files outside README.md or docs/.
#   7. Sync the Athena-regenerated Architecture section + live counts
#      (tests / modules / lines) into the ROOT README.md, preserving the
#      hand-written intro. The Python sync helper applies a markdown-only
#      content allowlist; a poisoned Athena output causes a fail-closed
#      abort BEFORE the splice writes to README.md.
#   8. Run scripts/check_docs_drift.py; abort the push if drift remains.
#   9. SIGNED commit `[athena-audit] weekly README truth-sync (<DATE>)`
#      via a dedicated bot key. Unsigned commits are NOT permitted --
#      a missing/broken signing key fails the run, never falls back.
#  10. Push to origin/<default-branch>.
#  11. If non-trivial drift was repaired, INSERT a TheForge open_question.
#
# Safety guards (added in task #2483 beyond the original #2480 set):
#   * Single-instance flock at /tmp/athena-truth-sync.lock.
#   * Default-branch allowlist: only master|main is accepted.
#   * Clean-checkout guard: uncommitted changes abort the run.
#   * Athena exit code is fatal -- working tree is reset, never swallowed.
#   * `git status --porcelain` is asserted to mention only README.md and
#     docs/; any other path causes `git checkout --` to revert it.
#   * GPG-signed commit; key fingerprint documented in
#     docs/ATHENA_TRUTH_SYNC.md. No unsigned fallback.
#   * Drift checker self-test via `--self-test` flag (no inline
#     `python3 -c "..."` interpolation).
#   * open_question insert via parameterized Python helper, not the
#     bash `sqlite3` CLI.
#
# Expected schedule (Claudinator, user `user`):
#   0 5 * * 0 /srv/forge-share/AI_Stuff/Equipa-repo/scripts/athena_truth_sync.sh \
#       >> /var/log/athena-truth-sync.log 2>&1

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration -- override via environment for testing.
# ---------------------------------------------------------------------------
EQUIPA_REPO="${EQUIPA_REPO:-/srv/forge-share/AI_Stuff/Equipa-repo}"
ATHENA_DIR="${ATHENA_DIR:-/srv/forge-share/AI_Stuff/Athena}"
ATHENA_CLI="${ATHENA_CLI:-${ATHENA_DIR}/dist/cli.js}"
ATHENA_MODEL="${ATHENA_MODEL:-quality}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Commit signing -- mandatory. The cron host must have the bot key
# imported under this fingerprint. See docs/ATHENA_TRUTH_SYNC.md for
# key generation and installation. Fail-closed if signing fails.
ATHENA_SIGNING_KEY="${ATHENA_SIGNING_KEY:-athena-bot@forgeborn.dev}"
ATHENA_BOT_NAME="${ATHENA_BOT_NAME:-EQUIPA truth-sync bot}"
ATHENA_BOT_EMAIL="${ATHENA_BOT_EMAIL:-athena-bot@forgeborn.dev}"

# Concurrency lock. flock -n exits cleanly if another run holds it.
ATHENA_LOCKFILE="${ATHENA_LOCKFILE:-/tmp/athena-truth-sync.lock}"

# Drift severity thresholds for "non-trivial" repair (open_question filing).
PATH_CORRECTION_THRESHOLD="${PATH_CORRECTION_THRESHOLD:-5}"
MODULE_DELTA_THRESHOLD="${MODULE_DELTA_THRESHOLD:-10}"
BADGE_DELTA_THRESHOLD="${BADGE_DELTA_THRESHOLD:-50}"

# TheForge for open_questions. Optional; absence is logged but not fatal.
THEFORGE_DB="${THEFORGE_DB:-/srv/forge-share/AI_Stuff/TheForge/theforge.db}"
EQUIPA_PROJECT_ID="${EQUIPA_PROJECT_ID:-23}"

# ---------------------------------------------------------------------------
# Logging helpers.
# ---------------------------------------------------------------------------
log() { printf '[athena-truth-sync %s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
err() { printf '[athena-truth-sync %s] ERROR: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# ---------------------------------------------------------------------------
# Default-branch detection (master / main agnostic).
# Falls back to "master" because that is the current Equipa default and a
# wrong-branch pull is loud (it errors), not silent corruption.
# Task #2483 S3: returns ONLY master|main; anything else aborts the run.
# ---------------------------------------------------------------------------
get_default_branch() {
    local branch
    branch="$(git -C "$EQUIPA_REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [[ -n "$branch" ]]; then
        branch="${branch#origin/}"
    fi
    if [[ -z "$branch" || "$branch" == "(unknown)" ]]; then
        branch="$(git -C "$EQUIPA_REPO" remote show origin 2>/dev/null \
            | awk '/HEAD branch/ {print $NF}')"
    fi
    if [[ -z "$branch" || "$branch" == "(unknown)" ]]; then
        if git -C "$EQUIPA_REPO" rev-parse --verify main >/dev/null 2>&1; then
            branch="main"
        else
            branch="master"
        fi
    fi

    case "$branch" in
        master|main) ;;
        *) err "unexpected default branch '$branch' -- aborting"; exit 1 ;;
    esac
    printf '%s\n' "$branch"
}

# ---------------------------------------------------------------------------
# Open-question filing via parameterized Python helper (task #2483 S4).
# Replaces the bash sqlite3 CLI invocation that interpolated user-supplied
# text into a SQL string. Best-effort; never fails the run.
# ---------------------------------------------------------------------------
file_open_question() {
    local summary="$1"
    if [[ ! -f "$THEFORGE_DB" ]]; then
        log "TheForge DB not present at $THEFORGE_DB -- skipping open_question filing."
        return 0
    fi

    local helper="$EQUIPA_REPO/scripts/athena_open_question.py"
    if [[ ! -f "$helper" ]]; then
        log "open_question helper not found at $helper -- skipping filing."
        return 0
    fi

    if ! "$PYTHON_BIN" "$helper" \
            --db "$THEFORGE_DB" \
            --project-id "$EQUIPA_PROJECT_ID" \
            --question "Athena truth-sync repaired non-trivial drift -- human review requested" \
            --context "$summary"; then
        log "open_question insert failed (non-fatal)"
    fi
}

# ---------------------------------------------------------------------------
# Safety preflight: drift checker must exist and import cleanly.
# Task #2483 S2: invoke the script's own --self-test flag rather than
# shelling out a multi-line `python3 -c` string into the interpreter.
# ---------------------------------------------------------------------------
verify_drift_checker() {
    local checker="$EQUIPA_REPO/scripts/check_docs_drift.py"
    if [[ ! -f "$checker" ]]; then
        err "scripts/check_docs_drift.py is missing -- refusing to sync."
        return 1
    fi
    if ! "$PYTHON_BIN" "$checker" --self-test >/dev/null 2>&1; then
        err "scripts/check_docs_drift.py --self-test failed -- refusing to sync."
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Reset working tree to HEAD across README.md / docs/.
# Used to undo Athena partial output on failure.
# ---------------------------------------------------------------------------
reset_athena_outputs() {
    git -C "$EQUIPA_REPO" checkout -- README.md docs/ 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Detect unexpected file changes after Athena runs.
# Athena's writeable domain is README.md and docs/ only. Anything else is
# reverted in-place and the run aborts.
# ---------------------------------------------------------------------------
assert_only_expected_paths_changed() {
    local porcelain
    porcelain="$(git -C "$EQUIPA_REPO" status --porcelain)"
    if [[ -z "$porcelain" ]]; then
        return 0
    fi

    local unexpected=""
    while IFS= read -r row; do
        local path="${row:3}"
        case "$path" in
            README.md|docs/*) ;;
            "") ;;
            *) unexpected+="$path"$'\n' ;;
        esac
    done <<< "$porcelain"

    if [[ -n "$unexpected" ]]; then
        err "Athena produced unexpected file changes outside README.md / docs/:"
        printf '%s' "$unexpected" >&2
        while IFS= read -r path; do
            [[ -z "$path" ]] && continue
            git -C "$EQUIPA_REPO" checkout -- "$path" 2>/dev/null || true
        done <<< "$unexpected"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------
main() {
    log "starting Athena truth-sync run"

    # ---- Single-instance flock (task #2483 S5) -------------------------------
    # We re-exec ourselves under flock so subsequent code is guaranteed
    # to hold the lock. ``flock -n`` exits 1 if another run holds it;
    # we treat that as a clean no-op exit.
    if [[ -z "${ATHENA_TRUTH_SYNC_LOCKED:-}" ]]; then
        if ! command -v flock >/dev/null 2>&1; then
            err "flock not on PATH -- refusing to proceed without concurrency guard."
            exit 1
        fi
        export ATHENA_TRUTH_SYNC_LOCKED=1
        exec flock -n "$ATHENA_LOCKFILE" "$0" "$@" \
            || { log "another truth-sync run holds $ATHENA_LOCKFILE -- skipping."; exit 0; }
    fi

    if [[ ! -d "$EQUIPA_REPO/.git" ]]; then
        err "EQUIPA_REPO=$EQUIPA_REPO is not a git checkout -- aborting."
        exit 1
    fi

    cd "$EQUIPA_REPO"

    if ! verify_drift_checker; then
        err "safety guard failed -- aborting before any code/doc changes."
        exit 1
    fi

    # ---- Clean-checkout guard (task #2483 S3, tightened #2485 S2) ------------
    # Reject BOTH uncommitted tracked-file changes AND any untracked files.
    # An untracked file in the working tree could be silently incorporated
    # by Athena's regeneration or by the unexpected-paths guard's diff, so
    # we refuse to operate unless the worktree is fully clean.
    if ! git diff --quiet HEAD || [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
        err "uncommitted/untracked changes detected -- aborting"
        exit 1
    fi

    local default_branch
    default_branch="$(get_default_branch)"
    log "default branch detected: $default_branch"

    log "fetching origin"
    git fetch origin "$default_branch" --quiet
    log "checking out $default_branch and fast-forwarding"
    git checkout --quiet "$default_branch"
    git pull --ff-only origin "$default_branch"

    # ---- Athena regeneration --------------------------------------------------
    # Task #2483 S6: Athena exit is FATAL. Reset any partial output and abort.
    if [[ -f "$ATHENA_CLI" ]]; then
        log "running Athena: $ATHENA_CLI generate-docs --scan --model $ATHENA_MODEL"
        if ! ( cd "$ATHENA_DIR" && node "$ATHENA_CLI" generate-docs \
                --project "$EQUIPA_REPO" \
                --scan \
                --model "$ATHENA_MODEL" ); then
            err "Athena exited non-zero -- resetting working tree and aborting."
            reset_athena_outputs
            exit 1
        fi
    else
        log "Athena CLI not found at $ATHENA_CLI -- skipping regeneration, syncing live counts only."
    fi

    # ---- Unexpected-paths guard (task #2483 S7) ------------------------------
    if ! assert_only_expected_paths_changed; then
        err "Athena scope violation -- aborting."
        exit 1
    fi

    # ---- README sync ----------------------------------------------------------
    local athena_readme="$EQUIPA_REPO/docs/README.md"
    local sync_args=( "$EQUIPA_REPO/scripts/athena_sync_readme.py"
                      --repo-root "$EQUIPA_REPO" )
    if [[ -f "$athena_readme" ]]; then
        sync_args+=( --athena-readme "$athena_readme" )
    fi

    log "syncing root README.md"
    local sync_rc=0
    "$PYTHON_BIN" "${sync_args[@]}" || sync_rc=$?
    if (( sync_rc == 3 )); then
        err "Athena output rejected by content allowlist (markdown-only) -- aborting."
        reset_athena_outputs
        exit 1
    elif (( sync_rc != 0 )); then
        err "README sync failed (rc=$sync_rc) -- aborting commit/push."
        reset_athena_outputs
        exit 1
    fi

    # ---- Drift verification --------------------------------------------------
    log "running drift verification"
    local drift_log
    drift_log="$(mktemp)"
    if "$PYTHON_BIN" "$EQUIPA_REPO/scripts/check_docs_drift.py" \
        --repo-root "$EQUIPA_REPO" --skip-pytest > "$drift_log" 2>&1; then
        log "drift check clean"
    else
        err "drift remains after sync -- skipping commit/push."
        cat "$drift_log" >&2 || true
        local context
        context="$(head -c 4000 "$drift_log" 2>/dev/null || true)"
        file_open_question "Drift persisted after weekly Athena sync. Output:\n${context}"
        rm -f "$drift_log"
        exit 1
    fi
    rm -f "$drift_log"

    # ---- Commit/push ---------------------------------------------------------
    if git diff --quiet --exit-code -- README.md docs/; then
        log "no changes to commit -- quiet exit."
        exit 0
    fi

    local diff_stat
    diff_stat="$(git diff --shortstat -- README.md docs/ || true)"
    log "diff to commit: $diff_stat"

    git add -- README.md docs/
    local date_str
    date_str="$(date -u '+%Y-%m-%d')"

    # ---- Signed commit (task #2483 S1) ---------------------------------------
    # Mandatory. The cron host must have a GPG key matching
    # ATHENA_SIGNING_KEY in the gpg keyring AND configured for git
    # (e.g. via `git config --global user.signingkey ...`). The
    # `-S` flag fails if signing fails; we never fall back to unsigned.
    log "creating signed commit (key=$ATHENA_SIGNING_KEY)"
    if ! git \
            -c "user.signingkey=$ATHENA_SIGNING_KEY" \
            -c "user.name=$ATHENA_BOT_NAME" \
            -c "user.email=$ATHENA_BOT_EMAIL" \
            commit -S -m "[athena-audit] weekly README truth-sync (${date_str})"; then
        err "signed commit failed -- bot key missing or unable to sign. Aborting."
        # Roll back the index so the working tree is clean for the next run.
        git reset HEAD -- README.md docs/ >/dev/null 2>&1 || true
        reset_athena_outputs
        exit 1
    fi

    log "pushing to origin/$default_branch"
    git push origin "$default_branch"

    # ---- Open-question gate (non-trivial drift) ------------------------------
    local readme_lines_changed
    readme_lines_changed="$(git show --stat HEAD -- README.md \
        | awk '/README\.md/ {print $3+$4}' | head -n1)"
    readme_lines_changed="${readme_lines_changed:-0}"
    if (( readme_lines_changed > BADGE_DELTA_THRESHOLD )); then
        log "non-trivial README delta ($readme_lines_changed lines) -- filing open_question."
        file_open_question \
            "Weekly Athena truth-sync repaired ${readme_lines_changed} lines of README drift on ${date_str}. Diff stat: ${diff_stat}"
    fi

    log "done."
}

main "$@"
