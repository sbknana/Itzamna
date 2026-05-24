"""Intelligent task routing — complexity scoring + model selection + circuit breaker.

Layer 2 module: imports from equipa.constants only.

Complexity scoring uses 4 weighted features to classify tasks as haiku/sonnet/opus
tier. Scoring is keyword-stuffing-resistant (RT-01): unique keyword presence is
used in place of raw count, low-tier keywords are capped so they cannot drag a
genuinely complex task into the cheap tier, and task.priority is cross-validated
against the scored tier so a "critical"/"high" priority cannot be downgraded.

Circuit breaker tracks consecutive failures per model with a 60s recovery
window. Fallback direction is DOWN to a cheaper tier, never up (RT-02): an
attacker who trips the cheap tier's breaker can never coerce dispatch onto the
expensive tier. If the cheapest tier is itself unavailable we fail closed
(``auto_select_model`` returns ``None``) rather than escalating cost.

The fail-closed return value of ``auto_select_model`` is propagated upward
through ``equipa.roles.get_role_model`` as ``CircuitOpenError`` (2453-S1).
This enforces the RT-02 invariant end-to-end: a tripped haiku circuit can
NEVER coerce dispatch onto ``DEFAULT_ROLE_MODELS[role]`` (which maps most
roles to opus). Dispatch entry points
(``equipa.dispatch.run_dev_test_loop_with_autoresearch`` and
``equipa.cli.run_mode_task``) catch ``CircuitOpenError`` and demote the
task outcome to ``circuit_breaker_blocked``.

Circuit-breaker state is shared across dispatcher threads/coroutines and is
guarded by a ``threading.Lock`` (RT-03) so concurrent record/read calls cannot
observe a torn or stale state. Every public function that touches
``_circuit_breaker_state`` acquires ``_circuit_breaker_lock`` for the full
read-modify-write window.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from equipa.constants import DEFAULT_MODEL

# Reuse the dedicated monotonic clock module so tests can monkeypatch it.
import time as _time

logger = logging.getLogger(__name__)


# --- Exceptions ---


class CircuitOpenError(RuntimeError):
    """Signal that auto-routing failed closed because every suitable tier is open.

    Raised upward from ``equipa.roles.get_role_model`` when auto-routing is
    enabled, ``auto_select_model`` returned ``None`` (fail-closed per RT-02),
    and the caller MUST NOT silently fall through to a default model. The
    dispatch wrapper catches this and demotes the task outcome to
    ``circuit_breaker_blocked`` so the breaker can recover before the task
    is retried.

    Attributes:
        role: The role name whose model resolution failed.
        tier_attempted: The cheapest tier the router tried before failing
            closed (informational only; may be ``None`` if unknown).
    """

    def __init__(self, role: str, tier_attempted: str | None = None) -> None:
        self.role = role
        self.tier_attempted = tier_attempted
        suffix = f" (cheapest tier attempted: {tier_attempted})" if tier_attempted else ""
        super().__init__(
            f"auto-routing fail-closed for role={role}: every suitable circuit is OPEN"
            f"{suffix}"
        )


# --- Complexity Scoring Keywords ---

# HIGH complexity: architectural, security, distributed system work
HIGH_KEYWORDS = frozenset({
    "architect", "security", "refactor", "distributed", "migrate",
    "optimize", "performance", "scalability", "infrastructure",
    "authentication", "authorization", "encryption", "vulnerability",
    "concurrent", "parallel", "multi-threaded", "race condition",
    "database migration", "schema design", "api design",
})

# MEDIUM complexity: standard feature/fix work
MEDIUM_KEYWORDS = frozenset({
    "implement", "feature", "fix", "test", "endpoint", "integration",
    "component", "validation", "error handling", "logging",
    "configuration", "deployment", "monitoring", "caching",
    "query", "model", "controller", "service", "middleware",
})

# LOW complexity: trivial edits
LOW_KEYWORDS = frozenset({
    "typo", "comment", "format", "rename", "style", "whitespace",
    "import", "dependency", "version", "update package", "bump",
    "documentation", "readme", "spelling", "punctuation",
})

# --- Feature Weights ---

WEIGHT_LEXICAL = 0.2
WEIGHT_SEMANTIC = 0.35
WEIGHT_SCOPE = 0.25
WEIGHT_UNCERTAINTY = 0.2

# --- Model Selection Thresholds ---

THRESHOLD_HAIKU = 0.3   # < 0.3 = haiku
THRESHOLD_SONNET = 0.6  # 0.3-0.6 = sonnet, >= 0.6 = opus

# Tier ordering — index 0 is cheapest, index -1 is most expensive.
TIER_ORDER: tuple[str, ...] = ("haiku", "sonnet", "opus")

# Priority -> minimum tier index in TIER_ORDER.
# RT-01(c): the scored tier may never be cheaper than what the human-set
# task priority demands. "critical" pins to opus, "high" pins to sonnet+.
PRIORITY_MIN_TIER: dict[str, int] = {
    "critical": 2,  # opus
    "high": 1,      # sonnet+
    "medium": 0,    # haiku+ (no constraint)
    "low": 0,
}

# RT-01(b): per-bucket caps on the LOW and MEDIUM buckets only. Without
# these caps an attacker can stuff a description with every distinct word
# in LOW_KEYWORDS (15 hits) and drag a genuinely complex task's semantic
# score toward 0. We count DISTINCT keyword hits ("typo typo typo" -> 1)
# and cap the cheap buckets so they cannot outvote the HIGH bucket by
# sheer numbers. The HIGH bucket is INTENTIONALLY uncapped — escalating
# upward on additional HIGH evidence is the safe direction.
LOW_BUCKET_CAP = 2
MED_BUCKET_CAP = 3

# Per-bucket weights used in the weighted-mean numerator.
HIGH_BUCKET_WEIGHT = 1.0
MED_BUCKET_WEIGHT = 0.5
LOW_BUCKET_WEIGHT = 0.1

# --- Circuit Breaker Settings ---

CB_FAILURE_THRESHOLD = 5  # consecutive failures before circuit opens
CB_RECOVERY_SECONDS = 60  # time before attempting recovery
CB_STATE_CLOSED = "closed"
CB_STATE_OPEN = "open"
CB_STATE_HALF_OPEN = "half_open"

# In-memory circuit breaker state per model — guarded by _circuit_breaker_lock.
# RT-03: all reads/writes must hold the lock for the full read-modify-write
# window. Holders are short (dict ops, time.time()), never call back into
# user code, and never block on I/O, so contention is negligible.
_circuit_breaker_state: dict[str, dict[str, Any]] = {}
_circuit_breaker_lock = threading.Lock()


def _lexical_complexity(text: str) -> float:
    """Compute lexical complexity: avg word length + avg sentence length.

    Returns normalized score 0.0-1.0 (higher = more complex).
    """
    if not text.strip():
        return 0.0

    words = text.split()
    if not words:
        return 0.0

    # Average word length (normalize: 4 chars = 0.4, 10+ chars = 1.0)
    avg_word_len = sum(len(w) for w in words) / len(words)
    word_score = min(avg_word_len / 10.0, 1.0)

    # Average sentence length (normalize: 10 words = 0.5, 30+ words = 1.0)
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    if sentences:
        avg_sent_len = len(words) / len(sentences)
        sent_score = min(avg_sent_len / 30.0, 1.0)
    else:
        sent_score = 0.5

    return (word_score + sent_score) / 2.0


def _semantic_depth(text: str) -> float:
    """Compute semantic depth via DISTINCT keyword counts (RT-01 hardened).

    Each bucket contributes ``min(distinct_count, BUCKET_CAP)``. Counting
    distinct hits (not raw occurrences) means "typo typo typo" stuffing is
    a no-op. Capping the distinct count per bucket means even an attacker
    that spams every keyword in LOW_KEYWORDS (15 distinct hits) can only
    contribute LOW_BUCKET_CAP of weight — so a single HIGH keyword can
    never be drowned out.

    Returns a weighted mean in 0.0-1.0; 0.5 when no bucket fires.
    """
    text_lower = text.lower()

    # HIGH bucket is uncapped: more HIGH evidence -> higher score is safe.
    high = sum(1 for kw in HIGH_KEYWORDS if kw in text_lower)
    med = min(sum(1 for kw in MEDIUM_KEYWORDS if kw in text_lower), MED_BUCKET_CAP)
    low = min(sum(1 for kw in LOW_KEYWORDS if kw in text_lower), LOW_BUCKET_CAP)

    total = high + med + low
    if total == 0:
        return 0.5  # neutral default

    weighted = (
        high * HIGH_BUCKET_WEIGHT
        + med * MED_BUCKET_WEIGHT
        + low * LOW_BUCKET_WEIGHT
    )
    return min(weighted / total, 1.0)


def _task_scope(text: str) -> float:
    """Compute task scope via regex patterns for multi-file/system-wide work.

    Returns score 0.0-1.0 (higher = broader scope).
    """
    text_lower = text.lower()
    score = 0.0

    # Multi-file indicators
    multi_file_patterns = [
        r"\bmultiple\s+files\b",
        r"\ball\s+files\b",
        r"\bproject-wide\b",
        r"\bcodebase\b",
        r"\bentire\s+system\b",
        r"\bacross\s+\d+\s+files\b",
    ]
    if any(re.search(p, text_lower) for p in multi_file_patterns):
        score += 0.5

    # System-wide indicators
    system_patterns = [
        r"\barchitecture\b",
        r"\binfrastructure\b",
        r"\bmigration\b",
        r"\bdeployment\b",
        r"\bCI/CD\b",
        r"\bpipeline\b",
    ]
    if any(re.search(p, text_lower) for p in system_patterns):
        score += 0.5

    return min(score, 1.0)


def _uncertainty_level(text: str) -> float:
    """Compute uncertainty level via debug/investigate/unclear patterns.

    Returns score 0.0-1.0 (higher = more uncertain).
    """
    text_lower = text.lower()

    uncertainty_patterns = [
        r"\bdebug\b",
        r"\binvestigate\b",
        r"\bdiagnose\b",
        r"\bnot sure\b",
        r"\bunclear\b",
        r"\bfind out\b",
        r"\broot cause\b",
        r"\bwhy\b.*\bfailing\b",
        r"\bintermittent\b",
    ]

    match_count = sum(1 for p in uncertainty_patterns if re.search(p, text_lower))
    return min(match_count / len(uncertainty_patterns), 1.0)


def _structural_complexity_bonus(description: str) -> float:
    """Semantic-features bonus that is immune to keyword stuffing (RT-01a).

    Uses purely structural signals — total length, line count, distinct
    code/path tokens — which an attacker cannot fake by repeating cheap
    words. Returns 0.0-1.0.
    """
    if not description:
        return 0.0

    # Length-based: 200 chars = small bump, 2000+ chars = max.
    length_score = min(len(description) / 2000.0, 1.0)

    # Line-based: tasks that span many lines typically span many files/steps.
    line_count = description.count("\n") + 1
    line_score = min(line_count / 40.0, 1.0)

    # Path/code token count — "file.py", "src/foo/bar.ts", "module.Class"
    # are all signals of multi-component work. Counts distinct tokens so
    # repetition does not inflate the bonus.
    path_tokens = set(re.findall(
        r"\b[\w-]+(?:/[\w.-]+)+|\b[\w-]+\.[a-zA-Z]{1,5}\b", description
    ))
    path_score = min(len(path_tokens) / 8.0, 1.0)

    return (length_score + line_score + path_score) / 3.0


def score_complexity(description: str, title: str = "") -> float:
    """Score task complexity using 4 weighted features + structural bonus.

    Args:
        description: Task description text
        title: Optional task title

    Returns:
        Complexity score 0.0-1.0 (0=trivial, 1=highly complex)
    """
    combined = f"{title} {description}".strip()
    if not combined:
        return 0.5  # neutral default

    lexical = _lexical_complexity(combined)
    semantic = _semantic_depth(combined)
    scope = _task_scope(combined)
    uncertainty = _uncertainty_level(combined)

    score = (
        WEIGHT_LEXICAL * lexical
        + WEIGHT_SEMANTIC * semantic
        + WEIGHT_SCOPE * scope
        + WEIGHT_UNCERTAINTY * uncertainty
    )

    # Structural bonus (RT-01a) — semantic features that resist keyword
    # stuffing. Worth up to 0.15 of the final score.
    structural = _structural_complexity_bonus(description)
    score = score + 0.15 * structural

    return round(min(score, 1.0), 3)


def _priority_minimum_tier_index(priority: Any) -> int:
    """Return the minimum TIER_ORDER index implied by task.priority.

    Unknown/None priorities yield 0 (no constraint). Case-insensitive.
    """
    if not priority:
        return 0
    key = str(priority).strip().lower()
    return PRIORITY_MIN_TIER.get(key, 0)


def select_model_by_complexity(
    score: float,
    uncertainty: float,
    config: dict[str, Any] | None = None,
    priority: Any = None,
) -> str:
    """Select model tier based on complexity, uncertainty, and priority.

    Args:
        score: Complexity score from score_complexity()
        uncertainty: Uncertainty level from _uncertainty_level()
        config: Optional dispatch config with model overrides
        priority: Optional task priority ("critical"/"high"/"medium"/"low")
            used for RT-01(c) cross-validation. If the scored tier is
            cheaper than what priority demands, the priority floor wins
            and a warning is logged.

    Returns:
        Model name: "haiku", "sonnet", or "opus"
    """
    # Uncertainty escalation: >0.15 auto-bumps tier
    if uncertainty > 0.15:
        score = min(score + 0.2, 1.0)

    # Three-tier thresholds
    if score < THRESHOLD_HAIKU:
        tier_index = 0  # haiku
    elif score < THRESHOLD_SONNET:
        tier_index = 1  # sonnet
    else:
        tier_index = 2  # opus

    # RT-01(c): cross-validate against task.priority. Take the HIGHER of
    # the scored tier and the priority-implied tier; never the cheaper.
    priority_floor = _priority_minimum_tier_index(priority)
    if priority_floor > tier_index:
        logger.warning(
            "routing: scored tier %s overridden by priority floor %s "
            "(score=%s, priority=%s)",
            TIER_ORDER[tier_index], TIER_ORDER[priority_floor], score, priority,
        )
        tier_index = priority_floor

    model = TIER_ORDER[tier_index]

    # Check for config overrides
    if config and "model_overrides" in config:
        overrides = config["model_overrides"]
        if model in overrides:
            model = overrides[model]

    return model


def record_model_outcome(model: str, success: bool) -> None:
    """Record success/failure outcome for circuit breaker tracking.

    Args:
        model: Model name
        success: True if task succeeded, False if failed
    """
    with _circuit_breaker_lock:
        if model not in _circuit_breaker_state:
            _circuit_breaker_state[model] = {
                "state": CB_STATE_CLOSED,
                "consecutive_failures": 0,
                "last_failure_time": 0.0,
            }

        state = _circuit_breaker_state[model]

        if success:
            # Reset on success
            state["consecutive_failures"] = 0
            if state["state"] == CB_STATE_HALF_OPEN:
                state["state"] = CB_STATE_CLOSED
        else:
            # Increment failure count
            state["consecutive_failures"] += 1
            state["last_failure_time"] = _time.time()

            # Open circuit if threshold exceeded
            if state["consecutive_failures"] >= CB_FAILURE_THRESHOLD:
                state["state"] = CB_STATE_OPEN


def _get_circuit_state(model: str) -> str:
    """Get current circuit breaker state for model.

    Handles recovery window logic: OPEN -> HALF_OPEN after recovery time.
    Holds _circuit_breaker_lock for the full check-and-transition window
    so concurrent callers cannot race and observe inconsistent states.

    Args:
        model: Model name

    Returns:
        Circuit state: "closed", "open", or "half_open"
    """
    with _circuit_breaker_lock:
        if model not in _circuit_breaker_state:
            return CB_STATE_CLOSED

        state = _circuit_breaker_state[model]
        current_time = _time.time()

        # Check recovery window
        if state["state"] == CB_STATE_OPEN:
            elapsed = current_time - state["last_failure_time"]
            if elapsed >= CB_RECOVERY_SECONDS:
                state["state"] = CB_STATE_HALF_OPEN
                state["consecutive_failures"] = 0

        return state["state"]


# RT-02: Fallback map ALWAYS goes DOWN to a cheaper tier. The cheapest tier
# falls to None (fail closed). An attacker who trips a cheap tier's breaker
# can NEVER coerce dispatch onto a more expensive tier — that is the policy
# invariant this map encodes. Do not change a value to a more-expensive tier.
_FALLBACK_DOWN: dict[str, str | None] = {
    "opus": "sonnet",
    "sonnet": "haiku",
    "haiku": None,  # fail closed — never escalate
}


def _fallback_when_open(model: str) -> str | None:
    """Return the cheaper-tier fallback for ``model`` (RT-02).

    Returns None for the cheapest tier — caller MUST treat this as
    "fail closed", not "use default". Callers that need a non-None
    fallback should retry later, not coerce to a different model.
    """
    return _FALLBACK_DOWN.get(model, None)


def auto_select_model(
    task: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str | None:
    """Auto-select model for task using complexity scoring + circuit breaker.

    Args:
        task: Task dict with "description", optional "title", optional
            "priority" keys.
        config: Optional dispatch config

    Returns:
        Selected model name, or ``None`` if all suitable circuits are
        open and no cheaper fallback is available (fail-closed). Callers
        must handle ``None`` — typically by deferring the dispatch.
    """
    description = task.get("description", "")
    title = task.get("title", "")
    priority = task.get("priority")

    # Score complexity
    complexity = score_complexity(description, title)
    uncertainty = _uncertainty_level(f"{title} {description}")

    # Select model tier (with priority cross-validation)
    model = select_model_by_complexity(complexity, uncertainty, config, priority)

    # RT-02: walk DOWN the tier ladder while the chosen circuit is open.
    # Bounded by len(TIER_ORDER) to prevent any loop pathology.
    for _ in range(len(TIER_ORDER) + 1):
        if model is None:
            return None
        if _get_circuit_state(model) != CB_STATE_OPEN:
            return model
        logger.warning(
            "routing: circuit OPEN for %s — falling DOWN to cheaper tier",
            model,
        )
        model = _fallback_when_open(model)

    # If we walked the entire ladder and every tier is open, fail closed.
    logger.error(
        "routing: every circuit is OPEN — failing closed, deferring dispatch"
    )
    return None


# DEFAULT_MODEL is imported but only referenced here for compatibility with
# downstream call sites that may want to know the configured default. We do
# NOT use it as a fallback when all circuits are open — that would defeat the
# RT-02 fail-closed guarantee.
_ = DEFAULT_MODEL
