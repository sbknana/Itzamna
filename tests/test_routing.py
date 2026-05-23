"""Tests for equipa.routing complexity scoring and circuit breaker.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import time

import pytest

from equipa.routing import (
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_SECONDS,
    CB_STATE_CLOSED,
    CB_STATE_HALF_OPEN,
    CB_STATE_OPEN,
    THRESHOLD_HAIKU,
    THRESHOLD_SONNET,
    _circuit_breaker_state,
    _get_circuit_state,
    _lexical_complexity,
    _semantic_depth,
    _task_scope,
    _uncertainty_level,
    auto_select_model,
    record_model_outcome,
    score_complexity,
    select_model_by_complexity,
)


class TestLexicalComplexity:
    """Test lexical complexity scoring."""

    def test_empty_string(self):
        assert _lexical_complexity("") == 0.0

    def test_simple_sentence(self):
        score = _lexical_complexity("fix typo in readme")
        assert 0.0 < score < 0.5

    def test_complex_sentence(self):
        score = _lexical_complexity(
            "Architect a distributed authentication system with "
            "multi-factor verification and encryption"
        )
        assert score > 0.5


class TestSemanticDepth:
    """Test semantic depth keyword matching."""

    def test_high_keywords(self):
        score = _semantic_depth("Refactor security and authentication")
        assert score > 0.7

    def test_medium_keywords(self):
        score = _semantic_depth("Implement feature with validation")
        assert 0.4 < score < 0.7

    def test_low_keywords(self):
        score = _semantic_depth("Fix typo in comment")
        assert score < 0.3

    def test_no_keywords(self):
        score = _semantic_depth("Do something unclear")
        assert score == 0.5

    def test_keyword_stuffing_does_not_drag_score_down(self):
        # RT-01: repeated LOW keywords MUST NOT drag a HIGH-keyword score
        # below the HIGH bucket's expected contribution. Old code averaged
        # raw counts so 20x "simple/trivial/typo" + 1x "security" produced
        # ~0.05 (haiku tier); new code uses set presence so the result
        # stays above the haiku threshold.
        stuffed = ("simple trivial typo simple trivial typo " * 20) + " security"
        score = _semantic_depth(stuffed)
        assert score >= THRESHOLD_HAIKU, (
            f"Keyword stuffing dragged semantic_depth to {score}"
        )


class TestTaskScope:
    """Test task scope detection."""

    def test_single_file(self):
        score = _task_scope("Update one function")
        assert score < 0.3

    def test_multi_file(self):
        score = _task_scope("Refactor across multiple files")
        assert score >= 0.5

    def test_system_wide(self):
        score = _task_scope("Migration and infrastructure changes")
        assert score >= 0.5


class TestUncertaintyLevel:
    """Test uncertainty detection."""

    def test_certain_task(self):
        score = _uncertainty_level("Add validation to endpoint")
        assert score < 0.2

    def test_uncertain_task(self):
        score = _uncertainty_level("Debug intermittent failure, not sure why")
        assert score > 0.2

    def test_investigation_task(self):
        score = _uncertainty_level("Investigate root cause of failing tests")
        assert score > 0.2


class TestScoreComplexity:
    """Test overall complexity scoring."""

    def test_trivial_task(self):
        score = score_complexity("Fix typo in README", "typo fix")
        assert score < THRESHOLD_HAIKU

    def test_medium_task(self):
        score = score_complexity(
            "Implement validation endpoint with error handling",
            "Add validation",
        )
        assert 0.2 <= score < THRESHOLD_SONNET

    def test_complex_task(self):
        score = score_complexity(
            "Architect distributed authentication system with encryption "
            "across multiple microservices and database migration",
            "Security architecture",
        )
        assert score >= 0.55

    def test_empty_description(self):
        score = score_complexity("", "")
        assert score == 0.5


class TestSelectModelByComplexity:
    """Test model selection based on complexity."""

    def test_haiku_selection(self):
        model = select_model_by_complexity(0.2, 0.05)
        assert model == "haiku"

    def test_sonnet_selection(self):
        model = select_model_by_complexity(0.45, 0.1)
        assert model == "sonnet"

    def test_opus_selection(self):
        model = select_model_by_complexity(0.75, 0.1)
        assert model == "opus"

    def test_uncertainty_escalation(self):
        # Score 0.25 (haiku) + uncertainty 0.2 -> escalates to sonnet
        model = select_model_by_complexity(0.25, 0.2)
        assert model == "sonnet"

    def test_config_overrides(self):
        config = {"model_overrides": {"haiku": "sonnet"}}
        model = select_model_by_complexity(0.2, 0.05, config)
        assert model == "sonnet"


class TestCircuitBreaker:
    """Test circuit breaker functionality."""

    def setup_method(self):
        # Clear circuit breaker state before each test
        _circuit_breaker_state.clear()

    def test_initial_state_closed(self):
        assert _get_circuit_state("test-model") == CB_STATE_CLOSED

    def test_success_keeps_closed(self):
        record_model_outcome("test-model", success=True)
        assert _get_circuit_state("test-model") == CB_STATE_CLOSED

    def test_failures_open_circuit(self):
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("test-model", success=False)
        assert _get_circuit_state("test-model") == CB_STATE_OPEN

    def test_recovery_window(self):
        # Open circuit
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("test-model", success=False)
        assert _get_circuit_state("test-model") == CB_STATE_OPEN

        # Wait for recovery
        _circuit_breaker_state["test-model"]["last_failure_time"] = (
            time.time() - CB_RECOVERY_SECONDS - 1
        )
        assert _get_circuit_state("test-model") == CB_STATE_HALF_OPEN

    def test_half_open_to_closed(self):
        _circuit_breaker_state["test-model"] = {
            "state": CB_STATE_HALF_OPEN,
            "consecutive_failures": 0,
            "last_failure_time": 0.0,
        }
        record_model_outcome("test-model", success=True)
        assert _get_circuit_state("test-model") == CB_STATE_CLOSED

    def test_success_resets_failures(self):
        record_model_outcome("test-model", success=False)
        record_model_outcome("test-model", success=False)
        record_model_outcome("test-model", success=True)
        state = _circuit_breaker_state["test-model"]
        assert state["consecutive_failures"] == 0


class TestAutoSelectModel:
    """Test auto-select model integration."""

    def setup_method(self):
        _circuit_breaker_state.clear()

    def test_trivial_task_selects_haiku(self):
        task = {"description": "Fix typo", "title": "Typo fix"}
        model = auto_select_model(task)
        assert model == "haiku"

    def test_complex_task_selects_opus(self):
        task = {
            "description": "Architect distributed authentication infrastructure with encryption, "
            "authorization, and database migration across multiple microservices",
            "title": "Security architecture",
        }
        model = auto_select_model(task)
        assert model == "opus"

    def test_circuit_open_haiku_fails_closed(self):
        # RT-02: trip Haiku breaker, dispatch MUST fall to None (fail-closed),
        # NEVER to Sonnet/Opus. Escalating up on failure is a financial-DoS
        # vector — an attacker who triggers Haiku rate-limits would otherwise
        # force every subsequent call onto Opus.
        task = {"description": "Fix typo", "title": "Typo fix"}

        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("haiku", success=False)

        model = auto_select_model(task)
        assert model is None, (
            f"RT-02 violation: haiku breaker open must fail closed, got {model}"
        )

    def test_opus_circuit_falls_down_to_sonnet(self):
        # RT-02: when Opus is open, fall DOWN to sonnet (cheaper), never
        # stay on Opus and never escalate.
        task = {
            "description": "Architect distributed authentication infrastructure with encryption, "
            "authorization, and database migration across multiple microservices",
            "title": "Security architecture",
        }

        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("opus", success=False)

        model = auto_select_model(task)
        assert model == "sonnet", (
            f"RT-02: opus open must fall DOWN to sonnet, got {model}"
        )

    def test_sonnet_circuit_falls_down_to_haiku(self):
        # RT-02: medium-tier task with Sonnet open must fall DOWN to Haiku.
        task = {
            "description": "Implement validation endpoint with error handling",
            "title": "Add validation",
        }
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome("sonnet", success=False)

        model = auto_select_model(task)
        assert model == "haiku", (
            f"RT-02: sonnet open must fall DOWN to haiku, got {model}"
        )

    def test_all_circuits_open_fails_closed(self):
        # RT-02: if every tier is open, return None (fail closed) rather
        # than coerce dispatch onto any model.
        task = {"description": "Fix typo", "title": "Typo fix"}
        for tier in ("haiku", "sonnet", "opus"):
            for _ in range(CB_FAILURE_THRESHOLD):
                record_model_outcome(tier, success=False)

        model = auto_select_model(task)
        assert model is None

    def test_empty_task(self):
        task = {}
        model = auto_select_model(task)
        # Empty task returns a model OR None — both are acceptable as long
        # as the result is not coerced to a more-expensive tier.
        assert model is None or model in ["haiku", "sonnet", "opus"]
