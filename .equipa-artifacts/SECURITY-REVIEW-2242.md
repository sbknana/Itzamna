# Security Review — Task #2242 (attempt 3)

**Scope:** EQUIPA vacuous-pass loophole — attempt 3 closes all 2 HIGH + 4
MEDIUM findings raised in the attempt-2 review.

**Branch:** `forge-task-2242` (built on top of `forge-task-2242-attempt2`).

**Files changed (this dispatch, on top of attempt-2):**
- `equipa/agent_runner.py` — Phase A3
- `equipa/loops.py` — Phase B3 (gate drop + paired guard), Phase A3 wiring
- `equipa/parsing.py` — Phase C3 + F3 (parser as source of truth), Phase D3 (anchored regex)
- `equipa/hooks/vacuous_pass.py` — Phase C3/F3 (None sentinel) + Phase E3
- `tests/test_tester_phase.py` — A3/B3 fixtures
- `tests/test_tester_parsing_2242.py` — C3/D3/F3 fixtures
- `tests/test_vacuous_pass_guard.py` — E3/F3 fixtures

## Counts

| Severity | Count | Status |
| --- | --- | --- |
| CRITICAL | 0 | — |
| HIGH | 0 | All attempt-2 HIGHs (F1, F2) resolved |
| MEDIUM | 0 | All attempt-2 MEDIUMs (F3, F4, F5, F6) resolved |
| LOW | 0 | Out-of-scope items deferred (F7, F8, F9) |
| INFO | 0 | — |

The attempt-2 fix landed Phase A/B/C/D but two HIGH gate-bypasses remained:
Phase B was structurally blind to framework stdout, and a tester reporting
`TESTS_RUN: 0` bypassed Phase A/C/D entirely. Attempt 3 closes both plus the
four MEDIUM defense-in-depth findings.

## Resolved Findings

### F1 HIGH (Phase A3) — Phase B sees framework stdout

**Original finding (attempt-2):** `grep_framework_skip_counts` reads
`result_text`, but `result_text` at `agent_runner.py:842` contains ONLY
`block_type == "text"` blocks from assistant messages — tool_use_result
blocks (the actual bash stdout the framework printed) were never added.
Phase B was therefore structurally blind to framework-emitted skip counts.

**Fix:**
- `equipa/agent_runner.py` now accumulates `tool_output_text_chunks` from
  every `tool_result` content block in the user-message stream, joins
  them (size-capped at 200 KiB to keep callers cheap), and exposes the
  result on the agent's result dict as `tool_output_text`.
- `equipa/loops.py` Phase B feeds **both** `result_text` and
  `tool_output_text` into `grep_framework_skip_counts`.

**Verification:**
`tests/test_tester_phase.py::test_tool_output_skip_disagreement_routes_inconclusive`
asserts that a tester emitting a clean structured block but a
`tool_output_text` containing a pytest footer `"5 skipped"` routes to
`tests_inconclusive` with reason `framework_skip_count_disagrees`.

### F2 HIGH (Phase B3) — Drop tests_run>0 gate on Phase A; paired guard

**Original finding (attempt-2):** `loops.py:1282` (Phase A), `:1304`
(Phase C), `:1312` (Phase D) all required `tests_run > 0`. A tester
reporting `RESULT: pass, TESTS_RUN: 0` bypassed all three.

**Fix:**
- `equipa/loops.py` drops the `tests_run > 0` gate on Phase A — the
  contract violation is the omission itself, regardless of `tests_run`.
- A paired guard fires immediately after Phase A: when
  `tester_outcome == "pass"` and `tests_run == 0`, the outcome routes to
  `tests_inconclusive` (reason: `pass_with_zero_tests`). "Pass with zero
  tests" is itself a contract violation.
- Phase C and Phase D continue to gate on `tests_run > 0` (the
  consistency check needs a positive denominator; the predicate
  references `tests_run`).

**Verification:**
- `test_pass_with_zero_tests_run_routes_inconclusive` — RESULT: pass +
  TESTS_RUN: 0 → `tests_inconclusive` with reason `pass_with_zero_tests`.
- `test_missing_skipped_with_zero_tests_run_routes_inconclusive` — same
  path with TESTS_SKIPPED also omitted → Phase A wins
  (`missing_tests_skipped_field`).

### F3 MEDIUM (Phase C3) — Parser as the source of truth

**Original finding (attempt-2):** The implementation used a side-channel
boolean `tests_skipped_present` while keeping `tests_skipped` defaulted
to 0. Any future caller using the existing `.get("tests_skipped", 0)`
pattern silently saw 0 on omission — the contract had to be re-enforced
at every call site.

**Fix:**
- `equipa/parsing.py:parse_tester_output` now treats TESTS_SKIPPED as
  REQUIRED. The new helper `_required_int(text, marker)` returns the
  parsed int on a clean marker line, and `None` on absence OR on an
  unparseable value (this folds F6 into the same code path). The parser
  attaches `tests_skipped=None` to its return dict on contract violation.
- The legacy `tests_skipped_present` payload key is removed; the canonical
  sentinel is `tests_skipped is None`.
- Both call sites (`equipa/loops.py` and
  `equipa/hooks/vacuous_pass.py`) derive `tests_skipped_present` locally
  from `tests_skipped is None` and never coerce `None` to 0 with `or 0`.

**Verification:**
- `test_tests_skipped_is_int_when_line_emitted_cleanly` — clean line → int.
- `test_tests_skipped_is_none_when_line_omitted` — absent → None.
- `test_tests_skipped_is_none_on_empty_text` — empty text → None.
- All A/B/C/D fixtures continue to pass with the new sentinel.

### F4 MEDIUM (Phase D3) — Anchored regex avoids prose false positives

**Original finding (attempt-2):** Patterns at `parsing.py:672-680`
(`\b(\d+)\s+skipped\b` etc.) matched anywhere in `result_text` including
tester prose. A SUMMARY line "2 tests were skipped earlier in dev"
pushed the count to 2 spuriously and produced false-positive
`tests_inconclusive` routings.

**Fix:**
- Each pattern in `_FRAMEWORK_SKIP_PATTERNS` is now anchored to the
  framework-specific line shape:
  - **pytest** footer: `(?m)^={3,}.*?\b(\d+)\s+skipped\b` — line must
    begin with ≥3 `=` signs (the footer rule).
  - **vitest / jest**: `(?m)^\s*Tests?:?\s+.*?\b(\d+)\s+skipped\b` —
    line must begin with `Tests`/`Test`.
  - **playwright (split footer)**: `(?m)^\s*\d+\s+passed.*?\b(\d+)\s+skipped\b`
    — line begins with `N passed`.
  - **generic anchored skip line**: `(?m)^\s*(\d+)\s+skipped\b` —
    catches playwright's "  4 skipped" variant and bare `N Skipped`.
  - **go**: `^---\s+SKIP:` (unchanged — already anchored).

**Verification:**
- `test_grep_ignores_prose_mentioning_skipped` — SUMMARY containing
  "2 tests were skipped earlier in dev" returns 0.
- `test_grep_ignores_embedded_skipped_in_log_line` — mid-line prose
  like "the 7 skipped runs" returns 0.
- All framework-specific fixtures (pytest/vitest/jest/playwright/go)
  still pass.

### F5 MEDIUM (Phase E3) — Hook predicate fixes no-tests blind spot

**Original finding (attempt-2):** `vacuous_pass.check_vacuous_pass` gated
Phase A on `tester_outcome in {"pass", "no-tests"} AND tests_run > 0`.
The "no-tests" branch carries `tests_run == 0` by definition, so Phase A
never engaged on no-tests — an adversarial tester could report
`RESULT: no-tests` while omitting TESTS_SKIPPED and silently bypass the
hook.

**Fix:**
- `equipa/hooks/vacuous_pass.py` drops the `tests_run > 0` gate on Phase
  A (consistent with the Phase-B3 change at the dispatch site). The
  contract violation is the omission itself; it does not become
  acceptable just because the tester also claimed zero tests ran.
- Phase C and Phase D in the hook retain the `tests_run > 0` gate (same
  rationale as at the dispatch site).

**Verification:**
- `test_no_tests_outcome_with_missing_skip_field_is_flagged` — RESULT:
  no-tests + omitted TESTS_SKIPPED → vacuous=True with reason
  `missing_tests_skipped_field`.

### F6 MEDIUM (Phase F3) — Presence requires parseable int

**Original finding (attempt-2):** `_has_marker_line` accepted any
`TESTS_SKIPPED:` line regardless of value parseability. A line
`TESTS_SKIPPED: (see comment)` satisfied presence while the value
coerced to 0 — same outcome as outright omission, but the contract
check said "present".

**Fix:**
- Folded into the F3 fix. `_required_int` returns `None` on a present
  marker whose value does not parse as `int`, so the hook and the
  dispatcher treat unparseable identically to absent.

**Verification:**
- `test_tests_skipped_is_none_when_value_unparseable` — parser returns
  `None` for `TESTS_SKIPPED: (see comment)`.
- `test_unparseable_tests_skipped_value_is_flagged` — hook flags the
  payload as vacuous with reason `missing_tests_skipped_field`.

## Out of scope (deferred per task constraints)

- **F7** — decisions audit row for drift telemetry.
- **F8** — playwright reporter coverage beyond split-footer.
- **F9** — formatting / style / INFO.

## Test summary

- `tests/test_tester_parsing_2242.py` — 17 cases (was 13).
- `tests/test_tester_phase.py` — 22 cases (was 19; 3 new A3/B3 fixtures).
- `tests/test_vacuous_pass_guard.py` — 24 cases (was 22; 2 new E3/F3 fixtures).
- `tests/test_single_agent_vacuous_pass.py` — 10 cases (unchanged).
- **Full pytest suite:** 1776 passed in 118.54s. Exit code 0.

## Architectural invariant

After this dispatch the contract is:

> ``parse_tester_output`` is the **single source of truth** for tester
> contract violations. The tester field ``tests_skipped`` is either a
> parseable integer (clean) or ``None`` (omission or unparseable). Every
> downstream call site — the dispatcher and the hook — derives the
> contract-violation signal from ``tests_skipped is None`` and routes the
> outcome to ``tests_inconclusive`` on violation. There is no shadow
> sentinel; there is no default-to-0; there is no per-callsite
> reinforcement of the contract.

Combined with the Phase D3 anchored regex (no prose false positives) and
the Phase A3 tool_output_text channel (no structural blindness), the
2236-class loophole is closed at every entry point.
