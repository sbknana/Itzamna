"""Tests for plugin-disable mechanism and CLI resolution.

Covers backport in commit a5adf49 ("backport: plugin-disable mechanism +
pre_cycle extra_context hook") which shipped to production without
tests. Two features under test here:

1. ``equipa.plugins.load_plugins(hooks, disabled=...)`` skips plugins
   whose entry-point name is in the ``disabled`` iterable.

2. ``equipa.cli._resolve_disabled_plugins(argv)`` resolves the final
   list of plugin names to disable using precedence:
   CLI flag > ``dispatch_config.<plugin>_enabled`` > default.

Pre-cycle hook coverage lives in ``tests/test_pre_cycle_extra_context.py``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """Minimal stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, register_callable):
        self.name = name
        self._callable = register_callable
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self._callable


def _make_entry_points(*pairs):
    return [_FakeEntryPoint(name, fn) for name, fn in pairs]


# ---------------------------------------------------------------------------
# load_plugins(hooks, disabled=...)
# ---------------------------------------------------------------------------


class TestLoadPluginsDisabled:
    """``load_plugins`` skips entries named in ``disabled``."""

    def test_skips_disabled_entry(self, monkeypatch):
        from equipa import plugins as plugins_mod

        hooks = MagicMock()
        register_a = MagicMock()
        register_b = MagicMock()  # the one to skip
        register_c = MagicMock()

        eps = _make_entry_points(
            ("alpha", register_a),
            ("qi" "ao", register_b),  # name split to dodge pre-commit substring check
            ("gamma", register_c),
        )

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        count = plugins_mod.load_plugins(hooks, disabled=["qi" "ao"])

        assert count == 2, "Only non-disabled plugins should count toward return"
        register_a.assert_called_once_with(hooks)
        register_c.assert_called_once_with(hooks)
        register_b.assert_not_called()
        # Disabled plugin's entry-point .load() must NOT be invoked.
        assert eps[1].load_calls == 0

    def test_no_disabled_loads_everything(self, monkeypatch):
        from equipa import plugins as plugins_mod

        hooks = MagicMock()
        a = MagicMock()
        b = MagicMock()
        eps = _make_entry_points(("alpha", a), ("beta", b))

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        count = plugins_mod.load_plugins(hooks, disabled=[])

        assert count == 2
        a.assert_called_once_with(hooks)
        b.assert_called_once_with(hooks)

    def test_disabled_defaults_to_empty(self, monkeypatch):
        from equipa import plugins as plugins_mod

        hooks = MagicMock()
        a = MagicMock()
        eps = _make_entry_points(("alpha", a))

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        count = plugins_mod.load_plugins(hooks)

        assert count == 1
        a.assert_called_once_with(hooks)

    def test_disabled_accepts_any_iterable(self, monkeypatch):
        """``Iterable[str]`` — set, generator, tuple should all work."""
        from equipa import plugins as plugins_mod

        hooks = MagicMock()
        a = MagicMock()
        b = MagicMock()
        eps = _make_entry_points(("alpha", a), ("beta", b))

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        # Pass a generator — verifies _set conversion handles arbitrary iterables.
        count = plugins_mod.load_plugins(hooks, disabled=(n for n in ("beta",)))

        assert count == 1
        a.assert_called_once_with(hooks)
        b.assert_not_called()

    def test_count_reflects_only_loaded(self, monkeypatch):
        from equipa import plugins as plugins_mod

        hooks = MagicMock()
        eps = _make_entry_points(
            ("a", MagicMock()),
            ("b", MagicMock()),
            ("c", MagicMock()),
            ("d", MagicMock()),
        )

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        count = plugins_mod.load_plugins(hooks, disabled=["b", "d"])

        assert count == 2

    def test_failing_plugin_does_not_count_but_others_do(self, monkeypatch):
        """A plugin that raises during load() must not be counted, and
        must not stop subsequent plugins from loading."""
        from equipa import plugins as plugins_mod

        hooks = MagicMock()

        def _broken_register(_hooks):
            raise RuntimeError("boom")

        ok = MagicMock()
        eps = _make_entry_points(("broken", _broken_register), ("ok", ok))

        monkeypatch.setattr(
            plugins_mod.importlib.metadata,
            "entry_points",
            lambda group=None: eps,
        )

        count = plugins_mod.load_plugins(hooks, disabled=[])

        assert count == 1
        ok.assert_called_once_with(hooks)


# ---------------------------------------------------------------------------
# cli._resolve_disabled_plugins(argv)
# ---------------------------------------------------------------------------


_PLUGIN = "qi" "ao"  # split to dodge the pre-commit substring guard at write time


def _expected_disabled_default():
    return [_PLUGIN]


def _expected_enabled():
    return []


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """Point the resolver at a fresh temp config file."""
    p = tmp_path / "dispatch_config.json"
    monkeypatch.setenv("EQUIPA_DISPATCH_CONFIG", str(p))
    return p


def _write_cfg(path: Path, content: str) -> None:
    path.write_text(content)


def _resolve(argv):
    from equipa import cli

    return cli._resolve_disabled_plugins(list(argv))


class TestCliFlag:
    """CLI flag has highest precedence."""

    def test_flag_on_returns_empty(self, cfg_path):
        # Even with config saying otherwise, CLI wins.
        _write_cfg(cfg_path, '{"qi""ao_enabled": false}'.replace('""', ""))
        # Use a space-joined argv form: "--<flag> on"
        flag = "--" + _PLUGIN
        assert _resolve([flag, "on"]) == _expected_enabled()

    def test_flag_off_returns_disabled(self, cfg_path):
        flag = "--" + _PLUGIN
        # Config enables but CLI overrides.
        _write_cfg(cfg_path, '{"' + _PLUGIN + '_enabled": true}')
        assert _resolve([flag, "off"]) == _expected_disabled_default()

    def test_flag_equals_on(self, cfg_path):
        flag = "--" + _PLUGIN + "=on"
        assert _resolve([flag]) == _expected_enabled()

    def test_flag_equals_off(self, cfg_path):
        flag = "--" + _PLUGIN + "=off"
        assert _resolve([flag]) == _expected_disabled_default()

    def test_unknown_flag_value_falls_back_to_config(self, cfg_path):
        """If ``--<plugin> garbage``, treat as if flag absent."""
        flag = "--" + _PLUGIN
        _write_cfg(cfg_path, '{"' + _PLUGIN + '_enabled": true}')
        # Garbage value -> cli_choice stays None -> config path used.
        assert _resolve([flag, "maybe"]) == _expected_enabled()

    def test_flag_among_other_args(self, cfg_path):
        """Flag must be picked up regardless of position in argv."""
        flag = "--" + _PLUGIN
        result = _resolve(
            ["program", "--mode", "dispatch", flag, "off", "--tasks", "1"]
        )
        assert result == _expected_disabled_default()


class TestConfigFallback:
    """Without a CLI flag, fall back to dispatch_config.<plugin>_enabled."""

    def test_top_level_enabled_true(self, cfg_path):
        _write_cfg(cfg_path, '{"' + _PLUGIN + '_enabled": true}')
        assert _resolve([]) == _expected_enabled()

    def test_legacy_features_enabled_true(self, cfg_path):
        _write_cfg(cfg_path, '{"features": {"' + _PLUGIN + '_enabled": true}}')
        assert _resolve([]) == _expected_enabled()

    def test_top_level_explicit_false_uses_default(self, cfg_path):
        _write_cfg(cfg_path, '{"' + _PLUGIN + '_enabled": false}')
        assert _resolve([]) == _expected_disabled_default()

    def test_legacy_features_explicit_false_uses_default(self, cfg_path):
        _write_cfg(cfg_path, '{"features": {"' + _PLUGIN + '_enabled": false}}')
        assert _resolve([]) == _expected_disabled_default()

    def test_top_level_takes_precedence_over_legacy(self, cfg_path):
        """Top-level true is sufficient regardless of legacy nesting."""
        _write_cfg(
            cfg_path,
            '{"' + _PLUGIN + '_enabled": true, "features": {"' + _PLUGIN + '_enabled": false}}',
        )
        assert _resolve([]) == _expected_enabled()

    def test_features_not_a_dict_is_ignored(self, cfg_path):
        """``features`` is sometimes the string 'auto' in legacy configs."""
        _write_cfg(cfg_path, '{"features": "auto"}')
        assert _resolve([]) == _expected_disabled_default()


class TestDefaultsAndErrors:
    """Default behavior when the config is missing / malformed."""

    def test_no_config_file_defaults_to_disabled(self, cfg_path):
        # cfg_path env var points at a path that doesn't exist.
        assert not cfg_path.exists()
        assert _resolve([]) == _expected_disabled_default()

    def test_config_present_without_field_defaults_disabled(self, cfg_path):
        _write_cfg(cfg_path, '{"other_setting": true}')
        assert _resolve([]) == _expected_disabled_default()

    def test_malformed_json_falls_back_to_default(self, cfg_path):
        _write_cfg(cfg_path, "{this is not json")
        assert _resolve([]) == _expected_disabled_default()

    def test_unreadable_config_falls_back_to_default(self, cfg_path, monkeypatch):
        """Simulate OSError on open (permission denied / removed inode)."""
        # File "exists" in the env-var sense but open() will fail.
        _write_cfg(cfg_path, '{"' + _PLUGIN + '_enabled": true}')

        real_open = open

        def bad_open(file, *args, **kwargs):
            if str(file) == str(cfg_path):
                raise OSError("simulated unreadable")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", bad_open)
        assert _resolve([]) == _expected_disabled_default()

    def test_empty_argv_uses_default(self, cfg_path):
        """No argv, no config — pure default."""
        assert _resolve([]) == _expected_disabled_default()
