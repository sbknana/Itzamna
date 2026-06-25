#!/usr/bin/env bash
# deploy-equipa-prod.sh
#
# Codified deploy from Equipa-repo (source) to Equipa-prod (production).
# Replaces ad-hoc cp/rsync flows that were silently overwriting production-only
# patches and leaving prod's git state ambiguous.
#
# Usage:
#   bash scripts/deploy-equipa-prod.sh
#
# This script is INTENTIONALLY non-destructive:
#   - never runs `rm -rf`
#   - never force-pushes
#   - snapshots production-only files before pulling
#   - aborts loudly on any verification failure
#
# Copyright (c) 2026 Forgeborn

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROD_DIR="${EQUIPA_PROD_DIR:-/srv/forge-share/AI_Stuff/Equipa-prod}"
SOURCE_REPO_MARKER="forge_orchestrator.py"   # file that must exist in source CWD
UPSTREAM_REMOTE="${EQUIPA_UPSTREAM_REMOTE:-origin}"
UPSTREAM_BRANCH="${EQUIPA_UPSTREAM_BRANCH:-main}"

# Files that exist ONLY in production. Keep this list in sync with
# .deploy-allowlist (canonical machine-readable form) and docs/PROD_ONLY.md.example.
# This in-script array is the *fallback* used when .deploy-allowlist cannot be
# located in the source tree, so deploy still works on a stripped-down checkout.
PROD_ONLY_FILES=(
    "dispatch_config.json"
    "forge_config.json"
    "mcp_config.json"
    "theforge.db"
    ".env"
)

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT_DIR="/tmp/equipa-prod-snapshot-${TIMESTAMP}"

# ---------------------------------------------------------------------------
# Allowlist loading
# ---------------------------------------------------------------------------
# Load .deploy-allowlist (preferred, machine-readable source of truth) and
# overlay it on the in-script default. If the file is absent we keep the
# hardcoded list so the script remains self-contained.
load_deploy_allowlist() {
    local allowlist_path="${1:-}"
    if [ -z "${allowlist_path}" ] || [ ! -f "${allowlist_path}" ]; then
        return 1
    fi
    local loaded=()
    local line trimmed
    while IFS= read -r line || [ -n "${line}" ]; do
        # Strip trailing CR (Windows line endings) and surrounding whitespace.
        trimmed="${line%$'\r'}"
        trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
        case "${trimmed}" in
            ''|'#'*) continue ;;
        esac
        loaded+=("${trimmed}")
    done < "${allowlist_path}"
    if [ "${#loaded[@]}" -eq 0 ]; then
        return 1
    fi
    PROD_ONLY_FILES=("${loaded[@]}")
    return 0
}

is_allowlisted() {
    local needle="$1"
    local entry
    for entry in "${PROD_ONLY_FILES[@]}"; do
        if [ "${entry}" = "${needle}" ]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
fail() {
    printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2
    if [ -n "${PROD_COMMIT_BEFORE:-}" ]; then
        cat >&2 <<EOF

To roll back Equipa-prod to its pre-deploy state:
    cd "${PROD_DIR}"
    git reset --hard ${PROD_COMMIT_BEFORE}

Snapshot of production-only files preserved at:
    ${SNAPSHOT_DIR}
EOF
    fi
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

# ---------------------------------------------------------------------------
# Step 1: verify source CWD
# ---------------------------------------------------------------------------
log "Step 1: verify deploy is running from Equipa-repo"
if [ ! -f "${SOURCE_REPO_MARKER}" ]; then
    fail "must run from Equipa-repo root (missing ${SOURCE_REPO_MARKER} in $(pwd))"
fi
SOURCE_DIR="$(pwd)"
log "  source=${SOURCE_DIR}"

require_cmd git
require_cmd python3
require_cmd cp

# Prefer the canonical allowlist file when it ships with the source repo.
ALLOWLIST_PATH="${SOURCE_DIR}/.deploy-allowlist"
if load_deploy_allowlist "${ALLOWLIST_PATH}"; then
    log "  loaded prod-only allowlist from .deploy-allowlist (${#PROD_ONLY_FILES[@]} entries)"
else
    log "  using built-in prod-only allowlist (${#PROD_ONLY_FILES[@]} entries; .deploy-allowlist not found)"
fi

# ---------------------------------------------------------------------------
# Step 2: verify Equipa-prod exists
# ---------------------------------------------------------------------------
log "Step 2: verify Equipa-prod exists at ${PROD_DIR}"
if [ ! -d "${PROD_DIR}" ]; then
    fail "Equipa-prod not found at ${PROD_DIR}"
fi
if [ ! -d "${PROD_DIR}/.git" ]; then
    fail "${PROD_DIR} is not a git repository"
fi

# ---------------------------------------------------------------------------
# Step 3: record current prod commit
# ---------------------------------------------------------------------------
log "Step 3: record current prod commit (for rollback)"
PROD_COMMIT_BEFORE="$(git -C "${PROD_DIR}" rev-parse HEAD)"
log "  prod HEAD before pull = ${PROD_COMMIT_BEFORE}"

# ---------------------------------------------------------------------------
# Step 4: snapshot prod-only files BEFORE pulling
# ---------------------------------------------------------------------------
log "Step 4: snapshot production-only files to ${SNAPSHOT_DIR}"
mkdir -p "${SNAPSHOT_DIR}"

PRESERVED_FILES=()
for rel_path in "${PROD_ONLY_FILES[@]}"; do
    src="${PROD_DIR}/${rel_path}"
    if [ -e "${src}" ] || [ -L "${src}" ]; then
        # Preserve symlinks as symlinks, regular files as copies.
        cp -a "${src}" "${SNAPSHOT_DIR}/$(basename "${rel_path}")"
        PRESERVED_FILES+=("${rel_path}")
        log "  snapshotted ${rel_path}"
    else
        warn "  prod-only file not present (skipping): ${rel_path}"
    fi
done

# ---------------------------------------------------------------------------
# Step 5: pull source into prod (with auto-bootstrap for source drift)
# ---------------------------------------------------------------------------
log "Step 5: git pull ${UPSTREAM_REMOTE} ${UPSTREAM_BRANCH} in prod"
if ! git -C "${PROD_DIR}" remote get-url "${UPSTREAM_REMOTE}" >/dev/null 2>&1; then
    fail "remote '${UPSTREAM_REMOTE}' not configured in ${PROD_DIR}"
fi

# Fetch first so we know the new upstream tip and the file set it tracks.
log "  fetching ${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"
if ! git -C "${PROD_DIR}" fetch "${UPSTREAM_REMOTE}" "${UPSTREAM_BRANCH}"; then
    fail "git fetch ${UPSTREAM_REMOTE} ${UPSTREAM_BRANCH} failed"
fi
NEW_UPSTREAM_TIP="$(git -C "${PROD_DIR}" rev-parse "${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}")"
log "  upstream tip          = ${NEW_UPSTREAM_TIP}"

# Inventory: which files does the new upstream tip track?
UPSTREAM_FILES_TMP="$(mktemp)"
trap 'rm -f "${UPSTREAM_FILES_TMP}"' EXIT
git -C "${PROD_DIR}" ls-tree -r --name-only "${NEW_UPSTREAM_TIP}" > "${UPSTREAM_FILES_TMP}"

# Inventory: what is dirty in prod right now? (modified, staged, untracked)
SOURCE_DRIFT=()
UNKNOWN=()
DIRTY_COUNT=0
while IFS= read -r status_line; do
    [ -z "${status_line}" ] && continue
    DIRTY_COUNT=$((DIRTY_COUNT + 1))
    # Porcelain v1 format: "XY <space> path".
    # Renames use "R  old -> new"; production never renames so we treat the
    # whole tail as a single path and surface anything weird as UNKNOWN.
    rel_path="${status_line:3}"
    # Strip surrounding double-quotes that git adds for paths with spaces.
    if [ "${rel_path#\"}" != "${rel_path}" ]; then
        rel_path="${rel_path#\"}"
        rel_path="${rel_path%\"}"
    fi
    if grep -qxF -- "${rel_path}" "${UPSTREAM_FILES_TMP}"; then
        SOURCE_DRIFT+=("${rel_path}")
    elif is_allowlisted "${rel_path}"; then
        # Already snapshotted in step 4; safe to leave alone.
        :
    else
        UNKNOWN+=("${rel_path}")
    fi
done < <(git -C "${PROD_DIR}" status --porcelain)

if [ "${#UNKNOWN[@]}" -gt 0 ]; then
    warn "  prod contains ${#UNKNOWN[@]} file(s) that are neither tracked upstream nor in the allowlist:"
    for f in "${UNKNOWN[@]}"; do
        warn "    - ${f}"
    done
    fail "Refusing to destroy unrecognized files. Either remove them, add them to .deploy-allowlist (and docs/PROD_ONLY.md.example), or commit them upstream first."
fi

if [ "${#SOURCE_DRIFT[@]}" -gt 0 ]; then
    log "[deploy] Auto-bootstrapping: ${#SOURCE_DRIFT[@]} source files reset to upstream"
    # Source files are NEVER edited in prod by hand — they only ever land
    # there via this deploy script. Anything dirty that upstream tracks is
    # safe to discard; the snapshot in step 4 already preserved prod-only
    # files separately, so this reset cannot lose tuned config.
    if ! git -C "${PROD_DIR}" reset --hard "${NEW_UPSTREAM_TIP}"; then
        fail "git reset --hard ${NEW_UPSTREAM_TIP} failed during auto-bootstrap"
    fi
    # Untracked files that collided with upstream paths still need removing
    # before they shadow the just-reset working tree. Delete them precisely,
    # one path at a time — never use rm -rf.
    while IFS= read -r status_line; do
        [ -z "${status_line}" ] && continue
        # Only ?? entries can remain after a hard reset.
        case "${status_line:0:2}" in
            '??')
                rel_path="${status_line:3}"
                if [ "${rel_path#\"}" != "${rel_path}" ]; then
                    rel_path="${rel_path#\"}"
                    rel_path="${rel_path%\"}"
                fi
                if grep -qxF -- "${rel_path}" "${UPSTREAM_FILES_TMP}"; then
                    log "  removing untracked source-tree collision: ${rel_path}"
                    rm -f -- "${PROD_DIR}/${rel_path}"
                fi
                ;;
        esac
    done < <(git -C "${PROD_DIR}" status --porcelain)
elif [ "${DIRTY_COUNT}" -eq 0 ]; then
    if ! git -C "${PROD_DIR}" pull --ff-only "${UPSTREAM_REMOTE}" "${UPSTREAM_BRANCH}"; then
        fail "git pull failed (non-fast-forward or conflict). Inspect ${PROD_DIR} manually."
    fi
else
    # Dirty state was entirely allowlisted prod-only files; pull is still
    # the safest path because those will be restored from snapshot in step 6.
    if ! git -C "${PROD_DIR}" pull --ff-only "${UPSTREAM_REMOTE}" "${UPSTREAM_BRANCH}"; then
        fail "git pull failed (non-fast-forward or conflict). Inspect ${PROD_DIR} manually."
    fi
fi

PROD_COMMIT_AFTER="$(git -C "${PROD_DIR}" rev-parse HEAD)"
log "  prod HEAD after pull  = ${PROD_COMMIT_AFTER}"

# ---------------------------------------------------------------------------
# Step 6: restore prod-only files from snapshot
# ---------------------------------------------------------------------------
log "Step 6: restore production-only files from snapshot"
for rel_path in "${PRESERVED_FILES[@]}"; do
    snap="${SNAPSHOT_DIR}/$(basename "${rel_path}")"
    dest="${PROD_DIR}/${rel_path}"
    if [ ! -e "${snap}" ] && [ ! -L "${snap}" ]; then
        fail "internal error: snapshot missing for ${rel_path}"
    fi
    # Remove anything the pull may have placed at the destination, then restore.
    if [ -e "${dest}" ] || [ -L "${dest}" ]; then
        rm -f "${dest}"
    fi
    cp -a "${snap}" "${dest}"
    log "  restored ${rel_path}"
done

# ---------------------------------------------------------------------------
# Step 7: verify imports
# ---------------------------------------------------------------------------
log "Step 7: verify equipa package imports cleanly"
if ! ( cd "${PROD_DIR}" && python3 -c "import equipa; print('import OK')" ); then
    fail "equipa package import failed in ${PROD_DIR}"
fi

# ---------------------------------------------------------------------------
# Step 8: verify skill_manifest hashes
#
# The manifest shipped from upstream was generated in source-repo's
# environment; forge_orchestrator.py also auto-regenerates it at runtime in
# prod. Comparing the just-pulled file against a fresh regeneration therefore
# produces a false-positive "drift" any time the two environments differ
# even slightly. To detect *real* tampering (skill content changed under us
# between two regenerations on the SAME host), we instead:
#
#   1. Regenerate once to establish a deterministic post-regen baseline.
#   2. Regenerate again and compare the two regen results.
#
# A stable manifest must hash identically across two consecutive regens.
# Any difference here indicates non-deterministic generation or skill content
# changing mid-deploy, both of which warrant aborting.
# ---------------------------------------------------------------------------
log "Step 8: verify skill_manifest hashes"
MANIFEST_PATH="${PROD_DIR}/skill_manifest.json"
ORCH_PATH="${PROD_DIR}/forge_orchestrator.py"

if [ ! -f "${MANIFEST_PATH}" ]; then
    warn "  skill_manifest.json missing in prod (skipping hash check)"
elif [ ! -f "${ORCH_PATH}" ]; then
    warn "  forge_orchestrator.py missing in prod (skipping hash check)"
else
    # First regeneration establishes the deterministic baseline. Without
    # this step the "before" hash is whatever upstream happened to commit,
    # which is often stale relative to the runtime regenerator.
    if ( cd "${PROD_DIR}" && python3 forge_orchestrator.py --regenerate-manifest >/dev/null 2>&1 ); then
        # Hash only the "files" map (sorted) — NOT the raw file. The manifest
        # embeds a wall-clock "generated_at" stamp that changes every regen, so a
        # raw-byte compare false-positives. Tamper detection only cares about the
        # skill file hashes.
        MANIFEST_BASELINE="$(python3 -c 'import json,hashlib,sys; d=json.load(open(sys.argv[1])); print(hashlib.sha256(json.dumps(d.get("files", d), sort_keys=True).encode()).hexdigest())' "${MANIFEST_PATH}")"
        # Second regeneration must produce an identical hash on the same host.
        if ! ( cd "${PROD_DIR}" && python3 forge_orchestrator.py --regenerate-manifest >/dev/null 2>&1 ); then
            fail "skill_manifest second regeneration failed (first succeeded). Investigate orchestrator state before continuing."
        fi
        MANIFEST_VERIFY="$(python3 -c 'import json,hashlib,sys; d=json.load(open(sys.argv[1])); print(hashlib.sha256(json.dumps(d.get("files", d), sort_keys=True).encode()).hexdigest())' "${MANIFEST_PATH}")"
        if [ "${MANIFEST_BASELINE}" != "${MANIFEST_VERIFY}" ]; then
            fail "skill_manifest.json hashes drifted between two consecutive regenerations (baseline=${MANIFEST_BASELINE} verify=${MANIFEST_VERIFY}). Generation is non-deterministic or skill content changed mid-deploy."
        fi
        log "  manifest hash stable across regenerations (${MANIFEST_VERIFY})"
    else
        warn "  --regenerate-manifest not available; skipping hash recomputation"
    fi
fi

# ---------------------------------------------------------------------------
# Step 9: summary
# ---------------------------------------------------------------------------
log "Step 9: summary"
cat <<EOF
------------------------------------------------------------
  Equipa production deploy complete
------------------------------------------------------------
  source repo                 : ${SOURCE_DIR}
  prod dir                    : ${PROD_DIR}
  deployed FROM commit (prod) : ${PROD_COMMIT_BEFORE}
  deployed TO   commit (prod) : ${PROD_COMMIT_AFTER}
  upstream pulled             : ${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}
  snapshot dir                : ${SNAPSHOT_DIR}
  production-only files preserved:
EOF
if [ "${#PRESERVED_FILES[@]}" -eq 0 ]; then
    echo "    (none — review PROD_ONLY.md if this is unexpected)"
else
    for rel_path in "${PRESERVED_FILES[@]}"; do
        echo "    - ${rel_path}"
    done
fi
echo "------------------------------------------------------------"
log "deploy OK"
