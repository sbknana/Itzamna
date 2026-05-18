"""Tests for plugin-disable mechanism and CLI resolution.

Covers:
- equipa.plugins.load_plugins(hooks, disabled=...) skips disabled plugins.
- equipa.cli._resolve_disabled_plugins(argv) precedence:
  CLI flag > dispatch_config.qiao_enabled > default.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# load_plugins(hooks, disabled=...)
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    def __init__(self, name: str, plugin_callable):
        self.name = name
        self._callable = plugin_callable

    def load(self):
        return self._callable


def _make_entry_points(*pairs):
    return [_FakeEntryPoint(name, fn) for name, fn in pairs]


def test_load_plugins_skips_disabled_entry():
    from equipa import plugins as plugins_mod

    hooks = MagicMock()
    loaded_a = MagicMock()
    loaded_b = MagicMock()
    loaded_c = MagicMock()

    eps = _make_entry_points(
        ("alpha", loaded_a),
        ("qiao", loaded_b),
        ("gamma", loaded_c),
    )

    with patch.object(plugins_mod, "_iter_entry_points", return_value=eps):
        count = plugins_mod.load_plugins(hooks, disabled=["qiao"])

    assert count == 2
    loaded_a.assert_called_once_with(hooks)
    loaded_c.assert_called_once_with(hooks)
    loaded_b.assert_not_called()


def test_load_plugins_no_disabled_loads_everything():
    from equipa import plugins as plugins_mod

    hooks = MagicMock()
    a = MagicMock()
    b = MagicMock()
    eps = _make_entry_points(("alpha", a), ("beta", b))

    with patch.object(plugins_mod, "_iter_entry_points", return_value=eps):
        count = plugins_mod.load_plugins(hooks, disabled=[])

    assert count == 2
    a.assert_called_once_with(hooks)
    b.assert_called_once_with(hooks)


def test_load_plugins_disabled_none_behaves_as_empty():
    from equipa import plugins as plugins_mod

    hooks = MagicMock()
    a = MagicMock()
    eps = _make_entry_points(("alpha", a))

    with patch.object(plugins_mod, "_iter_entry_points", return_value=eps):
        count = plugins_mod.load_plugins(hooks)

    assert count == 1
    a.assert_called_once_with(hooks)


def test_load_plugins_disabled_accepts_any_iterable():
    from equipa import plugins as plugins_mod

    hooks = MagicMock()
    a = MagicMock()
    b = MagicMock()
    eps = _make_entry_points(("alpha", a), ("beta", b))

    with patch.object(plugins_mod, "_iter_entry_points", return_value=eps):
        count = plugins_mod.load_plugins(hooks, disabled=iter({"beta"}))

    assert count == 1
    a.assert_called_once_with(hooks)
    b.assert_not_called()


def test_load_plugins_count_reflects_only_loaded():
    from equipa import plugins as plugins_mod

    hooks = MagicMock()
    eps = _make_entry_points(
        ("a", MagicMock()),
        ("b", MagicMock()),
        ("c", MagicMock()),
        ("d", MagicMock()),
    )

    with patch.object(plugins_mod, "_iter_entry_points", return_value=eps):
        count = plugins_mod.load_plugins(hooks, disabled=["b", "d"])

    assert count == 2


# ---------------------------------------------------------------------------
# cli._resolve_disabled_plugins(argv)
# ---------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    cfg = tmp_path / "dispatch_config.json"
    monkeypatch.setenv("EQUIPA_DISPATCH_CONFIG", str(cfg))
    return cfg


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _resolve(argv, config_path):
    from equipa import cli

    # Re-route the function to read from our test config path if it uses
    # a module-level constant.  Patch both the env var and the read helper
    # so behaviour is deterministic regardless of internal implementation.
    with patch.object(cli, "_DISPATCH_CONFIG_PATH", config_path, create=True):
        return cli._resolve_disabled_plugins(list(argv))


def test_qiao_flag_on_overrides_disabled_default(config_path):
    result = _resolve(["--qiao", "on"], config_path)
    assert result == []


def test_qiao_flag_off_disables_qiao(config_path):
    result = _resolve(["--qiao", "off"], config_path)
    assert result == ["qiao"]


def test_qiao_equals_on(config_path):
    result = _resolve(["--qiao=on"], config_path)
    assert result == []


def test_qiao_equals_off(config_path):
    result = _resolve(["--qiao=off"], config_path)
    assert result == ["qiao"]


def test_config_top_level_qiao_enabled_true(config_path):
    _write_config(config_path, {"qiao_enabled": True})
    result = _resolve([], config_path)
    assert result == []


def test_config_legacy_features_qiao_enabled_true(config_path):
    _write_config(config_path, {"features": {"qiao_enabled": True}})
    result = _resolve([], config_path)
    assert result == []


def test_no_flag_no_config_defaults_to_qiao_disabled(config_path):
    # config file does not exist
    assert not config_path.exists()
    result = _resolve([], config_path)
    assert result == ["qiao"]


def test_config_present_without_qiao_field_defaults_disabled(config_path):
    _write_config(config_path, {"other_setting": True})
    result = _resolve([], config_path)
    assert result == ["qiao"]


def test_malformed_config_falls_back_to_default(config_path):
    config_path.write_text("{this is not json")
    result = _resolve([], config_path)
    assert result == ["qiao"]


def test_unreadable_config_falls_back_to_default(config_path, monkeypatch):
    _write_config(config_path, {"qiao_enabled": True})

    from equipa import cli

    real_open = open

    def bad_open(file, *args, **kwargs):
        if str(file) == str(config_path):
            raise OSError("simulated unreadable")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", bad_open)
    result = cli._resolve_disabled_plugins([])
    assert result == ["qiao"]


def test_cli_flag_overrides_config_disabled(config_path):
    # config says qiao on, CLI says off -> CLI wins
    _write_config(config_path, {"qiao_enabled": True})
    result = _resolve(["--qiao", "off"], config_path)
    assert result == ["qiao"]


def test_cli_flag_overrides_config_enabled(config_path):
    # config says qiao off (implicit), CLI says on -> CLI wins
    _write_config(config_path, {"qiao_enabled": False})
    result = _resolve(["--qiao", "on"], config_path)
    assert result == []
