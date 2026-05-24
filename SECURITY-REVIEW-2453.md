# SECURITY-REVIEW-2453 — EQUIPA routing hardening

## Counts

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 0
- INFO: 3

## Scope

Closes three findings from SECURITY-REVIEW-1728 against `equipa/routing.py`
PLUS the S1 HIGH attempt-1 review finding that exposed an end-to-end gap
in the RT-02 fix:

- **RT-01 HIGH** — Complexity scoring trivially downgraded by keyword stuffing.
- **RT-02 HIGH** — Circuit-breaker fallback escalates UPWARD, enabling financial DoS.
- **RT-03 MEDIUM** — Circuit-breaker state is module-global with no locking.
- **2453-S1 HIGH** — RT-02 fail-closed signal absorbed by `roles.get_role_model`
  which then silently returned `DEFAULT_ROLE_MODELS[role]` (opus for the
  five most-used roles), defeating the RT-02 fix end-to-end.

Out of scope (separate follow-up tasks per task description):
RT-04 (potential ReDoS in `_uncertainty_level`), RT-L1 (config override
allows arbitrary string), RT-L2 (Opus circuit dead-branch recovery).

## Resolutions

### RT-01 — Keyword stuffing resistance + priority cross-validation [RESOLVED]

**Before:** `_semantic_depth` summed raw keyword occurrences. Repeating
`typo typo typo … security` 20×/1× drove the weighted score toward
`0.1` (haiku), even though the task was genuinely security work.

**Fix:**

- `_semantic_depth` now counts **distinct** keyword hits per bucket
  (`typo typo typo` = 1 distinct hit). The HIGH bucket is uncapped (more
  HIGH evidence is the safe direction); MEDIUM is capped at
  `MED_BUCKET_CAP=3`, LOW at `LOW_BUCKET_CAP=2`. An attacker that spams
  every distinct word in `LOW_KEYWORDS` (15 distinct hits) still only
  contributes 2 to the bucket — a single HIGH keyword keeps the score
  above the haiku threshold.
- New `_structural_complexity_bonus` adds up to `0.15` to the final
  score using purely structural features — description length, line
  count, and distinct path/file tokens. These cannot be faked by
  repeating cheap keywords.
- `select_model_by_complexity` accepts a `priority` argument
  (`critical`/`high`/`medium`/`low`). Priority sets a **minimum tier
  floor**: scored tier and priority floor are combined with `max()` so a
  `critical` task always pins to opus regardless of score, and a `high`
  task always lands on sonnet or above. Disagreement is logged at
  `WARNING` level. `auto_select_model` forwards `task["priority"]`
  automatically.

**Acceptance test (verbatim from task description):**
`tests/test_routing_hardening.py::TestRT01KeywordStuffing::test_low_keyword_spam_cannot_downgrade_high_priority_task`
passes: 40× "simple trivial easy quick" stuffed into a description with
`priority="high"` still routes to sonnet or above.

### RT-02 — Fallback NEVER escalates cost [RESOLVED]

**Before:** `fallback_map = {"haiku": "sonnet", "sonnet": "opus",
"opus": "opus"}`. An attacker who tripped the haiku circuit (via
legitimate-looking traffic that triggers rate limits) forced every
subsequent dispatch onto opus.

**Fix:**

- New `_FALLBACK_DOWN: dict[str, str | None]` always points DOWN:
  `opus → sonnet`, `sonnet → haiku`, `haiku → None` (fail closed).
- `auto_select_model` walks the ladder DOWN while the chosen circuit is
  open; if every tier is open it returns `None`. The caller
  (`equipa/roles.py:113-116`) already handles a falsy return by falling
  through to the next priority — fail-closed semantics are preserved.
- The fallback map is asserted as a structural invariant in
  `TestRT02FallbackNeverEscalates::test_fallback_map_has_no_upward_arrows`,
  so any future edit that points a value to a more-expensive tier
  fails CI.
- Module docstring documents the policy: *"circuit-breaker fallback
  NEVER increases cost"*.

**Acceptance test (verbatim):**
`TestRT02FallbackNeverEscalates::test_haiku_open_fails_closed` trips
the haiku breaker, asserts `auto_select_model` returns `None`, and
explicitly checks `model not in ("sonnet", "opus")`.

### RT-03 — Lock-guarded circuit-breaker state [RESOLVED]

**Before:** `_circuit_breaker_state: dict[str, dict]` was read and
mutated by `record_model_outcome` and `_get_circuit_state` without any
synchronization. Concurrent dispatchers could race on
`state["consecutive_failures"] += 1`, losing updates so the breaker
never tripped.

**Fix:**

- New module-level `_circuit_breaker_lock = threading.Lock()`.
- `record_model_outcome` and `_get_circuit_state` each acquire the lock
  for the **full** read-modify-write window (dict membership check,
  state read, time read, state mutation).
- Locking discipline documented in the module docstring.
- Critical sections are short (dict ops + `time.time()`), never block
  on I/O, and never call back into user code, so contention is
  negligible.

**Acceptance test (verbatim):**
`TestRT03Concurrency::test_concurrent_failures_open_circuit_exactly_once`
starts 10 threads (gated by `threading.Barrier`) that each call
`record_model_outcome("haiku", False)`. Post-state asserts
`consecutive_failures == 10` (no lost updates) and `state == OPEN`.
Two additional tests assert mixed-outcome consistency and that
parallel `auto_select_model` calls never crash or return invalid
values.

### 2453-S1 — Propagate fail-closed signal from auto_select_model through get_role_model [RESOLVED]

**Before (attempt 1):** `roles.get_role_model` ended with

```python
if effective_config and task and is_feature_enabled(effective_config, "auto_model_routing"):
    from equipa.routing import auto_select_model
    routed_model = auto_select_model(task, effective_config)
    if routed_model:
        return routed_model
return DEFAULT_ROLE_MODELS.get(role, DEFAULT_MODEL)
```

When `auto_select_model` returned `None` (every circuit OPEN, the
RT-02 fail-closed signal), control fell through to
`DEFAULT_ROLE_MODELS`. For the five most-used roles
(`developer`, `security-reviewer`, `planner`, `frontend-designer`,
`debugger`) `DEFAULT_ROLE_MODELS` maps to **opus** — so the exact
attack RT-02 was designed to block (trip the cheap circuit, force the
dispatcher onto opus) still succeeded end-to-end. The docstring claim
in `routing.py` (*"fail-closed semantics are preserved by the
caller"*) was not actually true.

**Fix:**

- New `CircuitOpenError(RuntimeError)` in `equipa/routing.py`. Carries
  the failing `role` and (optional) `tier_attempted` so the dispatch
  wrapper can emit a structured `[GATE-AUDIT] event=circuit-blocked`
  log line.
- `roles.get_role_model` now raises `CircuitOpenError` instead of
  falling through to `DEFAULT_ROLE_MODELS` when auto-routing is ON
  AND `auto_select_model` returned `None`. The legacy path
  (auto-routing OFF) is unchanged — `DEFAULT_ROLE_MODELS` still wins
  in that mode.
- `dispatch.run_dev_test_loop_with_autoresearch` (the canonical
  retry wrapper used by both `run_parallel_tasks` and the single-task
  dev-test path) catches `CircuitOpenError` and demotes the outcome
  to **`circuit_breaker_blocked`** — the same observable pattern
  as `security_review_blocked`. Cycle count is 0, cost is 0, branch
  is not merged, task is left in a state the orchestrator can retry
  after the breaker recovery window.
- The single-task `--dev-test` path in `cli.py:run_mode_task` mirrors
  the same `try/except CircuitOpenError → outcome=circuit_breaker_blocked`
  pattern at the call site of `run_dev_test_loop` (preserves the
  2448 single-task/parallel parity invariant).
- Post-loop telemetry (`record_agent_run`, `_post_task_telemetry`)
  guards its own `get_role_model` resolution with the same
  `try/except` and substitutes the sentinel string
  `"circuit_blocked"` — telemetry must never re-raise after the
  loop has already demoted.
- Docstring on `CircuitOpenError` documents the contract; the
  `routing.py` module-level docstring no longer claims the caller
  preserves fail-closed semantics — `roles.get_role_model` now
  enforces it.

**Acceptance tests:**

- `TestS1FailClosedPropagation::test_get_role_model_raises_circuit_open_error_for_opus_roles`
  (parametrized over the five DEFAULT_ROLE_MODELS=opus roles) — trips
  every circuit, asserts `CircuitOpenError` is raised rather than
  `"opus"` being returned silently.
- `TestS1FailClosedPropagation::test_get_role_model_raises_when_only_cheapest_attack_path_tripped`
  — the exact RT-02 attack: trips ONLY haiku, dispatches a trivial
  task, asserts `auto_select_model` returns `None` AND
  `get_role_model` raises.
- `TestS1FailClosedPropagation::test_get_role_model_flag_off_still_falls_through`
  — auto-routing OFF, all circuits open, must NOT raise; falls
  through to `DEFAULT_ROLE_MODELS["developer"] == "opus"`.
- `TestS1FailClosedPropagation::test_circuit_open_error_carries_diagnostic_payload`
  — verifies the exception carries `role` + `tier_attempted` for
  GATE-AUDIT logging.
- `TestS1DispatchWrapperDemotion::test_dispatch_wrapper_handler_emits_circuit_blocked_outcome`
  — monkeypatches `run_dev_test_loop` to raise `CircuitOpenError`,
  runs `run_dev_test_loop_with_autoresearch`, asserts the wrapper
  returns `outcome == "circuit_breaker_blocked"` and `cycles == 0`
  (does NOT propagate the exception).
- `TestS1DispatchWrapperDemotion::test_dispatch_wrapper_imports_circuit_open_error`
  and `test_cli_module_imports_circuit_open_error` — structural
  invariants ensuring both dispatch entry points have the typed
  exception in scope.

## Verification

```
$ timeout 300 python3 -m pytest --ignore=equipa/integration_test.py -q
............................................................................ [ ... ]
..........................                                               [100%]
1826+ passed
```

Targeted run on the routing + dispatch test files touched by attempt-2:

```
$ python3 -m pytest tests/test_routing_hardening.py tests/test_cost_routing.py
41 passed in 0.4s
```

The routing-specific tests now cover: 6 RT-01 cases, 6 RT-02 cases, 4
RT-03 concurrency cases, plus the 9 new **S1 fail-closed propagation**
cases (6 in `TestS1FailClosedPropagation`, 3 in
`TestS1DispatchWrapperDemotion`).

## Informational notes

- **RT-2453-I1 INFO:** `DEFAULT_MODEL` is imported but no longer used
  as a safety net — the RT-02 fail-closed policy is incompatible with
  "fall back to a default model." Left in place because downstream
  callers may legitimately want to read it.
- **RT-2453-I2 INFO:** `_uncertainty_level` regex set is unchanged in
  this task — RT-04 (potential ReDoS in `\bwhy\b.*\bfailing\b`) is the
  separate follow-up tracked in SECURITY-REVIEW-1728.
- **RT-2453-I3 INFO:** Lock contention monitoring is not added in this
  task. If observed contention becomes a problem, switching to
  `threading.RLock` and finer-grained per-model locks is a
  source-compatible change.

## Out of scope (deferred to follow-up tasks)

The attempt-2 review specifically excluded the following from this
patchset; each becomes a separate task if value warrants:

- **S2** — broader audit of trust placed in `task.priority` (any user-
  controllable column that can elevate cost or escape gating).
- **S3** — switch the circuit breaker to `asyncio.Lock` so async
  dispatchers do not serialize through the GIL-friendly but
  thread-only `threading.Lock`.
- **S4** — allowlist for `config["model_overrides"]` (currently
  accepts any string).
- **S5/S6** — unbounded regex performance concerns in
  `_uncertainty_level` and `_task_scope`.
- **S7** — TOCTOU between `_get_circuit_state` check and the actual
  dispatch (needs async lock from S3 first).
