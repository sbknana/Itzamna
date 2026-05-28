#!/usr/bin/env bash
# Athena weekly truth-sync cron for the EQUIPA repository (task #2480).
#
# Layer 2 of the repo-consistency workflow:
#   Layer 0 (#2475): hygiene -- agent artifacts routed to .equipa-artifacts/.
#   Layer 1 (#2477): drift fence GitHub Action.
#   Layer 2 (THIS): weekly Athena regen + README truth-sync (this script).
#   Layer 3:        /repo-audit skill for manual on-demand version.
#
# What it does (in order):
#   1. cd to the Equipa-repo working tree.
#   2. git fetch + git pull origin <default-branch>.
#   3. Run Athena `generate-docs --scan --model quality` against the repo.
#   4. Sync the Athena-regenerated Architecture section + live counts
#      (tests / modules / lines) into the ROOT README.md, preserving the
#      hand-written intro (lines 1-29 / up to first "## What It Actually Does").
#   5. Run scripts/check_docs_drift.py; abort the push if drift remains.
#   6. Commit `[athena-audit] weekly README truth-sync (<DATE>)` and push.
#   7. If non-trivial drift was repaired, INSERT a TheForge open_question.
#
# Safety guards:
#   * Aborts if scripts/check_docs_drift.py is missing or fails to import.
#   * Aborts if the README preserve boundary marker is missing.
#   * Skips push when drift remains after the sync (logs an open_question).
#   * Quiet-exit 0 when there is nothing to commit.
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
# ---------------------------------------------------------------------------
get_default_branch() {
    local branch
    branch="$(git -C "$EQUIPA_REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [[ -n "$branch" ]]; then
        printf '%s\n' "${branch#origin/}"
        return 0
    fi
    branch="$(git -C "$EQUIPA_REPO" remote show origin 2>/dev/null \
        | awk '/HEAD branch/ {print $NF}')"
    if [[ -n "$branch" && "$branch" != "(unknown)" ]]; then
        printf '%s\n' "$branch"
        return 0
    fi
    if git -C "$EQUIPA_REPO" rev-parse --verify main >/dev/null 2>&1; then
        printf 'main\n'
        return 0
    fi
    printf 'master\n'
}

# ---------------------------------------------------------------------------
# Open-question filing (best-effort; never fails the run).
# ---------------------------------------------------------------------------
file_open_question() {
    local summary="$1"
    if [[ ! -f "$THEFORGE_DB" ]]; then
        log "TheForge DB not present at $THEFORGE_DB -- skipping open_question filing."
        return 0
    fi
    if ! command -v sqlite3 >/dev/null 2>&1; then
        log "sqlite3 not on PATH -- skipping open_question filing."
        return 0
    fi
    local escaped
    escaped="${summary//\'/\'\'}"
    sqlite3 "$THEFORGE_DB" \
        "INSERT INTO open_questions (project_id, question, context, resolved) \
         VALUES (${EQUIPA_PROJECT_ID}, \
                 'Athena truth-sync repaired non-trivial drift -- human review requested', \
                 '${escaped}', \
                 0);" \
        || log "open_question insert failed (non-fatal)"
}

# ---------------------------------------------------------------------------
# Safety preflight: drift checker must exist and import cleanly.
# ---------------------------------------------------------------------------
verify_drift_checker() {
    local checker="$EQUIPA_REPO/scripts/check_docs_drift.py"
    if [[ ! -f "$checker" ]]; then
        err "scripts/check_docs_drift.py is missing -- refusing to sync."
        return 1
    fi
    if ! "$PYTHON_BIN" -c "import importlib.util, sys; \
spec = importlib.util.spec_from_file_location('cdd', '$checker'); \
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); \
sys.exit(0)" >/dev/null 2>&1; then
        err "scripts/check_docs_drift.py failed to import -- refusing to sync."
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------
main() {
    log "starting Athena truth-sync run"

    if [[ ! -d "$EQUIPA_REPO/.git" ]]; then
        err "EQUIPA_REPO=$EQUIPA_REPO is not a git checkout -- aborting."
        exit 1
    fi

    cd "$EQUIPA_REPO"

    if ! verify_drift_checker; then
        err "safety guard failed -- aborting before any code/doc changes."
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
    if [[ -f "$ATHENA_CLI" ]]; then
        log "running Athena: $ATHENA_CLI generate-docs --scan --model $ATHENA_MODEL"
        ( cd "$ATHENA_DIR" && node "$ATHENA_CLI" generate-docs \
            --project "$EQUIPA_REPO" \
            --scan \
            --model "$ATHENA_MODEL" ) \
            || log "Athena exited non-zero -- continuing with sync against any existing docs/README.md"
    else
        log "Athena CLI not found at $ATHENA_CLI -- skipping regeneration, syncing live counts only."
    fi

    # ---- README sync ----------------------------------------------------------
    local athena_readme="$EQUIPA_REPO/docs/README.md"
    local sync_args=( "$EQUIPA_REPO/scripts/athena_sync_readme.py"
                      --repo-root "$EQUIPA_REPO" )
    if [[ -f "$athena_readme" ]]; then
        sync_args+=( --athena-readme "$athena_readme" )
    fi

    log "syncing root README.md"
    if ! "$PYTHON_BIN" "${sync_args[@]}"; then
        err "README sync failed -- aborting commit/push."
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
    git commit -m "[athena-audit] weekly README truth-sync (${date_str})"

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
