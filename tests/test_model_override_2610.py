"""
Tests for task-2610: honor explicit --model even when it equals DEFAULT_MODEL,
and let explicit per-complexity config win over auto_model_routing.
"""
import pytest
from unittest.mock import Mock, patch


class FakeArgs:
    """Simulates CLI args namespace."""
    def __init__(self, model=None, dispatch_config=None):
        self.model = model
        self.dispatch_config = dispatch_config


class TestExplicitModelSentinel:
    """--model passed explicitly must be honored even when it equals DEFAULT_MODEL."""

    def test_explicit_model_equals_default_is_honored(self):
        """Regression for task-2610 bug 1: --model DEFAULT_MODEL must NOT be a no-op.

        Before the fix, get_role_model used DEFAULT_MODEL as the argparse sentinel.
        Passing --model DEFAULT_MODEL compared equal and was silently dropped,
        falling through to auto-routing or DEFAULT_ROLE_MODELS.
        """
        from equipa.roles import get_role_model
        from equipa.constants import DEFAULT_MODEL

        args = FakeArgs(model=DEFAULT_MODEL)  # user explicitly passed --model DEFAULT_MODEL
        config = {"features": {"auto_model_routing": False}}
        task = {"id": 1, "description": "fix a small bug", "title": "Fix bug", "complexity": "medium"}

        result = get_role_model("developer", args, config=config, task=task)

        assert result == DEFAULT_MODEL, (
            f"Expected explicit --model={DEFAULT_MODEL!r} to be honored even though it "
            f"equals DEFAULT_MODEL. Got: {result!r}"
        )

    def test_explicit_model_equals_default_beats_auto_routing(self):
        """Explicit --model DEFAULT_MODEL must win over auto_model_routing.

        If --model opus is passed but opus == DEFAULT_MODEL, the old sentinel
        equality check silently dropped the override and auto-routing ran instead,
        potentially returning a cheaper model (e.g. haiku for a trivial task).
        """
        from equipa.roles import get_role_model
        from equipa.constants import DEFAULT_MODEL

        args = FakeArgs(model=DEFAULT_MODEL)
        # trivial task — without the fix, auto-routing would pick haiku/sonnet
        config = {"features": {"auto_model_routing": True}}
        task = {"id": 2, "description": "fix typo", "title": "typo", "complexity": "medium"}

        result = get_role_model("developer", args, config=config, task=task)

        assert result == DEFAULT_MODEL, (
            f"Explicit --model={DEFAULT_MODEL!r} must win over auto_model_routing even "
            f"when its value equals DEFAULT_MODEL. Got: {result!r}"
        )

    def test_explicit_model_non_default_honored(self):
        """--model with a value different from DEFAULT_MODEL must be honored."""
        from equipa.roles import get_role_model
        from equipa.constants import DEFAULT_MODEL

        other_model = "claude-sonnet-4-6" if DEFAULT_MODEL != "claude-sonnet-4-6" else "claude-opus-4-8"
        args = FakeArgs(model=other_model)
        config = {"features": {"auto_model_routing": False}}
        task = {"id": 3, "description": "implement complex system", "title": "Complex", "complexity": "epic"}

        result = get_role_model("developer", args, config=config, task=task)
        assert result == other_model

    def test_no_explicit_model_falls_through(self):
        """When --model is not passed (None sentinel), fall through to config/defaults."""
        from equipa.roles import get_role_model

        args = FakeArgs(model=None)  # None = not passed by user
        config = {"features": {"auto_model_routing": False}}
        task = {"id": 4, "description": "fix typo", "title": "typo", "complexity": "medium"}

        result = get_role_model("developer", args, config=config, task=task)
        assert result is not None  # should get DEFAULT_ROLE_MODELS or config value


class TestAutoRoutingPrecedence:
    """Explicit config must win over auto_model_routing."""

    def test_per_complexity_config_beats_auto_routing(self):
        """When auto_model_routing is ON, explicit model_<complexity> config wins.

        This is the task-2610 bug 2 scenario: operator set model_epic=opus but
        auto_model_routing was routing epic tasks to sonnet/haiku via circuit-
        breaker fallback. Per-complexity config is checked BEFORE auto-routing.
        """
        from equipa.roles import get_role_model

        args = FakeArgs(model=None)
        config = {
            "features": {"auto_model_routing": True},
            "model_epic": "claude-opus-4-8",
        }
        task = {"id": 5, "description": "implement a large epic feature", "title": "Epic", "complexity": "epic"}

        result = get_role_model("developer", args, config=config, task=task)

        assert result == "claude-opus-4-8", (
            "Per-complexity config model_epic must win over auto_model_routing. "
            f"Got: {result!r}"
        )

    def test_per_complexity_config_honored_when_auto_routing_off(self):
        """When auto_model_routing is OFF, per-complexity config is used directly.

        This directly tests the requirement: 'config-model honored when auto_model_routing off'.
        """
        from equipa.roles import get_role_model

        args = FakeArgs(model=None)
        config = {
            "features": {"auto_model_routing": False},
            "model_epic": "claude-opus-4-8",
        }
        task = {"id": 6, "description": "implement an epic system", "title": "Epic", "complexity": "epic"}

        result = get_role_model("developer", args, config=config, task=task)

        assert result == "claude-opus-4-8", (
            "Per-complexity config model_epic must be used when auto_model_routing is off. "
            f"Got: {result!r}"
        )

    def test_per_role_config_honored_when_auto_routing_off(self):
        """Per-role config (model_developer) is used when auto_routing is off."""
        from equipa.roles import get_role_model

        args = FakeArgs(model=None)
        config = {
            "features": {"auto_model_routing": False},
            "model_developer": "claude-opus-4-8",
        }
        task = {"id": 7, "description": "fix a small bug", "title": "Fix", "complexity": "medium"}

        result = get_role_model("developer", args, config=config, task=task)

        assert result == "claude-opus-4-8", (
            "Per-role config model_developer must be used when auto_model_routing is off. "
            f"Got: {result!r}"
        )

    def test_per_complexity_config_has_higher_priority_than_cli(self):
        """Per-complexity config (priority 1) outranks CLI --model (priority 3).

        Documents the existing priority order: dispatch_config model_<complexity>
        wins over CLI --model. This is intentional — the operator-configured
        dispatch profile takes precedence over ad-hoc CLI overrides.
        """
        from equipa.roles import get_role_model

        args = FakeArgs(model="claude-sonnet-4-6")
        config = {
            "features": {"auto_model_routing": False},
            "model_epic": "claude-opus-4-8",
        }
        task = {"id": 8, "description": "implement epic feature", "title": "Epic", "complexity": "epic"}

        result = get_role_model("developer", args, config=config, task=task)

        # model_epic wins: per-complexity config is priority 1, CLI --model is priority 3.
        assert result == "claude-opus-4-8", (
            "Per-complexity config (priority 1) must outrank CLI --model (priority 3). "
            f"Got: {result!r}"
        )
