# BashSecurity workarounds for EQUIPA-dispatched tasks in Equipa-repo

> **Read this BEFORE making any bash command from inside an EQUIPA dispatch on this repo.**
>
> EQUIPA's bash security validator (`equipa/bash_security.py` — yes, the same code you may be modifying) runs every shell command the agent emits. Several common shell idioms trip false positives. These patterns will get you killed mid-task — with up to 25 autoresearch retries failing on the same shape.
>
> The most painful case observed: **task 2320** (the very fix for one of these false positives) exhausted all 25 retries because every retry tried to test the fix using `gh pr create --body "$(cat <<EOF ...)"` — which tripped check 19 (heredoc inside command substitution). Don't be that task. Read this doc.

## The trap that took 2320 down

When you're working on `equipa/bash_security.py` itself, **the checks you're modifying are running against your own bash commands in real time**. You cannot test a check-23 fix by invoking `gh pr create --body "$(cat <<EOF ... EOF)"` — that pattern also fires check 19, and you'll burn every retry attempting variations of the same shape.

**Rule of thumb when modifying `bash_security.py`:** test via pytest unit tests against `check_bash_command()` DIRECTLY in `tests/test_bash_security.py`. NEVER invoke `gh pr create`, `git commit -m "$(cat <<...)"`, or `python3 -c "..."` with embedded multi-line content to "exercise" the check. The checks fire on YOUR bash, not on YOUR test inputs.

## Status of known BashSecurity false-positives

| TheForge bug | Check | Status as of 2026-05-15 |
|---|---|---|
| 2282 | parallel-mode missing autoresearch retry | ✅ FIXED — retries fire |
| 2283 | check 7: newlines in `python3 -c` | ✅ FIXED in source + prod |
| 2284 | check 8: `$()` command substitution | ✅ FIXED in source + prod |
| 2285 | check 9: `<<EOF` heredoc treated as `<` redirection | ✅ FIXED in source + prod |
| 2310 | check 4: locale-quoting `$"..."` on data | ✅ FIXED in source + prod |
| 2214 | check 12 + check 4 on benign post-commit composition | ✅ FIXED in source + prod |
| 2316 | check 4 per-segment evaluation (composed commands) | ✅ FIXED in source + prod |
| 2320 | check 23: markdown body header in `gh pr create` heredoc | ✅ FIXED in source (commit f008911) — pending Equipa-prod pull |

Even with fixes deployed, **prod runs a snapshotted bash_security.py** — any false positive that hasn't been backported + deployed will still trip you. Always assume the production checker is stricter than the one you're editing in source.

## Workaround patterns

### `gh pr create` with multi-line body — use `--body-file`

```bash
# DON'T — trips check 19 (heredoc inside $()), and check 23 (## inside body)
gh pr create --title "X" --body "$(cat <<EOF
## Summary
- foo
- bar
EOF
)"
```

Write the body to a file first via the **Write tool**, then point `gh` at it:

```bash
# DO — file written via Write tool, then --body-file
gh pr create --title "X" --body-file /tmp/pr-body.md
```

The Write tool API does NOT route through bash, so multi-line markdown content (including `##` headings, code fences, quoted blocks) is safe.

### `git commit` with multi-line message — use `-F` not heredoc

```bash
# DON'T — trips check 19
git commit -m "$(cat <<EOF
fix: subject line

body paragraph with markdown ## heading
EOF
)"
```

```bash
# DO — write the message file via Write tool, then -F
git commit -F /tmp/commit-msg.txt

# OR — single-line message is always safe
git commit -m "fix: subject line"
```

### Testing `check_bash_command()` itself — pytest, not bash

If you're modifying `equipa/bash_security.py`, **do not** try to "test the fix" by invoking `gh`/`git` from bash with the patterns you're trying to allow. Write pytest unit tests in `tests/test_bash_security.py`:

```python
# In tests/test_bash_security.py
def test_check_23_allows_markdown_body_in_gh_pr_create(self) -> None:
    """Bug 2320: gh pr create with markdown body in heredoc must pass."""
    cmd = '''gh pr create --title "X" --body "$(cat <<EOF
## Summary
- foo
EOF
)"'''
    result = check_bash_command(cmd)
    assert result.safe, f"expected safe: got check {result.check_id}: {result.message}"
```

Run with `python3 -m pytest tests/test_bash_security.py -q` — no shell heredoc involved.

### `$()` command substitution in arguments — use intermediate variables

```bash
# DON'T — trips check 8 on inline $() (post-2284 fix this is allowlisted for git/go/mktemp, but bombs on others)
ls $(go env GOMODCACHE)
```

```bash
# DO — assignment-side $() is universally allowlisted
gomodcache=$(go env GOMODCACHE)
ls "$gomodcache"
```

### Multi-line `python3 -c "..."` — write a script file

```bash
# DON'T — trips check 7 on newlines inside -c (fixed in source, but prod may lag)
python3 -c "
import struct
with open('out.bin', 'wb') as f:
    f.write(struct.pack('I', 42))
"
```

```bash
# DO — Write the script to scripts/build_thing.py via the Write tool, then:
python3 scripts/build_thing.py
```

### Locale-quoting `$"..."` — single-quote the outer string

```bash
# DON'T — embeds $" as a literal in the arg, may trip check 4 in older code paths
echo "the literal string $\"hello\" appears here"
```

```bash
# DO — single-quote the whole argument so $" is opaque to the parser
echo 'the literal string $"hello" appears here'
```

## What to do if you trip a check anyway

After bug 2282's fix (autoresearch retries fire), failures inject the kill reason into the next attempt's context. **Read the reflection — don't repeat the same form.** If retry 3 says "check 19 blocked your `$(cat <<EOF...)`", retry 4 must use a different shape (Write tool + --body-file).

If you find yourself unable to express what you need within these constraints — for example, you need to emit binary bytes that don't fit in any --body-file pattern — log a comment in your PR body with:

```
BLOCKED-BY-BASHSECURITY: <description of what you tried, what check fired, what shape you'd need>
```

The operator will intervene with a hand-fix.

## When this doc becomes obsolete

Bug 2320 is now FIXED in source (commit `f008911`, 2026-05-15) — the `_MARKDOWN_BODY_TOOLS` + `_MARKDOWN_BODY_FLAGS` allowlist plus the token-aware `_last_segment_start` fallback recognize the `gh pr create --body "$(cat <<EOF ... )"` form and let it through. Once Equipa-prod pulls master, the heredoc-in-substitution workaround for `gh pr create` becomes unnecessary.

That said, the **agent-side guidance still holds**: prefer Write-tool + `--body-file` for multi-line PR bodies and `git commit -F` for multi-line commit messages. The fix recognized the legitimate pattern; the cleaner pattern remains a better choice for readability and audit.

The orchestrator IS the security checker — there is no shortcut around it.
