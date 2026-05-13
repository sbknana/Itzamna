"""Unit tests for ``equipa.config.is_security_review_enabled``.

Bug 2321 was caused by two divergent implementations of the security-review
enablement precedence chain (one in ``equipa.cli:run_dispatch``, one in
``equipa.dispatch:_is_security_review_enabled``). The S3 follow-up
extracted a single canonical helper into ``equipa.config`` so both call
sites apply identical rules.

Precedence (highest first):
  1. CLI flag ``args.security_review`` (when not None).
  2. ``dispatch_config["security_review"]`` top-level key.
  3. ``features.security_review`` feature flag — kill-switch that can
     disable even when (1) or (2) would enable.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from types import SimpleNamespace

from equipa.config import is_security_review_enabled


def _args(security_review=None, dispatch_config=None) -> SimpleNamespace:
    """Build a minimal argparse-like Namespace for the helper."""
    return SimpleNamespace(
        security_review=security_review,
        dispatch_config=dispatch_config or {},
    )


def test_cli_flag_overrides_config_disabled():
    """--security-review on CLI wins even when dispatch config disables."""
    args = _args(
        security_review=True,
        dispatch_config={"security_review": False},
    )
    assert is_security_review_enabled(args) is True


def test_cli_flag_false_overrides_config_enabled():
    """--no-security-review on CLI wins even when dispatch config enables."""
    args = _args(
        security_review=False,
        dispatch_config={"security_review": True},
    )
    assert is_security_review_enabled(args) is False


def test_config_top_level_when_no_cli():
    """dispatch_config.security_review=True enables when CLI flag unset."""
    args = _args(
        security_review=None,
        dispatch_config={"security_review": True},
    )
    assert is_security_review_enabled(args) is True


def test_feature_flag_can_disable():
    """features.security_review=False disables regardless of CLI/config."""
    args = _args(
        security_review=True,
        dispatch_config={
            "security_review": True,
            "features": {"security_review": False},
        },
    )
    assert is_security_review_enabled(args) is False


def test_feature_flag_can_disable_config_top_level():
    """Feature flag also disables the dispatch_config top-level path."""
    args = _args(
        security_review=None,
        dispatch_config={
            "security_review": True,
            "features": {"security_review": False},
        },
    )
    assert is_security_review_enabled(args) is False


def test_default_disabled_when_unset():
    """No CLI flag, no dispatch_config = disabled."""
    args = _args(security_review=None, dispatch_config={})
    assert is_security_review_enabled(args) is False


def test_explicit_dispatch_config_arg_overrides_args_attr():
    """When dispatch_config is passed explicitly, it takes priority over
    args.dispatch_config — supports call sites that thread config separately."""
    args = _args(
        security_review=None,
        dispatch_config={"security_review": False},
    )
    # Explicit override should enable even though args.dispatch_config disables.
    assert (
        is_security_review_enabled(
            args, dispatch_config={"security_review": True}
        )
        is True
    )


def test_missing_security_review_attr_on_args():
    """An args-like object without ``security_review`` falls back to config."""
    args = SimpleNamespace(dispatch_config={"security_review": True})
    assert is_security_review_enabled(args) is True


def test_none_dispatch_config_uses_args_fallback():
    """Passing dispatch_config=None falls back to args.dispatch_config."""
    args = _args(
        security_review=None,
        dispatch_config={"security_review": True},
    )
    assert is_security_review_enabled(args, dispatch_config=None) is True
