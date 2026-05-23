# SECURITY-REVIEW-2452 — EQUIPA MCP server hardening

Resolves three findings from SECURITY-REVIEW-1728 against `equipa/mcp_server.py`:
MCP-01 (HIGH), MCP-02 (HIGH), MCP-03 (MEDIUM).

## Counts

- CRITICAL: 0
- HIGH: 0 (closes 2 HIGH)
- MEDIUM: 0 (closes 1 MEDIUM)
- LOW: 0
- INFO: 0

## Findings closed

### MCP-01 HIGH — equipa_dispatch unauthenticated, unrate-limited

**Status:** RESOLVED.

Fixes landed in `equipa/mcp_server.py`:

- **Authentication.** `_check_auth` reads `EQUIPA_MCP_TOKEN` at call time
  (not import time, so the server fails closed when the env var is missing
  from the systemd unit) and compares it to a caller-supplied `auth_token`
  argument. Mismatch or absent token returns
  `{"error": "Invalid or missing auth_token", "auth": "rejected"}`. Server
  with no token configured returns `auth: "unconfigured"` and refuses every
  privileged call.
- **Rate limit.** `_TokenBucket(DISPATCH_RATE_CAPACITY=10,
  DISPATCH_RATE_REFILL_SECONDS=3600)` is the bucket. On exhaustion the
  handler returns `{"error": "Rate limit exceeded...", "retry_after_seconds": N}`.
  Keyed by token so two callers do not starve each other.
- **Cost cap.** `_dispatch_cost_cap_usd()` reads `EQUIPA_MCP_COST_CAP_USD`.
  When set, `_recent_dispatch_cost_usd()` sums `agent_runs.cost_usd` over the
  last 24h and refuses dispatch once the cap is reached.
- **Role + model allowlists.** `ALLOWED_ROLES` is constructed from
  `ROLE_PROMPTS`, `DEFAULT_ROLE_TURNS`, and `DEFAULT_ROLE_MODELS` keys so it
  tracks the orchestrator's real role list. `ALLOWED_MODELS = {opus, sonnet,
  haiku}` matches the orchestrator's CLI. Out-of-allowlist values are
  rejected before any subprocess is spawned.

Regression tests (in `tests/test_mcp_server.py`):
`test_dispatch_rejects_missing_token`,
`test_dispatch_rejects_bad_token`,
`test_dispatch_rejects_unconfigured_token`,
`test_dispatch_rejects_unknown_role`,
`test_dispatch_rejects_unknown_model`,
`test_dispatch_rate_limit_fires`,
`test_dispatch_cost_cap_blocks`.

### MCP-02 HIGH — equipa_task_create unvalidated, unquoted

**Status:** RESOLVED.

Fixes landed in `_handle_equipa_task_create`:

- Same `auth_token` check + dedicated `_TASK_CREATE_BUCKET` (100/hour/token).
- Validates `project_id` is a positive integer.
- Looks up the project row before insert: refuses if absent
  (`f"project_id {project_id} does not exist"`) and refuses if `projects.status`
  is not in `{active, planning}` (per task spec).
- `description` is type-checked as `str` and byte-length-capped at
  `MAX_DESCRIPTION_BYTES = 32 KB`.
- Optional `EQUIPA_MCP_PROJECT_IDS` env var (comma-separated integers) acts as
  a deny-by-default allowlist for the calling token.

Regression tests:
`test_task_create_rejects_missing_token`,
`test_task_create_rejects_nonexistent_project`,
`test_task_create_rejects_inactive_project`,
`test_task_create_rejects_oversize_description`,
`test_task_create_respects_project_allowlist`,
`test_task_create_rate_limit_fires`.

### MCP-03 MEDIUM — Unbounded limit on query handlers

**Status:** RESOLVED.

`_clamp_limit(requested, tool)` clamps to `[1, MAX_QUERY_LIMIT=500]`, falling
back to the max on non-integer or non-positive input. Applied in
`_handle_equipa_lessons`, `_handle_equipa_agent_logs`, and
`_handle_equipa_session_notes`. The effective limit is echoed back in the
response (`"limit": N`) and clamping is logged to stderr.

Regression tests:
`test_lessons_limit_clamped`,
`test_agent_logs_limit_clamped`,
`test_session_notes_limit_clamped`,
`test_clamp_limit_unit`.

## Runtime config

`mcp_config.example.json` now documents an `equipa` MCP server entry with the
three env vars (`EQUIPA_MCP_TOKEN`, `EQUIPA_MCP_COST_CAP_USD`,
`EQUIPA_MCP_PROJECT_IDS`). Operators copy this into `mcp_config.json` on
Equipa-prod and replace the placeholder token.

## Out of scope (per task description)

- MCP-04 (subprocess PIPE deadlock at ~64 KB)
- MCP-05 / MCP-06 / MCP-07 (type validation, LIKE injection, exception leak)
  — bundled into a follow-up MEDIUM task.

## Verification

`python3 -m pytest tests/test_mcp_server.py -x -q` → 33 passed in 7.01s.
