# SECURITY-REVIEW-2453 — EQUIPA routing hardening

## Counts

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 0
- INFO: 3

## Scope

Closes three findings from SECURITY-REVIEW-1728 against `equipa/routing.py`:

- **RT-01 HIGH** — Complexity scoring trivially downgraded by keyword stuffing.
- **RT-02 HIGH** — Circuit-breaker fallback escalates UPWARD, enabling financial DoS.
- **RT-03 MEDIUM** — Circuit-breaker state is module-global with no locking.

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

## Verification

```
$ timeout 300 python3 -m pytest --ignore=equipa/integration_test.py -q
............................................................................ [ ... ]
..........................                                               [100%]
1826 passed, 2 warnings in 185.25s (0:03:05)
```

Targeted run on the three routing test files (the only ones that
touch the changed module):

```
$ python3 -m pytest tests/test_routing_hardening.py tests/test_routing.py tests/test_cost_routing.py
64 passed in 0.47s
```

The 64 routing-specific tests cover: 6 RT-01 cases, 6 RT-02 cases, 4
RT-03 concurrency cases, plus the pre-existing regression suite
(updated to assert the new fail-closed contract on `test_circuit_open_*`).

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
