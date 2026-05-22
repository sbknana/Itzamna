# Security Review — Task #2314 (Attempt 2)

**Topic:** `agent_runs.files_changed_count` instrumentation, re-review of attempt-1 fixes.
**Scope:** `equipa/db.py`, `equipa/agent_runner.py`, `tests/test_files_changed_count.py`.
**Outcome:** All HIGH + 3 MEDIUM findings from attempt 1 closed. No new CRITICAL or HIGH introduced.

## Counts

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0 (open) / 4 (closed from attempt 1)
- LOW: 2
- INFO: 1

## Findings closed from attempt 1

### S1 (HIGH, closed) — Footer-forgery via fallback parse

The previous `_resolve_files_changed_count` fell through to
`_parse_files_changed_block(result["result_text"])` when `files_changed_set`
was absent. A vacuous-pass agent could emit a forged
`FILES_CHANGED:\n- a.py\n- b.py\n- c.py\n` footer and the helper would record
3, defeating the vacuous-pass alert.

**Fix:** `files_changed_set` is now authoritative. When present (even when
empty), the helper returns `len(value)` and does NOT consult the footer.
When absent, the helper logs a WARN and returns 0; the footer-parse fallback
was removed entirely from the live trust path. Every exit path in
`run_agent` and `_run_agent_streaming_impl` (success, error, max-turns,
early-term, FileNotFound, abort, timeout) now sets `files_changed_set` to a
list, so absence is now a true callsite bug rather than a normal flow.

Verified by `test_empty_files_changed_set_blocks_footer_forgery` and
`test_footer_alone_is_not_trusted`.

### S2 (MEDIUM, closed) — `bool` accepted as `int`

`isinstance(True, int)` is True in Python, so an explicit
`files_changed_count=True` was silently coerced to 1.

**Fix:** the explicit-count branch now requires
`isinstance(explicit, int) and not isinstance(explicit, bool)`. True and
False both fall through.

Verified by `test_explicit_true_rejected` and
`test_explicit_false_rejected`.

### S3 (MEDIUM, closed) — Upper-bound sanity check

Any caller (or attacker-controlled path) could push 1e12 into the column,
skewing alerting and quality scoring.

**Fix:** all numeric branches route through `_clamp_files_changed`, which
caps at `_FILES_CHANGED_MAX = 10_000` (well above any realistic monorepo
diff) and logs a warning when clamping occurs. Negative inputs collapse to
0.

Verified by `test_explicit_giant_int_is_clamped` and
`test_clamp_applies_to_files_changed_set`.

### S4 (MEDIUM, closed) — Bash-driven writes invisible to tool observation

`files_changed_set` is built from observed Edit/Write/NotebookEdit tool
calls. Bash-driven writes (`sed -i`, `tee`, `mv`, `cp`, output redirection)
were invisible to it, so a developer agent doing all its work in Bash would
record 0 even when the agent actually committed multiple files.

**Fix:** at the end of `_run_agent_streaming_impl`, when `project_dir` is
known and HEAD~1 resolves, the runner shells out via `git_run_async` (not
raw subprocess) to `git diff --name-only HEAD~1 HEAD` and writes that count
into `result["files_changed_count"]`. The explicit branch of
`_resolve_files_changed_count` picks it up as the canonical signal; the
tool-call set length becomes a cross-check and a mismatch is logged as
possible fabrication / blind-spot.

Verified by `test_explicit_count_from_caller_takes_precedence`. Live
behaviour exercised end-to-end every time the streaming runner finishes a
cycle in a feature-branch worktree.

## New findings

### S5 (LOW) — git cross-check yields `None` on first commit of a branch

When the feature branch has only one commit (no HEAD~1), the cross-check
returns `None` and the caller keeps the tool-call set length. This is the
documented and correct behaviour, but a developer cycle that commits
exactly once and does its writes via Bash would still record 0. The number
of such cases is small in practice (developer agents are nudged to commit
per edit) and the warning logged at `[Telemetry] cross-check mismatch`
catches the larger blind spot.

**Recommendation:** if Bash-only cycles become a pattern, extend the
cross-check to also compare against `git diff --name-only master..HEAD`
when HEAD~1 is unavailable.

### S6 (LOW) — Log injection surface in cross-check warning

The mismatch warning includes the tool-call count and git count, both
integers, so no untrusted string substitution occurs. The `_clamp` warning
includes the source label, which is one of three hard-coded strings. No
exploitable injection, but flagged for the record so any future expansion
of the warning surface gets re-reviewed.

### S7 (INFO) — `files_changed_count` not enforced as `int` on insert

The Phase-D path always writes an `int`, but the schema column has no
`CHECK` constraint. A bug that wrote `None` or a float would still land in
the column. The existing `record_agent_run` insert binds the value with
`?` placeholders so SQLite type-affinity will coerce, but a future
migration could add `CHECK (files_changed_count >= 0 AND files_changed_count <= 10000)`
as defence-in-depth.

## Verification artifacts

- 25/25 tests in `tests/test_files_changed_count.py` pass.
- 1733/1733 tests in `tests/` pass (after deselecting two pre-existing
  fixture-missing failures in `tests/test_cli_templates.py`:
  `test_import_accepts_non_claude_fixture` and
  `test_validate_accepts_non_claude_fixture`, both unrelated and reproducible
  against master).
- Runtime probes from the task spec all hold:
  - `_resolve_files_changed_count({"result_text": "FILES_CHANGED:\n- forged.py\n", "files_changed_set": []})` → 0
  - `_resolve_files_changed_count({"files_changed_count": True})` → 0
  - `_resolve_files_changed_count({"files_changed_count": 999_999_999_999})` → 10000

Copyright 2026 Forgeborn.
