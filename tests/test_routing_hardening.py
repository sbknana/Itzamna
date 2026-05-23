"""Acceptance tests for SECURITY-REVIEW-1728 RT-01/RT-02/RT-03 hardening.

These tests are the explicit acceptance criteria from task 2453:

* RT-01 — Routing scoring is keyword-stuffing-resistant: a description with
  20x "simple trivial" still scores >= the priority-implied tier.
* RT-02 — Circuit-breaker fallback NEVER escalates cost: trip Haiku breaker,
  dispatch falls DOWN to fail-closed, NEVER to Sonnet/Opus.
* RT-03 — Concurrent dispatch fixture: 10 parallel calls do not corrupt
  circuit state.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import threading

import pytest

from equipa.routing import (
    CB_FAILURE_THRESHOLD,
    CB_STATE_CLOSED,
    CB_STATE_OPEN,
    THRESHOLD_HAIKU,
    TIER_ORDER,
    _circuit_breaker_state,
    _get_circuit_state,
    auto_select_model,
    record_model_outcome,
    score_complexity,
    select_model_by_complexity,
)


@pytest.fixture(autouse=True)
def _reset_circuit_state():
    _circuit_breaker_state.clear()
    yield
    _circuit_breaker_state.clear()


# ---------------------------------------------------------------------------
# RT-01: keyword-stuffing resistance + priority cross-validation
# ---------------------------------------------------------------------------


class TestRT01KeywordStuffing:
    """RT-01 HIGH: complexity scoring must resist keyword stuffing."""

    def test_low_keyword_spam_cannot_downgrade_high_priority_task(self):
        """20x 'simple trivial' must NOT downgrade a high-priority task.

        Acceptance criterion (verbatim from task 2453):
            "a description with 20x 'simple trivial' still scores >=
             the priority-implied tier."
        """
        stuffed_description = (
            "simple trivial easy quick simple trivial easy quick "
            "simple trivial easy quick simple trivial easy quick "
            "simple trivial easy quick simple trivial easy quick "
            "simple trivial easy quick simple trivial easy quick "
            "simple trivial easy quick simple trivial easy quick "
            # Real task buried in spam:
            "Implement input validation for the authentication endpoint."
        )
        task = {
            "description": stuffed_description,
            "title": "auth validation",
            "priority": "high",  # human-set priority: sonnet floor
        }

        model = auto_select_model(task)

        # Priority-implied floor for "high" is sonnet (TIER_ORDER index 1).
        priority_floor = TIER_ORDER.index("sonnet")
        chosen_index = TIER_ORDER.index(model) if model else -1
        assert chosen_index >= priority_floor, (
            f"RT-01: keyword stuffing dragged tier below priority floor. "
            f"Got {model!r}, expected >= sonnet."
        )

    def test_critical_priority_forces_opus_even_on_trivial_description(self):
        """RT-01(c): priority='critical' pins to opus regardless of score."""
        task = {
            "description": "typo typo typo typo typo typo typo typo",
            "title": "spelling",
            "priority": "critical",
        }
        model = auto_select_model(task)
        assert model == "opus", (
            f"RT-01(c): critical priority must pin to opus, got {model!r}"
        )

    def test_keyword_stuffing_via_high_keywords_capped(self):
        """RT-01(b): spamming HIGH keywords does not push score past 1.0."""
        spammed = " ".join(
            ["security architect refactor distributed encryption"] * 50
        )
        score = score_complexity(spammed, "")
        assert 0.0 <= score <= 1.0
        # And spamming HIGH keywords should not silently push the score
        # higher than a real complex task does.
        real_complex = score_complexity(
            "Architect distributed authentication system with encryption "
            "across multiple microservices and database migration",
            "Security architecture",
        )
        # Both produce comparable, capped scores.
        assert abs(score - real_complex) < 0.5, (
            f"RT-01: HIGH spamming amplified score abnormally "
            f"(spam={score}, real={real_complex})"
        )

    def test_semantic_score_uses_unique_presence_not_count(self):
        """Doubling/tripling LOW keyword counts must not change the score."""
        from equipa.routing import _semantic_depth

        one = _semantic_depth("typo")
        many = _semantic_depth(" ".join(["typo"] * 50))
        assert one == many, (
            f"RT-01: semantic depth changed with count ({one} vs {many})"
        )

    def test_priority_floor_overrides_haiku_score(self):
        """Direct unit test on select_model_by_complexity priority floor."""
        # Score that would normally pick haiku, but high-priority floor is sonnet.
        model = select_model_by_complexity(
            score=0.1, uncertainty=0.0, priority="high"
        )
        assert model == "sonnet"

    def test_priority_floor_does_not_downgrade(self):
        """If scored tier is HIGHER than priority floor, scored tier wins."""
        # Opus by score; priority "low" must NOT downgrade.
        model = select_model_by_complexity(
            score=0.9, uncertainty=0.0, priority="low"
        )
        assert model == "opus"


# ---------------------------------------------------------------------------
# RT-02: circuit-breaker fallback NEVER escalates cost
# ---------------------------------------------------------------------------


class TestRT02FallbackNeverEscalates:
    """RT-02 HIGH: open-circuit fallback must fall DOWN, never UP."""

    def test_haiku_open_fails_closed(self):
        """Trip Haiku breaker -> dispatch returns None (fail-closed).

        Acceptance criterion (verbatim from task 2453):
            "Circuit-breaker fallback NEVER escalates cost (new test: trip
             Haiku breaker, dispatch -> falls down to fail-closed, NEVER
             to Sonnet/Opus)."
        """
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("haiku", success=False)
        assert _get_circuit_state("haiku") == CB_STATE_OPEN

        task = {"description": "fix typo", "title": "typo"}
        model = auto_select_model(task)

        assert model is None, (
            f"RT-02 violation: haiku open must fail closed, got {model!r}. "
            f"Falling UP to sonnet/opus enables financial DoS."
        )
        # Explicitly assert the forbidden outcomes.
        assert model not in ("sonnet", "opus"), (
            f"RT-02: forbidden escalation to {model!r} on haiku breaker open"
        )

    def test_sonnet_open_falls_to_haiku(self):
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("sonnet", success=False)
        task = {
            "description": "Implement validation endpoint with error handling",
            "title": "Add validation",
        }
        model = auto_select_model(task)
        assert model == "haiku", f"RT-02: sonnet->haiku expected, got {model!r}"

    def test_opus_open_falls_to_sonnet(self):
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("opus", success=False)
        task = {
            "description": (
                "Architect distributed authentication infrastructure with "
                "encryption, authorization, and database migration across "
                "multiple microservices"
            ),
            "title": "Security architecture",
        }
        model = auto_select_model(task)
        assert model == "sonnet", f"RT-02: opus->sonnet expected, got {model!r}"

    def test_opus_and_sonnet_open_falls_to_haiku(self):
        """Two open circuits at the top -> still walks down, never up."""
        for tier in ("opus", "sonnet"):
            for _ in range(CB_FAILURE_THRESHOLD):
                record_model_outcome(tier, success=False)
        task = {
            "description": (
                "Architect distributed authentication infrastructure with "
                "encryption and database migration across microservices"
            ),
            "title": "Security architecture",
        }
        model = auto_select_model(task)
        assert model == "haiku", (
            f"RT-02: opus+sonnet open must fall all the way down to haiku, "
            f"got {model!r}"
        )

    def test_all_circuits_open_fails_closed(self):
        for tier in TIER_ORDER:
            for _ in range(CB_FAILURE_THRESHOLD):
                record_model_outcome(tier, success=False)
        task = {"description": "fix typo", "title": "typo"}
        model = auto_select_model(task)
        assert model is None, (
            f"RT-02: every circuit open must fail closed, got {model!r}"
        )

    def test_fallback_map_has_no_upward_arrows(self):
        """Structural invariant: no entry in the fallback map points up.

        This is the policy-level guarantee — even if a future edit changes
        the ladder, this assertion catches any value that points to a
        more-expensive tier.
        """
        from equipa.routing import _FALLBACK_DOWN

        for src, dst in _FALLBACK_DOWN.items():
            if dst is None:
                continue
            src_idx = TIER_ORDER.index(src)
            dst_idx = TIER_ORDER.index(dst)
            assert dst_idx < src_idx, (
                f"RT-02 invariant violated: fallback {src}->{dst} escalates "
                f"(src_idx={src_idx} dst_idx={dst_idx})"
            )


# ---------------------------------------------------------------------------
# RT-03: concurrent dispatch must not corrupt circuit state
# ---------------------------------------------------------------------------


class TestRT03Concurrency:
    """RT-03 MED: circuit-breaker state is shared and must be lock-guarded."""

    def test_concurrent_failures_open_circuit_exactly_once(self):
        """10 parallel record_model_outcome(False) calls must produce a
        consistent post-state: state == OPEN and consecutive_failures == 10.

        Without the lock, parallel `state["consecutive_failures"] += 1`
        operations lose updates and the breaker may not trip.
        """
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            record_model_outcome("haiku", success=False)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = _circuit_breaker_state["haiku"]
        assert state["consecutive_failures"] == 10, (
            f"RT-03: lost updates — expected 10, got "
            f"{state['consecutive_failures']}"
        )
        assert state["state"] == CB_STATE_OPEN
        assert _get_circuit_state("haiku") == CB_STATE_OPEN

    def test_concurrent_mixed_outcomes_are_consistent(self):
        """Interleaving successes and failures across threads must leave
        the breaker in a coherent state — no torn dicts, no KeyErrors."""
        barrier = threading.Barrier(20)

        def worker(success: bool):
            barrier.wait()
            record_model_outcome("sonnet", success=success)

        threads = []
        for i in range(20):
            threads.append(threading.Thread(target=worker, args=(i % 2 == 0,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Final state must be one of the three known states.
        assert _get_circuit_state("sonnet") in (
            CB_STATE_CLOSED, CB_STATE_OPEN, "half_open",
        )
        # And consecutive_failures must never be negative.
        assert _circuit_breaker_state["sonnet"]["consecutive_failures"] >= 0

    def test_concurrent_auto_select_does_not_crash(self):
        """10 parallel auto_select_model calls must all return either a
        valid tier name or None — no exceptions, no torn state."""
        results: list[object] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)
        task = {"description": "Add validation", "title": "validation"}

        def worker():
            barrier.wait()
            r = auto_select_model(task)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        for r in results:
            assert r is None or r in TIER_ORDER, (
                f"RT-03: concurrent auto_select_model returned {r!r}"
            )

    def test_lock_object_exists_and_is_a_lock(self):
        """Documentation invariant: _circuit_breaker_lock must be a Lock
        (or RLock) so any new helper that needs the breaker state can grab
        it the same way."""
        from equipa.routing import _circuit_breaker_lock

        assert hasattr(_circuit_breaker_lock, "acquire")
        assert hasattr(_circuit_breaker_lock, "release")
        # threading.Lock and threading.RLock both expose these.
