"""
Tests for task-2610: honor explicit --model even when it equals DEFAULT_MODEL,
and let explicit config win over auto_model_routing.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Ensure equipa package is importable from worktree
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equipa.roles import resolve_model_for_role
from equipa.constants import DEFAULT_MODEL


class FakeArgs:
    """Simulates CLI args namespace."""
    def __init__(self, model=None, role=None, complexity=None):
        self.model = model
        self.role = role
        self.complexity = complexity


class TestExplicitModelSentinel:
    """--model passed explicitly must be honored even when it equals DEFAULT_MODEL."""

    def test_explicit_model_equals_default_is_honored(self):
        """Regression: --model DEFAULT_MODEL must NOT be silently ignored."""
        args = FakeArgs(model=DEFAULT_MODEL)
        features = {"auto_model_routing": False}
        dispatch_config = {}
        result = resolve_model_for_role(
            role="developer",
            complexity="epic",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        assert result == DEFAULT_MODEL, (
            f"Expected explicit --model={DEFAULT_MODEL!r} to be honored, got {result!r}. "
            "Equality with DEFAULT_MODEL must not cause a silent no-op."
        )

    def test_explicit_model_non_default_honored(self):
        """--model with a value different from DEFAULT_MODEL must still be honored."""
        args = FakeArgs(model="claude-sonnet-4-6")
        features = {"auto_model_routing": False}
        dispatch_config = {}
        result = resolve_model_for_role(
            role="developer",
            complexity="epic",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        assert result == "claude-sonnet-4-6"

    def test_no_explicit_model_uses_config_or_default(self):
        """When --model is not passed (None sentinel), fall through to config/default."""
        args = FakeArgs(model=None)
        features = {"auto_model_routing": False}
        dispatch_config = {}
        result = resolve_model_for_role(
            role="developer",
            complexity="epic",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        # Should not raise; result is determined by config/defaults
        assert result is not None

    def test_no_explicit_model_with_auto_routing_off_uses_role_config(self):
        """When --model is None and auto_model_routing is off, per-role config wins."""
        args = FakeArgs(model=None)
        features = {"auto_model_routing": False}
        dispatch_config = {
            "model_epic": "claude-opus-4-8",
        }
        result = resolve_model_for_role(
            role="developer",
            complexity="epic",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        assert result == "claude-opus-4-8", (
            f"Per-complexity config 'model_epic=claude-opus-4-8' must win when "
            f"auto_model_routing is off and no explicit --model given. Got: {result!r}"
        )


class TestAutoRoutingPrecedence:
    """auto_model_routing must NOT override explicit --model or per-complexity config."""

    def test_explicit_model_beats_auto_routing(self):
        """--model must win over auto_model_routing complexity scoring."""
        args = FakeArgs(model="claude-opus-4-8")
        features = {"auto_model_routing": True}
        dispatch_config = {}
        # auto_model_routing is on, but explicit --model was given
        result = resolve_model_for_role(
            role="developer",
            complexity="simple",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        assert result == "claude-opus-4-8", (
            "Explicit --model must win over auto_model_routing complexity scoring. "
            f"Got: {result!r}"
        )

    def test_per_complexity_config_beats_auto_routing_when_disabled(self):
        """When auto_model_routing is off, per-complexity config must be used."""
        args = FakeArgs(model=None)
        features = {"auto_model_routing": False}
        dispatch_config = {
            "model_epic": "claude-opus-4-8",
        }
        result = resolve_model_for_role(
            role="developer",
            complexity="epic",
            args=args,
            features=features,
            dispatch_config=dispatch_config,
        )
        assert result == "claude-opus-4-8"

    def test_auto_routing_used_when_no_explicit_override(self):
        """When auto_model_routing is on and no explicit --model or config, routing runs."""
        args = FakeArgs(model=None)
        features = {"auto_model_routing": True}
        dispatch_config = {}
        mock_model = "claude-sonnet-4-6"

        with patch("equipa.roles.auto_select_model", return_value=mock_model):
            result = resolve_model_for_role(
                role="developer",
                complexity="simple",
                args=args,
                features=features,
                dispatch_config=dispatch_config,
            )
        assert result == mock_model
