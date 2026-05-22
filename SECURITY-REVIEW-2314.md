# Security Review — Task #2314 (Attempt 3)

**Topic:** `agent_runs.files_changed_count` instrumentation, re-review of attempt-2 fixes.
**Scope:** `equipa/db.py`, `equipa/agent_runner.py`, `tests/test_files_changed_count.py`.
**Outcome:** Attempt-2 S1 HIGH + S2 MED + S3 MED all closed by Phase A3
(unified pre_head..post_head diff range). No new CRITICAL or HIGH introduced.

## Counts

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 2 (carried from attempt 2 — S5 narrower scope; S6 unchanged)
- INFO: 1 (carried from attempt 2 — S7 unchanged)

## Findings closed in attempt 3

### S1 (HIGH, closed) — Reviewer rows leaked the developer's diff

Attempt-2 ran `git diff --name-only HEAD~1 HEAD` unconditionally at the end
of `_run_agent_streaming_impl` and assigned the result to
`result["files_changed_count"]`. When a code-reviewer or security-reviewer
agent ran after the developer in the **same worktree**, `HEAD~1..HEAD` still
pointed at the developer's last commit — so the reviewer's `agent_runs` row
recorded the developer's file count, even though the reviewer wrote nothing.

That is the **exact failure mode the task was filed to detect**: the vacuous-
pass alert (`success=1 AND files_changed_count=0`) would *never* trip for
reviewer rows, because reviewers always inherited a non-zero count from
upstream commits in the worktree.

**Fix (Phase A3):** capture `pre_head` at the START of
`_run_agent_streaming_impl`, before any agent work. At the end, capture
`post_head`. Only override `result["files_changed_count"]` when
`pre_head != post_head` (i.e. at least one commit landed during this run).
Non-writer roles see `pre_head == post_head` and the override is skipped;
their `files_changed_set` (now authoritative on every exit path by attempt-
2 Phase A) determines the recorded count.

Verified by `test_no_commits_means_pre_head_equals_post_head` and the
existing `test_security_reviewer_records_zero_files`.

### S2 (MEDIUM, closed) — Multi-commit cycles under-counted

`HEAD~1..HEAD` only sees the LAST commit. A developer cycle that committed
Phases A/B/C/D + tests + docs (5–6 commits is normal in this repo) showed
only the last commit's files — chronically under-counting productive
cycles, and inverting the vacuous-pass signal for cycles that did the most
work.

**Fix (Phase A3):** the diff range is now `pre_head..post_head`, covering
every commit made during the cycle. The helper deduplicates filenames
(returns `len(set(...))`) so a file edited in multiple commits is counted
once. A 3-commit cycle touching 6 unique files now reports 6.

Verified by `test_multi_commit_cycle_counts_all_unique_files` and
`test_unique_files_across_commits_not_double_counted`.

### S3 (MEDIUM, closed) — Deferred-commit override of fresh edits

If the developer used Edit/Write but the orchestrator committed the work
AFTER `_run_agent_streaming_impl` returned, `HEAD~1..HEAD` measured the
PREVIOUS commit. `files_changed_set` held the real edits; the git-diff
fallback held stale ones. Attempt-2's unconditional override preferred the
smaller (stale) signal — inverse of Phase D's intent.

**Fix (Phase A3):** when no commits land during the cycle
(`pre_head == post_head`), the override does not fire. The in-memory
`files_changed_set` written during the cycle is recorded verbatim. When the
orchestrator commits later, the value already in `agent_runs` reflects the
agent's actual writes — not a stale measurement from a different cycle.

Verified by `test_no_commits_means_pre_head_equals_post_head` (the helper
correctly returns the same SHA when no commits land) plus the existing
`test_developer_with_edits_records_nonzero` and
`test_explicit_count_from_caller_takes_precedence`.

## Findings carried forward

### S5 (LOW, carried) — git cross-check yields `None` if pre_head missing

The Phase-A3 helper `_git_rev_parse_head` returns `None` when the repo has
no commits yet or the directory is not a git repo. The caller skips the
override and trusts `files_changed_set`. This is the intended graceful
degradation — verified by `test_rev_parse_returns_none_when_no_commits`
and `test_rev_parse_returns_none_for_non_repo`. Recorded as LOW because the
graceful path is exercised only on fresh worktrees and could in principle
mask a worktree-corruption case.

### S6 (LOW, carried) — Log injection surface in cross-check warning

The mismatch warning now includes both abbreviated commit SHAs (8 hex
chars, regex-safe) and integer counts. No untrusted string substitution
occurs. No exploitable injection; flagged for re-review if the warning
surface ever takes string input.

### S7 (INFO, carried) — `files_changed_count` schema constraint

The Phase-A3 path always writes an `int`. The schema column still has no
`CHECK` constraint. A future migration could add
`CHECK (files_changed_count >= 0 AND files_changed_count <= 10000)` as
defence-in-depth.

## Verification artifacts

- **31/31** tests in `tests/test_files_changed_count.py` pass (24 attempt-2 +
  7 Phase A3).
- **1767/1767** tests in the full suite pass (excluding the pre-existing
  `equipa/integration_test.py` collection issue that aborts the runner at
  import time; unrelated to this task).
- Attempt-3 fixture probes:
  - Multi-commit cycle: 3 commits × 2 unique files each → count == 6 (not 2).
  - No-commit reviewer role: `pre_head == post_head` → override skipped,
    `files_changed_count` taken from `files_changed_set` (0 for a reviewer).
  - Bash-driven writes: 1 commit touching 2 files via Bash → count == 2.
  - Same file in 2 commits → counted once (set dedup).
- Attempt-2 probes still hold:
  - `_resolve_files_changed_count({"result_text": "FILES_CHANGED:\n- forged.py\n", "files_changed_set": []})` → 0
  - `_resolve_files_changed_count({"files_changed_count": True})` → 0
  - `_resolve_files_changed_count({"files_changed_count": 999_999_999_999})` → 10000

## Out of scope

S4 (dead-code `_parse_files_changed_block`), S5 bare-dash sentinel, S6–S8
LOW polish — explicitly excluded by the task spec.

Copyright 2026 Forgeborn.
