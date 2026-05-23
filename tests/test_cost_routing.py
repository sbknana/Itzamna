"""Tests for EQUIPA cost-based model routing (Task 1699).

Tests complexity scoring, tier selection, circuit breaker, uncertainty
escalation, and get_role_model integration with auto_model_routing flag.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from equipa.routing import (
    CB_RECOVERY_SECONDS,
    CB_STATE_CLOSED,
    CB_STATE_HALF_OPEN,
    CB_STATE_OPEN,
    THRESHOLD_HAIKU,
    THRESHOLD_SONNET,
    _circuit_breaker_state,
    _get_circuit_state,
    _uncertainty_level,
    auto_select_model,
    record_model_outcome,
    score_complexity,
    select_model_by_complexity,
)
from equipa.roles import get_role_model


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset circuit breaker state before each test."""
    _circuit_breaker_state.clear()
    yield
    _circuit_breaker_state.clear()


def test_score_complexity_returns_low_for_trivial_tasks():
    """Test: score_complexity returns <0.3 for 'fix typo in README'."""
    score = score_complexity("fix typo in README", "Fix typo")
    assert score < THRESHOLD_HAIKU, (
        f"Expected trivial task score < {THRESHOLD_HAIKU}, got {score}"
    )


def test_score_complexity_returns_high_for_complex_tasks():
    """Test: score_complexity returns >=0.6 for 'architect distributed authentication system'."""
    score = score_complexity(
        "Architect a distributed authentication system with multi-threaded concurrent "
        "request handling, encryption, scalability, infrastructure migration, security "
        "vulnerability scanning, and database migration across multiple files in the codebase",
        "Architect distributed authentication infrastructure",
    )
    assert score >= THRESHOLD_SONNET, (
        f"Expected complex task score >= {THRESHOLD_SONNET}, got {score}"
    )


def test_select_model_by_complexity_maps_tiers_correctly():
    """Test: select_model_by_complexity maps <0.3=haiku, 0.3-0.6=sonnet, >=0.6=opus."""
    # Low score -> haiku
    assert select_model_by_complexity(0.2, 0.0) == "haiku"

    # Mid score -> sonnet
    assert select_model_by_complexity(0.4, 0.0) == "sonnet"

    # High score -> opus
    assert select_model_by_complexity(0.8, 0.0) == "opus"

    # Edge cases
    assert select_model_by_complexity(0.29, 0.0) == "haiku"
    assert select_model_by_complexity(0.3, 0.0) == "sonnet"
    assert select_model_by_complexity(0.59, 0.0) == "sonnet"
    assert select_model_by_complexity(0.6, 0.0) == "opus"


def test_circuit_breaker_degrades_after_5_failures():
    """Test: circuit breaker opens after 5 consecutive failures."""
    model = "haiku"

    # Record 4 failures - circuit should stay closed
    for _ in range(4):
        record_model_outcome(model, success=False)
    assert _get_circuit_state(model) == CB_STATE_CLOSED

    # 5th failure - circuit should open
    record_model_outcome(model, success=False)
    assert _get_circuit_state(model) == CB_STATE_OPEN


def test_circuit_breaker_recovers_after_60s():
    """Test: circuit breaker transitions to half_open after 60s."""
    model = "sonnet"

    # Open the circuit
    for _ in range(5):
        record_model_outcome(model, success=False)
    assert _get_circuit_state(model) == CB_STATE_OPEN

    # Advance time by 60s (simulate recovery window)
    state = _circuit_breaker_state[model]
    state["last_failure_time"] = time.time() - (CB_RECOVERY_SECONDS + 1)

    # Circuit should now be half_open
    assert _get_circuit_state(model) == CB_STATE_HALF_OPEN


def test_circuit_breaker_resets_on_success():
    """Test: circuit breaker resets consecutive_failures counter on success."""
    model = "opus"

    # Record 4 failures
    for _ in range(4):
        record_model_outcome(model, success=False)
    state = _circuit_breaker_state[model]
    assert state["consecutive_failures"] == 4

    # Record success - should reset counter
    record_model_outcome(model, success=True)
    assert state["consecutive_failures"] == 0
    assert state["state"] == CB_STATE_CLOSED


def test_uncertainty_escalation_bumps_tier():
    """Test: uncertainty >0.15 auto-bumps complexity tier by 0.2."""
    # Task with truly trivial complexity but high uncertainty
    description = "fix typo"
    title = "Typo fix - unclear which file, unsure, investigate, diagnose, not sure"

    uncertainty = _uncertainty_level(f"{title} {description}")
    assert uncertainty > 0.15, f"Expected high uncertainty, got {uncertainty}"

    # Without escalation, should be haiku tier (very low complexity)
    base_score = score_complexity(description, title)
    no_escalation = select_model_by_complexity(base_score, 0.0)
    assert no_escalation == "haiku", f"Base score {base_score} should be haiku, got {no_escalation}"

    # With escalation, score + 0.2 should bump to sonnet
    with_escalation = select_model_by_complexity(base_score, uncertainty)
    assert with_escalation == "sonnet", (
        f"Expected uncertainty escalation to bump haiku->sonnet (base={base_score}, "
        f"uncertainty={uncertainty}), got {with_escalation}"
    )


def test_get_role_model_respects_all_5_overrides_when_auto_routing_on():
    """Test: get_role_model respects all 5 existing overrides when auto_model_routing is ON.

    Priority order (from roles.py docstring):
      1. model_epic (complexity-based)
      2. model_developer (role-based)
      3. CLI --model
      4. config global model
      5. DEFAULT_ROLE_MODELS
      6. auto-routing (only if no override matched)
    """
    task = {
        "id": 1,
        "description": "fix typo in README",
        "title": "Fix typo",
        "type": "trivial",
    }
    role = "developer"

    # Override 1: model_epic (complexity-based)
    config = {
        "features": {"auto_model_routing": True},
        "model_trivial": "haiku",  # explicit override
    }
    args = Mock(model="sonnet", dispatch_config=None)
    result = get_role_model(role, args, config=config, task=task)
    assert result == "haiku", "Expected complexity override to win over auto-routing"

    # Override 2: model_developer (role-based)
    config = {
        "features": {"auto_model_routing": True},
        "model_developer": "opus",  # role override
    }
    args = Mock(model="sonnet", dispatch_config=None)
    result = get_role_model(role, args, config=config, task=task)
    assert result == "opus", "Expected role override to win over auto-routing"

    # Override 3: CLI --model
    config = {"features": {"auto_model_routing": True}}
    args = Mock(model="opus", dispatch_config=None)  # CLI override
    result = get_role_model(role, args, config=config, task=task)
    assert result == "opus", "Expected CLI override to win over auto-routing"

    # Override 4: config global model
    config = {
        "features": {"auto_model_routing": True},
        "model": "opus",  # global override
    }
    args = Mock(model="sonnet", dispatch_config=None)  # CLI is sonnet (default)
    result = get_role_model(role, args, config=config, task=task)
    assert result == "opus", "Expected global config override to win over auto-routing"

    # Override 5: DEFAULT_ROLE_MODELS (via role lookup)
    # When no overrides match, falls back to DEFAULT_ROLE_MODELS
    # NOTE: DEFAULT_ROLE_MODELS includes "developer": "opus"
    # This test verifies DEFAULT_ROLE_MODELS is checked after CLI but BEFORE auto-routing
    # Use DEFAULT_MODEL (sonnet) for args.model to test DEFAULT_ROLE_MODELS activation
    config = {"features": {"auto_model_routing": True}}
    args = Mock(model="sonnet", dispatch_config=None)  # CLI sonnet = DEFAULT_MODEL (not override)
    # Developer has DEFAULT_ROLE_MODELS entry of "opus"
    # When CLI is default value, DEFAULT_ROLE_MODELS should activate
    result = get_role_model("developer", args, config=config, task=None)
    assert result == "opus", "Expected DEFAULT_ROLE_MODELS (developer=opus) to activate when CLI is default"


def test_get_role_model_uses_auto_routing_when_no_overrides():
    """Test: get_role_model uses auto-routing when no overrides match and flag ON."""
    # Trivial task: should route to haiku
    trivial_task = {
        "id": 1,
        "description": "fix typo in README",
        "title": "Fix typo",
    }
    # Complex task: should route to opus
    complex_task = {
        "id": 2,
        "description": "Architect distributed authentication system with multi-threaded "
                      "concurrent request handling, encryption, scalability, infrastructure "
                      "migration, security vulnerability scanning, database migration",
        "title": "Architect distributed authentication infrastructure",
    }

    config = {
        "features": {"auto_model_routing": True},
        # No overrides
    }
    args = Mock(model="sonnet", dispatch_config=None)  # CLI default
    role = "developer"

    # Trivial task -> auto-route to haiku
    result = get_role_model(role, args, config=config, task=trivial_task)
    assert result == "haiku", f"Expected auto-routing to select haiku for trivial task, got {result}"

    # Complex task -> auto-route to opus
    result = get_role_model(role, args, config=config, task=complex_task)
    assert result == "opus", f"Expected auto-routing to select opus for complex task, got {result}"


def test_get_role_model_ignores_auto_routing_when_flag_off():
    """Test: get_role_model ignores auto-routing when flag OFF."""
    task = {
        "id": 1,
        "description": "architect distributed authentication system",
        "title": "Architect auth",
    }
    config = {
        "features": {"auto_model_routing": False},  # FLAG OFF
        "model": "haiku",  # global config
    }
    args = Mock(model="sonnet", dispatch_config=None)  # CLI default
    role = "developer"

    # Should use global config "haiku", NOT auto-route to opus
    result = get_role_model(role, args, config=config, task=task)
    assert result == "haiku", (
        f"Expected auto-routing to be ignored (flag OFF), got {result}"
    )


def test_circuit_breaker_fallback_never_escalates_cost():
    """RT-02: auto_select_model MUST NOT escalate cost when circuit is open.

    Old behavior (haiku open -> sonnet) was a financial-DoS vector: an
    attacker who tripped Haiku's rate-limit-driven breaker forced every
    subsequent call onto a more expensive model. Fix: fallback walks DOWN
    the tier ladder; the cheapest tier fails closed (returns None).
    """
    # Open haiku circuit
    for _ in range(5):
        record_model_outcome("haiku", success=False)
    assert _get_circuit_state("haiku") == CB_STATE_OPEN

    # Trivial task that would normally select haiku
    task = {
        "description": "fix typo in README",
        "title": "Fix typo",
    }

    result = auto_select_model(task, config=None)
    assert result is None, (
        f"RT-02 violation: haiku breaker open must fail closed (None), "
        f"got {result!r}. Falling UP to a more expensive tier on failure "
        f"enables financial DoS via rate-limit-triggered escalation."
    )


def test_circuit_breaker_half_open_recovers_to_closed_on_success():
    """Test: circuit breaker transitions from half_open to closed on success."""
    model = "haiku"

    # Open circuit
    for _ in range(5):
        record_model_outcome(model, success=False)
    assert _get_circuit_state(model) == CB_STATE_OPEN

    # Advance time to trigger half_open
    state = _circuit_breaker_state[model]
    state["last_failure_time"] = time.time() - (CB_RECOVERY_SECONDS + 1)
    assert _get_circuit_state(model) == CB_STATE_HALF_OPEN

    # Record success - should transition to closed
    record_model_outcome(model, success=True)
    assert _get_circuit_state(model) == CB_STATE_CLOSED
