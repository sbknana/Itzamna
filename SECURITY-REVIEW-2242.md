# Security Review — Task #2242 (re-dispatch)

**Scope:** EQUIPA vacuous-pass loophole — re-dispatch closing all S1 HIGH /
S2 HIGH / S3 MEDIUM / S4 MEDIUM findings from the attempt-1 review.

**Branch:** `forge-task-2242` (built on top of `forge-task-2242-attempt1`).
**Files changed (this dispatch, on top of attempt-1):**
- `equipa/parsing.py`
- `equipa/loops.py`
- `equipa/hooks/vacuous_pass.py`
- `prompts/tester.md`
- `tests/test_vacuous_pass_guard.py`
- `tests/test_tester_phase.py`
- `tests/test_tester_parsing_2242.py` (new)
- `skill_manifest.json` (auto-regenerated)

## Summary

| Severity | Count | Status |
| --- | --- | --- |
| CRITICAL | 0 | — |
| HIGH | 0 | All attempt-1 HIGHs resolved |
| MEDIUM | 0 | All attempt-1 MEDIUMs resolved (in-scope items only) |
| LOW | 0 | Out-of-scope (S5–S7) deferred per task constraints |
| INFO | 0 | — |

The attempt-1 guard failed open on the very class of bypass it was meant
to catch. This dispatch lands four defense-in-depth layers, each backed
by fixtures, and tightens the canonical predicate to `==`.

## Resolved Findings

### S1 HIGH (Phase A) — Mandatory TESTS_SKIPPED presence

**Original finding (attempt-1):** Tester prompt declares `TESTS_SKIPPED`
as REQUIRED, but the parser at `_parse_structured_output` silently
defaults missing int fields to 0. A tester that omits the line (drift,
truncation, model regression, deliberate misreport) produced
`tests_skipped=0`, the all-skipped predicate failed for any
`tests_run > 0`, and the loophole reopened.

**Fix:**
- `equipa/parsing.py:parse_tester_output` now sets a new boolean key
  `tests_skipped_present` based on whether the raw text contains a line
  beginning with `TESTS_SKIPPED:`. Implemented via the new helper
  `_has_marker_line`.
- `equipa/loops.py:_dispatch_tester_outcome` reads
  `tests_skipped_present` and routes the outcome to `tests_inconclusive`
  when the line is absent and `tests_run > 0`.
- `equipa/hooks/vacuous_pass.py:check_vacuous_pass` mirrors the
  fail-closed behavior.
- `prompts/tester.md` now states explicitly: "omission is a contract
  violation, not a default to 0", and warns that the orchestrator will
  retry the run as tests_inconclusive when the line is missing.

**Verification:** `tests/test_tester_parsing_2242.py` covers both
present-and-zero and absent cases. `tests/test_vacuous_pass_guard.py`
asserts the hook flags the absent case. `tests/test_tester_phase.py`
asserts the dispatcher posts the correct `missing_tests_skipped_field`
reason and routes to `tests_inconclusive`.

### S2 HIGH (Phase B) — TESTS_RUN=0 bypass

**Original finding (attempt-1):** Both detector sites short-circuited on
`tests_run > 0`. A tester reporting `TESTS_RUN: 0, TESTS_PASSED: 0`
fell through to the no-tests branch, where only `_is_docs_only` could
save us. Nothing enforced that the report agreed with reality.

**Fix:**
- New helper `equipa.parsing.grep_framework_skip_counts(result_text)`
  scans tester stdout for framework-emitted skip signals:
  - `\b(\d+)\s+skipped\b` (vitest, playwright, generic)
  - `\b(\d+)\s+todo\b` (jest)
  - `=\s*(\d+)\s+skipped[\s=]` (pytest footer)
  - `^---\s+SKIP:\s+\S+` per-line count (go test)
  Returns the max count observed across patterns.
- `_dispatch_tester_outcome` compares the framework count against the
  tester's reported `tests_skipped`. Disagreement (framework > tester)
  routes to `tests_inconclusive` with reason
  `framework_skip_count_disagrees` and the observed count attached.

**Verification:** `tests/test_tester_parsing_2242.py` parametrizes
vitest, jest, pytest, playwright, and go output; `tests/test_tester_phase.py`
covers the dispatcher's disagreement-detection path end-to-end.

### S3 MEDIUM (Phase C) — Internal-consistency check

**Original finding (attempt-1):** The four reported integers were
accepted independently. `tests_run=10, passed=0, failed=0, skipped=3`
is internally inconsistent (7 tests unaccounted for) but currently
passed both guards.

**Fix:**
- `_dispatch_tester_outcome` now requires
  `tests_passed + tests_failed + tests_skipped == tests_run` whenever
  `tests_run > 0`. Inconsistency routes to `tests_inconclusive` with
  reason `counts_inconsistent` and both `expected_sum` and
  `actual_sum` attached to the message payload.
- `vacuous_pass.check_vacuous_pass` mirrors the consistency guard
  (without the message payload — the hook is pure).
- `prompts/tester.md` documents the requirement under the OUTPUT FORMAT
  block.

**Verification:** Both the hook and the dispatcher have explicit
fixtures (`test_counts_inconsistent_is_flagged`,
`test_counts_inconsistent_routes_inconclusive`) plus regressions
asserting consistent partial-skip still passes.

### S4 MEDIUM (Phase D) — Predicate spec drift

**Original finding (attempt-1):** Code used `>=` (vacuous_pass.py:167,
loops.py:1242); docstring + prompt + tests all said `==`. Both
descriptions match on consistent input but `>=` is strictly broader on
inconsistent input.

**Fix:**
- With Phase C rejecting inconsistent input upstream, equality is now
  the canonical statement. The predicate at both call sites is now
  `tests_skipped == tests_run` (and `tests_passed == 0`).
- Docstring in `equipa/hooks/vacuous_pass.py` updated to match.
- `prompts/tester.md` already used `==`; reaffirmed in the new
  contract-violation paragraph.

**Verification:** `test_phase_d_equality_predicate` asserts the path
fires on `skipped == run`. The Phase C guard above forbids
`skipped > run` from ever reaching this path.

## Out of Scope

Per task constraints:
- S5 (negative integer rejection) — not addressed; would require Zod-
  style schema validation at parse time.
- S6 (hook-path detector unused) — not addressed.
- S7 (`_branch_has_real_work` git shell-out review) — not addressed.

## Test Posture

| Suite | Result |
| --- | --- |
| `tests/test_tester_parsing_2242.py` (new, 14 cases) | 14 pass |
| `tests/test_vacuous_pass_guard.py` (5 new + 17 updated) | 22 pass |
| `tests/test_tester_phase.py` (4 new + 15 existing) | 19 pass |
| `tests/test_single_agent_vacuous_pass.py` (unchanged) | 11 pass |
| `tests/test_compaction_detection.py` (unchanged) | 28 pass |

`tests/test_cli_templates.py::test_import_accepts_non_claude_fixture`
fails on this branch AND on master (pre-existing fixture issue:
`tables/projects.jsonl` is referenced but not present in the test
manifest). Not caused by this dispatch.

## Counts

CRITICAL: 0
HIGH: 0
MEDIUM: 0
LOW: 0
INFO: 0
