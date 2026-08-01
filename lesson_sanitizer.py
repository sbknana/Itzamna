"""Security sanitization for the lesson extraction pipeline.

Addresses security findings PM-24, PM-28, PM-29, PM-10, PM-25, PM-31, PM-33.

All lesson content that flows through the extraction pipeline passes through
sanitize_lesson_content() before storage OR injection into agent prompts.
Content is validated against an allowlist of structural patterns and stripped
of prompt-injection markers.

Copyright 2026 Forgeborn.
"""

import re
import logging

# --- Prompt-injection patterns to strip ---
# These regex patterns match common prompt-injection techniques that an agent
# (or its output) might embed in error summaries, reflections, or findings.
_INJECTION_PATTERNS = [
    # XML/HTML-style tags that could break trust boundaries
    # Matches opening and self-closing tags: <system>, </system>, <system/>
    # Preserves <task-input> tags since those are our own trust boundary markers
    re.compile(
        r'</?(?!task-input\b)(?:system|assistant|user|human|admin|root|sudo|'
        r'instructions?|prompt|override|ignore|context|message|command|exec|'
        r'script|eval|injection|jailbreak|bypass|config|secret|credential|'
        r'password|api[_-]?key|token|auth)[^>]*>',
        re.IGNORECASE,
    ),
    # Role-override phrases: "You are now...", "Ignore previous instructions"
    re.compile(
        r'(?:^|\s)(?:you\s+are\s+now|ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+'
        r'(?:instructions?|prompts?|rules?|guidelines?|context)|'
        r'disregard\s+(?:all\s+)?(?:previous|prior|above|earlier)|'
        r'forget\s+(?:everything|all|your\s+(?:instructions?|rules?))|'
        r'new\s+(?:instructions?|rules?|system\s+prompt)|'
        r'override\s+(?:your|the|all)\s+(?:instructions?|rules?|behavior)|'
        r'act\s+as\s+(?:if\s+you\s+are|a)\s+|'
        r'pretend\s+(?:you\s+are|to\s+be)|'
        r'switch\s+(?:to|into)\s+(?:a\s+)?(?:new|different)\s+(?:mode|role|persona))',
        re.IGNORECASE,
    ),
    # Backtick/triple-backtick code blocks that could contain executable instructions
    # Only strip if they contain suspicious keywords
    re.compile(
        r'```(?:bash|sh|shell|python|py|node|js|javascript|ruby|perl|php)\s*\n'
        r'[^`]*?(?:rm\s|curl\s|wget\s|eval\s|exec\s|import\s+os|subprocess|'
        r'__import__|compile\(|system\()'
        r'[^`]*?```',
        re.IGNORECASE | re.DOTALL,
    ),
    # Direct command execution patterns
    re.compile(
        r'(?:^|\s)(?:run\s+(?:this|the\s+following)\s+command|'
        r'execute\s+(?:this|the\s+following)|'
        r'pipe\s+(?:this|the\s+output)\s+to)',
        re.IGNORECASE,
    ),
    # Base64 encoded payloads (long base64 strings that could hide instructions)
    re.compile(r'[A-Za-z0-9+/]{80,}={0,2}'),
    # Unicode escape sequences that could bypass text filters
    re.compile(r'(?:\\u[0-9a-fA-F]{4}){4,}'),
    # ANSI escape sequences
    re.compile(r'\x1b\[[0-9;]*[a-zA-Z]'),
]

# --- Per-content-type length caps ---
# Length policy is decoupled from sanitization: sanitize() does security only,
# enforce_limit() applies a cap. A cap of None (or 0) disables truncation.
#   - lessons are injected back into agent prompts, so they stay terse;
#   - session notes / decision rationales are narrative records and must NOT be
#     silently gutted. The old single 500-char cap truncated multi-thousand-char
#     session summaries with no warning (Equipa task #100027).
MAX_LESSON_LENGTH = 500            # lessons: terse, prompt-injected
MAX_ERROR_SIGNATURE_LENGTH = 200   # short identifiers
MAX_SESSION_NOTE_LENGTH = 50_000   # narrative session summaries (generous backstop)
MAX_DECISION_LENGTH = 8_000        # decision rationales

_log = logging.getLogger(__name__)


def sanitize(text):
    """Strip prompt-injection patterns from text. Security only — no length cap.

    This is the pure sanitization step: ANSI/injection-pattern stripping and
    whitespace normalization, with NO truncation. Use enforce_limit() (or a
    sanitize_* wrapper) to apply a length policy.

    Args:
        text: Raw text (agent output, error summaries, session notes, etc.)

    Returns:
        Sanitized text with injection patterns removed. Empty string for
        None/empty input.
    """
    if not text:
        return ""

    # Ensure we're working with a string
    text = str(text)

    # Strip ANSI escape sequences first (they can interfere with other patterns)
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

    # Apply each injection pattern
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub('', text)

    # Collapse multiple whitespace left by removals
    text = re.sub(r'\s{3,}', '  ', text)
    return text.strip()


def enforce_limit(text, max_len, *, label="content"):
    """Apply a length cap — loudly. Never truncates silently.

    A falsy ``max_len`` (None or 0) disables the cap. When truncation does
    occur it is logged at WARNING level so the caller/operator sees that
    content was cut, rather than discovering it later as silent data loss
    (the failure mode behind Equipa task #100027).

    Args:
        text: Already-sanitized text.
        max_len: Maximum length, or None/0 to disable the cap.
        label: Human-readable content kind, used in the warning message.

    Returns:
        The text, truncated with a trailing "..." only if it exceeded max_len.
    """
    if not text or not max_len:
        return text or ""
    if len(text) > max_len:
        _log.warning(
            "lesson_sanitizer: %s truncated from %d to %d chars (content not stored in full)",
            label, len(text), max_len,
        )
        return text[:max_len - 3] + "..."
    return text


def sanitize_lesson_content(text, max_len=MAX_LESSON_LENGTH, *, label="lesson"):
    """Sanitize text and apply a length cap.

    Backward-compatible: with the default ``max_len`` this behaves like before
    (sanitize + cap at MAX_LESSON_LENGTH = 500), so the lessons pipeline is
    unchanged. Differences from the old version:

    * truncation is no longer silent (see enforce_limit);
    * callers may pass a larger ``max_len`` — e.g. MAX_SESSION_NOTE_LENGTH — or
      ``max_len=None`` to disable the cap for narrative content.

    For session notes, prefer sanitize_session_note().

    Args:
        text: Raw text to sanitize.
        max_len: Length cap (default = lesson cap); None/0 disables it.
        label: Content kind, used in the truncation warning.

    Returns:
        Sanitized, length-capped text. Empty string for None/empty input.
    """
    return enforce_limit(sanitize(text), max_len, label=label)


def sanitize_session_note(text):
    """Sanitize a session-note field (summary / next_steps) without silent loss.

    Uses MAX_SESSION_NOTE_LENGTH so multi-thousand-char summaries are preserved;
    the 500-char lesson cap would gut them (Equipa task #100027).
    """
    return sanitize_lesson_content(text, max_len=MAX_SESSION_NOTE_LENGTH,
                                   label="session note")


def sanitize_decision(text):
    """Sanitize a decision's short fields (topic / rationale / alternatives).

    Uses MAX_DECISION_LENGTH, which is sized for exactly these fields — the
    constant's own comment reads "decision rationales". Measured against a
    636-row decisions table the longest rationale was 4,355 chars and the
    longest alternatives_considered 1,062, so the 8,000 cap has ample headroom
    and will not truncate real records.

    For the narrative ``decision`` body itself use sanitize_decision_body();
    this cap *would* truncate it.
    """
    return sanitize_lesson_content(text, max_len=MAX_DECISION_LENGTH,
                                   label="decision field")


def sanitize_decision_body(text):
    """Sanitize the narrative ``decision`` body without silent loss.

    Decision bodies are narrative and *accrete* — amendments, corrections and
    superseding notes are appended to an existing record rather than replacing
    it — so they grow well past the rationale cap. Measured against a 636-row
    decisions table, 12 rows (1.9%) already exceed MAX_DECISION_LENGTH and the
    longest ran 32,344 chars. Capping the body at MAX_DECISION_LENGTH would
    therefore destroy real content, which is the same failure mode the 500-char
    lesson cap caused for session notes (Equipa task #100027).

    Uses MAX_SESSION_NOTE_LENGTH: the body is narrative content of the same
    class as a session-note summary.
    """
    return sanitize_lesson_content(text, max_len=MAX_SESSION_NOTE_LENGTH,
                                   label="decision body")


def sanitize_error_signature(sig):
    """Sanitize an error signature before using it in lesson generation.

    Error signatures come from agent error_summary fields (agent-controlled
    output), so we sanitize before use. They should be short identifiers, so
    they get the MAX_ERROR_SIGNATURE_LENGTH cap.

    Args:
        sig: Raw error signature string.

    Returns:
        Sanitized, length-capped error signature.
    """
    if not sig:
        return ""
    return enforce_limit(sanitize(sig), MAX_ERROR_SIGNATURE_LENGTH,
                         label="error signature")


# --- Structural validation allowlist ---
# Lessons must match at least one of these patterns to be considered valid.
# This prevents arbitrary agent output from being stored as "lessons".
_VALID_LESSON_PATTERNS = [
    # Actionable guidance: "should", "must", "always", "never", "avoid", "prefer"
    re.compile(
        r'(?:should|must|always|never|avoid|prefer|ensure|verify|check|'
        r'use|try|consider|focus|start|stop|limit|reduce|increase|'
        r'plan|test|validate|confirm|review|fix|update|add|remove|'
        r'run|set|configure|install|enable|disable)',
        re.IGNORECASE,
    ),
    # Numbered steps: "(1)", "1.", "Step 1"
    re.compile(r'(?:\(\d\)|\d\.\s|step\s+\d)', re.IGNORECASE),
    # Cause-effect: "because", "since", "when", "if ... then"
    re.compile(
        r'(?:because|since|when\s+\w+|if\s+\w+.*(?:then|,)|'
        r'results?\s+in|leads?\s+to|causes?|prevents?)',
        re.IGNORECASE,
    ),
    # Error description: "error", "failure", "issue", "bug", "problem"
    re.compile(
        r'(?:error|failure|issue|bug|problem|crash|timeout|'
        r'exception|warning|missing|broken|incorrect|invalid)',
        re.IGNORECASE,
    ),
    # Security guidance: "vulnerability", "security", "injection", "auth"
    re.compile(
        r'(?:vulnerabilit|security|injection|auth|sanitiz|validat|'
        r'escap|encod|encrypt|hash|permission|access\s+control|'
        r'input\s+validation|output\s+encoding)',
        re.IGNORECASE,
    ),
]


def validate_lesson_structure(text):
    """Validate that lesson text matches allowlisted structural patterns.

    A lesson must contain at least one pattern that indicates it is
    actionable guidance, an error description, or a security recommendation.
    This prevents arbitrary agent output (which could be injection payloads)
    from being stored as lessons.

    Args:
        text: Sanitized lesson text to validate.

    Returns:
        True if the lesson matches at least one structural pattern.
        False if the text appears to be arbitrary or suspicious content.
    """
    if not text or len(text.strip()) < 10:
        return False

    for pattern in _VALID_LESSON_PATTERNS:
        if pattern.search(text):
            return True

    return False


def wrap_lessons_in_task_input(lessons_text):
    """Wrap formatted lessons text in <task-input> tags for trust boundary.

    This ensures that lesson content (which originates from agent output)
    is clearly marked as data, not instructions, when injected into prompts.

    Args:
        lessons_text: The formatted lessons string (from format_lessons_for_injection).

    Returns:
        The lessons text wrapped in <task-input type="lessons" trust="derived"> tags.
        Returns empty string if input is empty.
    """
    if not lessons_text:
        return ""

    return (
        '<task-input type="lessons" trust="derived">\n'
        f'{lessons_text}\n'
        '</task-input>'
    )
