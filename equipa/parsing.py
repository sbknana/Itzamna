"""EQUIPA parsing module — output parsing, token estimation, and text extraction.

Layer 4: Pure parsing/text-processing functions. Dependencies on constants
only, plus late imports for sanitize_lesson_content (forgesmith integration).

Extracted from forge_orchestrator.py as part of Phase 2 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import gzip
import json
import math
import re

from equipa.constants import EARLY_TERM_KILL_TURNS, SYSTEM_PROMPT_DYNAMIC_BOUNDARY

# --- Token Budget Constants ---
# Anthropic recommendation: ~4 chars/token for Claude
CHARS_PER_TOKEN: int = 4
SYSTEM_PROMPT_TOKEN_TARGET: int = 8000   # 8K token target
SYSTEM_PROMPT_TOKEN_HARD_LIMIT: int = 10000  # absolute max before aggressive trimming
EPISODE_REDUCTION_THRESHOLD: int = 6000  # reduce episodes from 3->2 above this


# --- Token Estimation ---

def estimate_tokens(text: str) -> int:
    """Estimate token count using ~4 chars/token approximation for Claude.

    Always returns at least 1 (a call site that estimates "no tokens" for a
    real input is a footgun for budget arithmetic). Rounds up so partial
    chunks count. This is the canonical implementation; rlm_decompose.py
    re-exports this symbol.
    """
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def compute_keyword_overlap(text_a: str, text_b: str) -> float:
    """Compute simple keyword overlap score between two texts.

    Returns a float between 0.0 and 1.0 representing the fraction of
    words in common (Jaccard similarity on word sets).
    """
    if not text_a or not text_b:
        return 0.0
    words_a = set(re.findall(r'\w+', text_a.lower()))
    words_b = set(re.findall(r'\w+', text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _compute_ngram_jaccard(text_a: str, text_b: str, n: int = 3) -> float:
    """Compute n-gram Jaccard similarity between two texts.

    Uses character n-grams for fuzzy matching (catches typos, variations).
    Returns similarity score 0.0-1.0.
    """
    if not text_a or not text_b:
        return 0.0

    def ngrams(s: str, n: int) -> set[str]:
        s_lower = s.lower()
        return set(s_lower[i:i+n] for i in range(len(s_lower) - n + 1))

    ngrams_a = ngrams(text_a, n)
    ngrams_b = ngrams(text_b, n)

    if not ngrams_a or not ngrams_b:
        return 0.0

    intersection = ngrams_a & ngrams_b
    union = ngrams_a | ngrams_b
    return len(intersection) / len(union)


def deduplicate_lessons(lessons: list[dict]) -> list[dict]:
    """Remove semantically duplicate lessons based on 60%+ word overlap.

    Keeps the first (highest times_seen) lesson when duplicates are found.
    Returns deduplicated list, max 5 lessons.
    """
    if not lessons:
        return []

    unique: list[dict] = []
    for lesson in lessons:
        lesson_text = lesson.get("lesson", "")
        is_dup = False
        for existing in unique:
            overlap = compute_keyword_overlap(lesson_text, existing.get("lesson", ""))
            if overlap >= 0.6:
                is_dup = True
                break
        if not is_dup:
            unique.append(lesson)
        if len(unique) >= 5:
            break

    return unique


# --- Section Extraction ---

def _extract_section(text: str, marker: str, max_lines: int = 1) -> str:
    """Extract a named section from structured agent output.

    Finds the last occurrence of 'MARKER:' and returns content.
    For max_lines=1, returns just the header line value.
    For max_lines>1, returns header + continuation lines.
    For max_lines=-1 (bullet mode), returns header + bullet lines.
    """
    start = text.rfind(marker + ":")
    if start == -1:
        return ""
    lines = text[start:].split("\n")
    if max_lines == -1:
        # Bullet mode: header + up to 5 bullet lines
        kept = [lines[0]]
        for line in lines[1:6]:
            s = line.strip()
            if s.startswith("-") or s == "":
                kept.append(line)
            else:
                break
        return "\n".join(kept).strip()
    if max_lines == 1:
        return lines[0].strip()
    return "\n".join(lines[:max_lines]).strip()


def _deduplicate_log_lines(lines: list[str], threshold: float = 0.85) -> list[str]:
    """Deduplicate similar log lines using n-gram Jaccard similarity.

    Groups identical or nearly-identical lines, showing count instead of repeating.
    Example: "Error: timeout\n" × 50 → "Error: timeout (×50)"
    """
    if not lines:
        return lines

    result: list[str] = []
    line_groups: dict[str, int] = {}  # line → count
    group_reps: list[str] = []  # first representative of each group

    for line in lines:
        if not line.strip():
            result.append(line)
            continue

        # Find matching group
        matched_idx = -1
        for idx, rep in enumerate(group_reps):
            sim = _compute_ngram_jaccard(line, rep)
            if sim >= threshold:
                matched_idx = idx
                break

        if matched_idx >= 0:
            # Increment existing group
            rep = group_reps[matched_idx]
            line_groups[rep] += 1
        else:
            # New group
            group_reps.append(line)
            line_groups[line] = 1

    # Emit deduplicated lines with counts
    for rep in group_reps:
        count = line_groups[rep]
        if count > 1:
            result.append(f"{rep.rstrip()} (×{count})")
        else:
            result.append(rep)

    return result


def _aggressive_compress_code(text: str) -> str:
    """Aggressively compress code blocks by stripping comments, blank lines, normalizing indentation.

    Reduces code output by 40-60% while preserving structure.
    """
    lines = text.splitlines()
    compressed: list[str] = []
    in_code = False
    prev_blank = False

    for line in lines:
        # Detect code blocks
        if line.strip().startswith("```"):
            in_code = not in_code
            compressed.append(line)
            continue

        if not in_code:
            compressed.append(line)
            continue

        # Strip comments (Python, JS, Go, etc.)
        stripped = re.sub(r'//.*$', '', line)  # Single-line //
        stripped = re.sub(r'#.*$', '', stripped)  # Python #
        stripped = stripped.rstrip()

        # Collapse consecutive blank lines
        is_blank = not stripped
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank

        # Normalize indentation (collapse to 2 spaces)
        if stripped:
            indent = len(line) - len(line.lstrip())
            normalized_indent = ' ' * min(indent // 2, 4)
            stripped = normalized_indent + stripped.lstrip()

        compressed.append(stripped)

    return "\n".join(compressed)


def compact_agent_output(
    raw_output: str,
    max_words: int = 200,
    agent_id: str | None = None,
    session_dir: str | None = None,
    persist_threshold: int = 50_000,
) -> str:
    """Compact raw agent output into a concise summary preserving actionable details.

    If agent_id and session_dir are provided, large outputs (>50KB by default) are
    persisted to disk before compaction, preventing context bloat.

    Applies n-gram Jaccard deduplication to repetitive log lines and aggressive
    compression to code blocks (strips comments, collapses blank lines, normalizes indentation).

    Args:
        raw_output: Raw agent output text
        max_words: Maximum words in compacted output (default 200)
        agent_id: Optional agent identifier for persistence (e.g., "developer-123-turn-5")
        session_dir: Optional session directory path for persistence
        persist_threshold: Size threshold in bytes for persistence (default 50KB)

    Returns:
        Compacted output, or persistence reference if output was too large
    """
    if not raw_output:
        return ""

    # Apply tool result persistence if configured
    if agent_id and session_dir:
        from equipa.tool_result_storage import process_agent_output
        raw_output = process_agent_output(raw_output, agent_id, session_dir, persist_threshold)

        # If output was persisted, return the reference message directly (no further compaction)
        from equipa.tool_result_storage import is_content_already_compacted
        if is_content_already_compacted(raw_output):
            return raw_output

    # Apply aggressive code compression
    raw_output = _aggressive_compress_code(raw_output)

    # Apply n-gram Jaccard log line deduplication
    lines = raw_output.splitlines()
    deduped_lines = _deduplicate_log_lines(lines)
    raw_output = "\n".join(deduped_lines)

    # Late import to avoid circular dependency — sanitizer may not be available
    from lesson_sanitizer import sanitize_lesson_content  # HARD dependency — no silent fallback

    sections = {
        "SUMMARY": _extract_section(raw_output, "SUMMARY"),
        "FILES_CHANGED": _extract_section(raw_output, "FILES_CHANGED", max_lines=-1),
        "BLOCKERS": _extract_section(raw_output, "BLOCKERS"),
        "DECISIONS": _extract_section(raw_output, "DECISIONS"),
        "REFLECTION": _extract_section(raw_output, "REFLECTION", max_lines=3),
    }

    # Sanitize all extracted sections to prevent cross-agent prompt injection (PS-02)
    for key in sections:
        if sections[key]:
            sections[key] = sanitize_lesson_content(sections[key])

    parts: list[str] = []
    if sections["SUMMARY"]:
        parts.append(sections["SUMMARY"])
    if sections["FILES_CHANGED"]:
        parts.append(sections["FILES_CHANGED"])
    for key in ("BLOCKERS", "DECISIONS"):
        if sections[key] and "none" not in sections[key].lower():
            parts.append(sections[key])
    if sections["REFLECTION"]:
        parts.append(sections["REFLECTION"])

    compact = "\n".join(parts)

    if not compact.strip():
        words = raw_output.split()
        compact = ("..." + " ".join(words[-max_words:])) if len(words) > max_words else raw_output

    words = compact.split()
    if len(words) > max_words:
        compact = " ".join(words[:max_words]) + "..."

    return compact


def _trim_prompt_section(
    prompt: str, section_heading: str, max_chars: int | None = None
) -> str:
    """Remove or truncate a section from the prompt by its ## heading.

    If max_chars is None, removes the entire section.
    If max_chars is given, truncates the section content to that limit.
    Returns the modified prompt.
    """
    start = prompt.find(section_heading)
    if start == -1:
        return prompt

    # Find the next section boundary (## or ---)
    next_section = len(prompt)
    for marker in ["\n## ", "\n---"]:
        pos = prompt.find(marker, start + len(section_heading))
        if pos != -1 and pos < next_section:
            next_section = pos

    if max_chars is None:
        # Remove entire section
        return prompt[:start] + prompt[next_section:]
    else:
        # Truncate section content
        section = prompt[start:next_section]
        if len(section) > max_chars:
            section = section[:max_chars] + "\n[...trimmed...]\n"
        return prompt[:start] + section + prompt[next_section:]


# --- Marker Extraction ---

def _extract_marker_value(
    text: str, marker: str, multiline: bool = False
) -> str | None:
    """Extract value after a MARKER: line from structured agent output.

    For single-line markers, returns the value after the colon.
    For multiline, collects continuation lines until the next known marker.
    Returns None if not found or value is 'none'.
    """
    if not text:
        return None
    section_markers = ("RESULT:", "SUMMARY:", "FILES_CHANGED:", "DECISIONS:",
                       "BLOCKERS:", "REFLECTION:", "```")
    lines = text.splitlines()
    collected: list[str] = []
    in_section = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(marker):
            value = stripped.split(":", 1)[1].strip()
            if not multiline:
                return value if value and value.lower() != "none" else None
            in_section = True
            if value and value.lower() != "none":
                collected.append(value)
            continue
        if in_section:
            if any(stripped.startswith(m) for m in section_markers if m != marker):
                break
            if stripped:
                collected.append(stripped)

    result = " ".join(collected).strip()
    return result if result else None


def parse_reflection(result_text: str) -> str | None:
    """Extract REFLECTION text from agent structured output."""
    return _extract_marker_value(result_text, "REFLECTION:", multiline=True)


def parse_approach_summary(result_text: str) -> str | None:
    """Extract SUMMARY text from agent output for the episode record."""
    return _extract_marker_value(result_text, "SUMMARY:")


# --- Failure Classification ---

# Keyword patterns for failure classification — data-driven instead of if/elif chains
_FAILURE_KEYWORD_PATTERNS: dict[str, list[str]] = {
    "build_failure": ["syntaxerror", "compileerror", "build failed", "build error",
                      "cannot find module", "type error", "typeerror"],
    "import_error": ["modulenotfounderror", "importerror", "no module named",
                     "module not found", "cannot resolve", "dependency",
                     "missing package", "version conflict"],
    "test_failure": ["assertionerror", "test failed", "tests failed",
                     "assertion failed", "expected", "pytest"],
    "environment_error": ["permission denied", "connection refused", "eacces",
                          "econnrefused", "no such file or directory"],
}

# Priority order for selecting primary failure class
_FAILURE_PRIORITY: list[str] = [
    "analysis_paralysis", "timeout", "build_failure", "import_error",
    "test_failure", "environment_error", "wrong_approach", "max_turns",
]


def classify_agent_failure(
    outcome: str, result: dict, result_text: str = ""
) -> dict | None:
    """Classify an agent failure into structured categories.

    Returns a dict with failure_class, secondary_classes, confidence,
    signals, and raw_errors — or None if no failure detected.
    """
    if not isinstance(result, dict):
        result = {}

    errors = result.get("errors", [])
    unique_errors = list(dict.fromkeys(errors))[:5]
    truncated_errors = [e[:200] for e in unique_errors]
    num_turns = result.get("num_turns", 0)
    early_terminated = result.get("early_terminated", False)
    early_term_reason = result.get("early_term_reason", "")

    lower_errors = " ".join(e.lower() for e in unique_errors)
    lower_result = result_text.lower() if result_text else ""
    lower_reason = early_term_reason.lower()
    signals: list[str] = []
    classes_detected: list[str] = []

    # --- Special cases (need contextual logic, not just keyword matching) ---
    # Analysis paralysis
    if "consecutive turns without file changes" in lower_reason:
        classes_detected.append("analysis_paralysis")
        signals.append(f"early_term: {early_term_reason[:120]}")
    elif "consecutive" in lower_errors and "without file changes" in lower_errors:
        classes_detected.append("analysis_paralysis")
        signals.append("error mentions consecutive turns without file changes")
    elif (num_turns >= EARLY_TERM_KILL_TURNS
          and "files_changed: none" in lower_result):
        classes_detected.append("analysis_paralysis")
        signals.append(f"high turn count ({num_turns}) with no files changed")

    # Timeout
    if "timed out" in lower_reason or "timed out" in lower_errors:
        classes_detected.append("timeout")
        signals.append("process timed out")
    elif outcome == "tester_timeout":
        classes_detected.append("timeout")
        signals.append("tester timeout outcome")

    # Max turns
    if any(k in lower_errors for k in ("max_turns", "max turns", "error_max_turns")) \
            or outcome == "developer_max_turns":
        classes_detected.append("max_turns")
        signals.append("agent hit max turns limit")

    # --- Keyword-driven classification (build, import, test, env) ---
    for failure_class, keywords in _FAILURE_KEYWORD_PATTERNS.items():
        # Test failure has special outcome-based detection
        if failure_class == "test_failure" and outcome in ("cycles_exhausted", "tester_failed"):
            classes_detected.append("test_failure")
            signals.append(f"outcome indicates test failure: {outcome}")
            continue
        sources = [lower_errors] if failure_class == "environment_error" else [lower_errors, lower_result]
        for kw in keywords:
            if any(kw in s for s in sources):
                if failure_class not in classes_detected:
                    classes_detected.append(failure_class)
                    signals.append(f"{failure_class} signal: {kw}")
                break

    # Wrong approach / loop detection
    if "loop detected" in lower_reason or "repeated the same operation" in lower_reason:
        classes_detected.append("wrong_approach")
        signals.append(f"loop detection: {early_term_reason[:120]}")
    elif "stuck" in lower_reason and "repeated" in lower_reason:
        classes_detected.append("wrong_approach")
        signals.append(f"stuck agent: {early_term_reason[:120]}")
    elif outcome == "no_progress":
        classes_detected.append("wrong_approach")
        signals.append("no progress across multiple cycles")
    elif ("result: blocked" in lower_result and "result: failed" in lower_result
          and "wrong_approach" not in classes_detected):
        classes_detected.append("wrong_approach")
        signals.append("agent reported both blocked and failed")

    # --- Determine primary class and confidence ---
    if not classes_detected:
        if unique_errors:
            return {"failure_class": "unknown", "secondary_classes": [],
                    "confidence": "low", "signals": ["unclassified error(s) present"],
                    "raw_errors": truncated_errors}
        return None

    primary = next((c for c in _FAILURE_PRIORITY if c in classes_detected), "unknown")
    secondary = [c for c in classes_detected if c != primary]
    confidence = ("high" if len(signals) >= 2
                  or early_terminated
                  or outcome in ("cycles_exhausted", "no_progress")
                  else "medium")

    return {"failure_class": primary, "secondary_classes": secondary,
            "confidence": confidence, "signals": signals[:5],
            "raw_errors": truncated_errors}


def parse_error_patterns(
    result: dict, outcome: str | None = None, result_text: str | None = None
) -> str | None:
    """Extract and classify error patterns from agent result for episode record.

    Produces structured JSON with failure classification when the agent failed.
    Falls back to raw error string for backward compatibility if classification
    yields nothing.
    """
    if not isinstance(result, dict):
        return None

    errors = result.get("errors", [])
    rt = result_text or result.get("result_text", "")

    # Attempt structured classification
    classification = classify_agent_failure(outcome, result, rt)
    if classification:
        return json.dumps(classification)

    # Fallback: raw error join (backward compat for edge cases)
    if errors:
        unique = list(dict.fromkeys(errors))
        return "; ".join(e[:200] for e in unique[:5])

    return None


def compute_initial_q_value(outcome: str) -> float:
    """Set initial Q-value based on task outcome.

    Success starts higher (0.7), failure starts lower (0.3),
    partial/blocked at neutral (0.5).
    """
    if outcome in ("tests_passed", "no_tests"):
        return 0.7
    elif outcome in ("developer_failed", "cycles_exhausted"):
        return 0.3
    else:
        # blocked, timeout, no_progress, etc.
        return 0.4


# --- Structured Output Parsing ---

def _parse_structured_output(text: str, schema: dict) -> dict:
    """Generic parser for structured agent output with MARKER: value lines.

    Schema is a dict mapping marker names to their types:
        str   — single-line string value
        int   — single-line integer value
        list  — multi-line bullet list (lines starting with "- ")
    Returns a dict with parsed values.
    """
    result: dict = {}
    for marker, typ in schema.items():
        if typ is list:
            result[marker.lower().replace(" ", "_")] = []
        elif typ is int:
            result[marker.lower().replace(" ", "_")] = 0
        else:
            result[marker.lower().replace(" ", "_")] = "" if marker != "RESULT" else "unknown"

    if not text:
        return result

    current_list_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()

        # Check all schema markers
        matched = False
        for marker, typ in schema.items():
            key = marker.lower().replace(" ", "_")
            if stripped.startswith(marker + ":"):
                value = stripped.split(":", 1)[1].strip()
                if typ is int:
                    try:
                        result[key] = int(value)
                    except ValueError:
                        pass
                elif typ is list:
                    current_list_key = key
                else:
                    result[key] = value.lower() if marker == "RESULT" else value
                if typ is not list:
                    current_list_key = None
                matched = True
                break

        # Collect bullet items for list sections
        if not matched and current_list_key and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item.lower() != "none":
                result[current_list_key].append(item)
        elif not matched and stripped and not stripped.startswith("-"):
            current_list_key = None

    return result


# Schemas for structured agent output parsing
_TESTER_SCHEMA: dict = {
    "RESULT": str, "TEST_FRAMEWORK": str, "TESTS_RUN": int,
    "TESTS_PASSED": int, "TESTS_FAILED": int, "TESTS_SKIPPED": int,
    "SUMMARY": str, "FAILURE_DETAILS": list, "RECOMMENDATIONS": list,
}

_DEVELOPER_FILES_SCHEMA: dict = {"FILES_CHANGED": list}


def parse_tester_output(result_text: str) -> dict:
    """Parse structured output from the Tester agent.

    Task #2242 Phase A/C3/F3: TESTS_SKIPPED is REQUIRED in the tester
    contract. The generic parser silently defaults missing int fields to 0,
    which lets drift / truncation / regression / adversarial misreport bypass
    the all-skipped guard (a tester that omits the line produces
    tests_skipped=0, the predicate ``tests_skipped == tests_run`` fails for
    any tests_run > 0, and the 2236-class loophole reopens).

    The parser is now the single source of truth: ``tests_skipped`` is the
    parsed integer when the line is present AND its value parses as int;
    otherwise it is ``None`` (the contract-violation sentinel). Call sites
    MUST treat ``None`` as "fail closed → tests_inconclusive" and MUST NOT
    coerce it to 0 with ``or 0``. The legacy ``tests_skipped_present``
    sidechannel is removed — ``tests_skipped is None`` is the canonical
    signal.

    Phase F3: presence is not sufficient — the value must also be a
    parseable int. A line ``TESTS_SKIPPED: (see comment)`` is treated as a
    contract violation (returns ``None``), not as 0.
    """
    parsed = _parse_structured_output(result_text, _TESTER_SCHEMA)
    # Backward compat: default test_framework to "none"
    if not parsed.get("test_framework"):
        parsed["test_framework"] = "none"
    # Phase C3/F3: parser is the source of truth. None on absent OR
    # unparseable value; the int on a clean marker line.
    parsed["tests_skipped"] = _required_int(result_text, "TESTS_SKIPPED")
    return parsed


def _required_int(text: str, marker: str) -> int | None:
    """Return the int value of ``MARKER: <int>`` or ``None`` on contract violation.

    Task #2242 Phase C3/F3: the single canonical "is this marker present
    AND parseable?" check. Used by ``parse_tester_output`` to flag a
    REQUIRED int field as missing-or-bogus so callers can fail closed.

    Contract:
        * marker absent           -> ``None``
        * marker present, value parses as ``int`` -> the int
        * marker present, value does NOT parse   -> ``None``

    Matches the same line shape ``_parse_structured_output`` accepts (the
    marker must start the stripped line, followed by a colon).
    """
    if not text:
        return None
    prefix = f"{marker}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped.split(":", 1)[1].strip()
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _has_marker_line(text: str, marker: str) -> bool:
    """True if ``text`` contains a line that begins with ``{marker}:``.

    Matches the same shape ``_parse_structured_output`` accepts (the marker
    must start the stripped line, followed by a colon). Retained for the
    benefit of other callers; the tester-contract path now uses
    ``_required_int`` directly so presence-AND-parseable is a single check.
    """
    if not text:
        return False
    prefix = f"{marker}:"
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return True
    return False


# Framework-emitted skip patterns (Task #2242 Phase B). We grep the raw
# tester stdout/result_text for these to cross-check the tester's reported
# TESTS_SKIPPED count. If the framework's own output disagrees with what
# the tester reported, the tester's numbers are suspect — fail closed.
#
# Task #2242 Phase D3: each pattern is ANCHORED to a framework-specific
# footer/header line. The previous unanchored patterns (e.g. "\b\d+\s+
# skipped\b") matched anywhere — including tester prose like
# "2 tests were skipped earlier in dev", which spuriously inflated the
# disagreement count and produced false-positive ``tests_inconclusive``
# routings. Anchoring requires the match to start at a line beginning that
# the test framework actually emits.
#
# Each pattern captures the integer skip count via group 1.
_FRAMEWORK_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # pytest summary footer: "==== 3 passed, 1 skipped in 0.12s ===="
    # Anchored on a line that begins with at least 3 '=' (the footer rule).
    re.compile(r"(?m)^={3,}.*?\b(\d+)\s+skipped\b", re.IGNORECASE),
    # vitest / jest "Tests:" summary header — line MUST start with Tests/Test
    #   vitest: " Tests  10 passed | 2 skipped (12)"
    #   jest:   "Tests:       1 skipped, 4 passed, 5 total"
    re.compile(
        r"(?m)^\s*Tests?:?\s+.*?\b(\d+)\s+skipped\b", re.IGNORECASE,
    ),
    # vitest / jest "N todo" on the same anchored Tests: header line.
    re.compile(
        r"(?m)^\s*Tests?:?\s+.*?\b(\d+)\s+todo\b", re.IGNORECASE,
    ),
    # playwright list/summary: "5 passed (1m), 2 skipped" — line begins with
    # an int + 'passed' which is the format playwright's reporter prints.
    re.compile(
        r"(?m)^\s*\d+\s+passed.*?\b(\d+)\s+skipped\b", re.IGNORECASE,
    ),
    # generic anchored "N skipped" line — line BEGINS with optional
    # whitespace, then the int, then 'skipped'. This catches the playwright
    # variant that prints "  4 skipped" on its own line AND a bare
    # "4 Skipped". Anchoring to line start prevents matching tester prose
    # such as "the 7 skipped runs were due to env vars" because that line
    # does not begin with the count token.
    re.compile(r"(?m)^\s*(\d+)\s+skipped\b", re.IGNORECASE),
    # go test "--- SKIP: TestFoo" is counted via _GO_SKIP_PATTERN below.
)

_GO_SKIP_PATTERN: re.Pattern[str] = re.compile(
    r"^---\s+SKIP:\s+\S+", re.MULTILINE,
)


def grep_framework_skip_counts(result_text: str) -> int:
    """Return the max skip count observed in framework-emitted output.

    Task #2242 Phase B: cross-check the tester's TESTS_SKIPPED claim against
    what the actual test framework printed. If the framework's footer says
    "5 skipped" but the tester reported TESTS_SKIPPED: 0 (or omitted the
    line), the tester is misreporting — route to ``tests_inconclusive``.

    Returns the maximum count seen across all known patterns. Returns 0
    when no framework skip signal is found (which is the correct default:
    "no evidence of disagreement").
    """
    if not result_text:
        return 0
    max_count = 0
    for pattern in _FRAMEWORK_SKIP_PATTERNS:
        for match in pattern.finditer(result_text):
            try:
                count = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if count > max_count:
                max_count = count
    # go test prints one "--- SKIP:" line per skipped test
    go_skips = len(_GO_SKIP_PATTERN.findall(result_text))
    if go_skips > max_count:
        max_count = go_skips
    return max_count


def parse_developer_output(result_text: str) -> list[str]:
    """Extract FILES_CHANGED list from developer output."""
    parsed = _parse_structured_output(result_text, _DEVELOPER_FILES_SCHEMA)
    return parsed.get("files_changed", [])


# --- Session Compaction ---


def verify_files_changed(claimed_files: list[str], project_dir: str) -> list[str]:
    """Cross-reference claimed FILES_CHANGED against actual git diff.

    Returns only files that actually changed according to git.
    Prevents agents from faking progress by claiming changes they didn't make.
    """
    import subprocess
    if not claimed_files or not project_dir:
        return claimed_files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return claimed_files  # Can't verify, trust the claim
        actual_files = set(result.stdout.strip().splitlines())
        # Also check staged files
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        if staged.returncode == 0:
            actual_files.update(staged.stdout.strip().splitlines())
        if not actual_files:
            return claimed_files  # No git changes detected, trust claim (might be committed already)
        # Return intersection — only files that were both claimed AND actually changed
        verified = [f for f in claimed_files if any(f.endswith(a) or a.endswith(f) for a in actual_files)]
        return verified if verified else claimed_files  # Fallback to claims if no match (path format differences)
    except Exception:
        return claimed_files  # On error, trust the claim


def build_compaction_summary(
    role: str, result: dict, cycle: int, task: dict
) -> str:
    """Compact agent output into a concise summary preserving actionable details.

    Uses compact_agent_output() to extract structured data (RESULT, FILES_CHANGED,
    BLOCKERS, SUMMARY) from raw output instead of passing raw tail content.
    Target: ~200 words max to prevent context rot across cycles.
    """
    text = result.get("result_text", "")
    # Compact to 200 words max, preserving file paths and error messages
    compacted = compact_agent_output(text, max_words=200)

    summary = (
        f"## Prior Work Summary (Cycle {cycle}, {role})\n"
        f"Task: #{task['id']} - {task['title']}\n"
        f"Turns used: {result.get('num_turns', '?')}\n"
        f"---\n"
        f"{compacted}\n"
    )
    return summary


def build_test_failure_context(test_results: dict, cycle: int) -> str:
    """Format Tester failures + recommendations for the Developer's next attempt.

    Returns a string to append to the Developer's system prompt.
    Caps output to prevent unbounded context growth: max 5 failure details,
    each truncated to 200 chars; max 3 recommendations.
    """
    lines = [
        f"## Test Failures from Cycle {cycle}",
        "",
        f"The Tester agent ran {test_results['tests_run']} tests "
        f"using {test_results['test_framework']}.",
        f"**{test_results['tests_failed']} tests failed.**",
        "",
    ]

    if test_results["failure_details"]:
        lines.append("### Failing Tests:")
        # Cap at 5 details, truncate each to 200 chars
        for detail in test_results["failure_details"][:5]:
            truncated = detail[:200] + "..." if len(detail) > 200 else detail
            lines.append(f"- {truncated}")
        remaining = len(test_results["failure_details"]) - 5
        if remaining > 0:
            lines.append(f"- ...and {remaining} more failure(s)")
        lines.append("")

    if test_results["recommendations"]:
        lines.append("### Tester Recommendations:")
        # Cap at 3 recommendations
        for rec in test_results["recommendations"][:3]:
            truncated = rec[:200] + "..." if len(rec) > 200 else rec
            lines.append(f"- {truncated}")
        lines.append("")

    lines.append("**Fix these test failures. Do NOT skip or delete failing tests.**")
    lines.append("")
    return "\n".join(lines)


def validate_output(result: dict) -> tuple[bool, str]:
    """Check if agent output contains the expected structured response.

    Returns (is_valid, reason) tuple.
    """
    if not result["success"]:
        return False, "Agent reported failure"

    text = result.get("result_text", "")
    if not text:
        return False, "No output from agent"

    # Check for the structured RESULT: marker
    if "RESULT:" in text:
        return True, "Structured output found"

    # Agent might have done useful work without the marker
    # (e.g., hit max turns). Check if there's substantial output.
    if len(text) > 100:
        return True, "Substantial output (no RESULT marker)"

    return False, "Output too short and missing RESULT marker"
