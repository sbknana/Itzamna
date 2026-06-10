#!/usr/bin/env python3
"""Tests for the OPRO meta-prompt sizing fix (bug #2532).

Phase 4.7 OPRO timed out at 2 min and returned 0 proposals because the
hard-coded 120s timeout was too short and the embedded role prompt was too
large. The fix raised the default timeout to 300s and truncates the embedded
prompt via ``truncate_opro_prompt``.

These tests exercise the pure truncation helper and the ``load_config`` OPRO
defaults directly — no Claude CLI, no populated database, nothing env-gated —
so they actually run and validate the fix rather than skipping.
"""

import pytest

from forgesmith import truncate_opro_prompt, load_config


def test_short_prompt_is_unchanged():
    """Prompts at or under the cap pass through untouched."""
    prompt = "short prompt"
    assert truncate_opro_prompt(prompt, max_prompt_chars=12000) == prompt


def test_prompt_exactly_at_cap_is_unchanged():
    """A prompt exactly max_prompt_chars long is not truncated (<= boundary)."""
    prompt = "x" * 12000
    assert truncate_opro_prompt(prompt, max_prompt_chars=12000) == prompt


def test_long_prompt_is_truncated_and_marked():
    """Oversized prompts get a head+tail with an omission marker in between."""
    prompt = "A" * 5000 + "B" * 30000 + "C" * 5000  # 40000 chars
    result = truncate_opro_prompt(prompt, max_prompt_chars=12000)

    assert len(result) < len(prompt)
    assert "chars omitted" in result
    # Head and tail of the original are preserved.
    assert result.startswith("A")
    assert result.endswith("C")
    # The exact omitted count is reported.
    assert f"{40000 - 12000} chars omitted" in result


def test_truncated_result_stays_bounded():
    """Result length stays close to the cap regardless of input size."""
    prompt = "z" * 1_000_000
    result = truncate_opro_prompt(prompt, max_prompt_chars=12000)
    # cap + a small constant marker; never the full million chars.
    assert len(result) <= 12000 + 200


def test_head_and_tail_split_covers_cap():
    """Head + tail slices together account for max_prompt_chars characters."""
    prompt = "H" * 8000 + "T" * 8000  # 16000 chars
    result = truncate_opro_prompt(prompt, max_prompt_chars=12000)
    head_kept = len(result) - len(result.lstrip("H"))
    tail_kept = len(result) - len(result.rstrip("T"))
    assert head_kept == 12000 // 2
    assert tail_kept == 12000 - (12000 // 2)


@pytest.mark.parametrize("disabled", [0, None])
def test_truncation_can_be_disabled(disabled):
    """A falsy cap disables truncation entirely."""
    prompt = "Q" * 50000
    assert truncate_opro_prompt(prompt, max_prompt_chars=disabled) == prompt


def test_load_config_has_opro_timeout_above_two_minutes():
    """The OPRO timeout default must exceed the old 120s that caused 0 proposals."""
    cfg = load_config()
    opro = cfg.get("opro", {})
    assert opro.get("timeout_seconds", 0) > 120
    assert opro.get("timeout_seconds") == 300


def test_load_config_has_max_prompt_chars():
    """The OPRO config exposes a prompt-size cap so meta-prompts stay small."""
    cfg = load_config()
    opro = cfg.get("opro", {})
    assert opro.get("max_prompt_chars", 0) > 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
