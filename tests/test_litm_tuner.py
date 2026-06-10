#!/usr/bin/env python3
"""Tests for ForgeSmith LITM weight tuner."""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from forgesmith_litm import (
    detect_middle_attention_misses,
    analyze_miss_distribution,
    adjust_weights,
    load_litm_weights,
    save_litm_weights,
    run_litm_audit,
    DEFAULT_WEIGHTS,
)


def create_test_db():
    """Create a temporary test database with sample agent runs."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create agent_runs table
    cursor.execute("""
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY,
            model TEXT,
            output TEXT,
            tool_calls TEXT,
            status TEXT,
            created_at TEXT
        )
    """)

    # Insert test data
    now = datetime.now()
    yesterday = now - timedelta(days=1)

    # Case 1: Re-read pattern
    cursor.execute("""
        INSERT INTO agent_runs (model, output, tool_calls, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        "sonnet",
        "Reading from compaction checkpoint. Let me re-read the file.",
        '{"tool": "Read", "file_path": "test.py"}',
        "completed",
        yesterday.isoformat()
    ))

    # Case 2: Clarification question
    cursor.execute("""
        INSERT INTO agent_runs (model, output, tool_calls, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        "opus",
        "I'm not sure what you mean by that. Can you clarify?",
        "",
        "completed",
        yesterday.isoformat()
    ))

    # Case 3: Normal run (no miss)
    cursor.execute("""
        INSERT INTO agent_runs (model, output, tool_calls, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        "sonnet",
        "Successfully completed the task.",
        "",
        "completed",
        yesterday.isoformat()
    ))

    # Case 4: Multiple misses
    for i in range(5):
        cursor.execute("""
            INSERT INTO agent_runs (model, output, tool_calls, status, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "haiku",
            f"Reviewing anti-compaction state. I need to ask what approach to use.",
            '{"name": "Read"}',
            "completed",
            (yesterday - timedelta(hours=i)).isoformat()
        ))

    conn.commit()
    conn.close()

    return db_path


def test_detect_middle_attention_misses():
    """Test detection of missed-attention events."""
    db_path = create_test_db()

    misses = detect_middle_attention_misses(db_path, lookback_days=7)

    # We expect at least 3 types of misses: 1 re_read + 1 clarification + 5 haiku misses
    assert len(misses) >= 3, f"Expected at least 3 misses, got {len(misses)}"

    # Check we have different models
    models = set(m[0] for m in misses)
    assert "sonnet" in models
    assert "opus" in models or "haiku" in models

    Path(db_path).unlink()
    print("✓ test_detect_middle_attention_misses passed")


def test_analyze_miss_distribution():
    """Test miss distribution analysis."""
    misses = [
        ("sonnet", "run1", "re_read"),
        ("sonnet", "run2", "clarification"),
        ("opus", "run3", "re_read"),
        ("haiku", "run4", "clarification"),
        ("haiku", "run5", "clarification"),
    ]

    distribution = analyze_miss_distribution(misses)

    assert "sonnet" in distribution
    assert distribution["sonnet"]["total"] == 2
    assert distribution["opus"]["total"] == 1
    assert distribution["haiku"]["total"] == 2
    assert distribution["haiku"]["clarification"] == 2

    print("✓ test_analyze_miss_distribution passed")


def test_adjust_weights():
    """Test weight adjustment logic."""
    current_weights = DEFAULT_WEIGHTS.copy()

    # Scenario: sonnet has 6 misses (above threshold of 5)
    distribution = {
        "sonnet": {"re_read": 3, "clarification": 3, "total": 6},
        "opus": {"re_read": 1, "clarification": 1, "total": 2},
    }

    updated_weights, change_log = adjust_weights(current_weights, distribution, threshold=5)

    # sonnet should have increased beta
    assert updated_weights["sonnet"]["beta"] > current_weights["sonnet"]["beta"]
    assert len(change_log) == 1
    assert "sonnet" in change_log[0]
    assert "beta" in change_log[0]

    # opus should be unchanged (below threshold)
    assert updated_weights["opus"]["beta"] == current_weights["opus"]["beta"]

    print("✓ test_adjust_weights passed")


def test_weight_caps():
    """Test that weights respect maximum caps."""
    # Start with weights already near cap
    current_weights = {
        "sonnet": {"alpha": 0.94, "beta": 0.64, "gamma": 0.91},
    }

    # High miss count
    distribution = {
        "sonnet": {"total": 10},
    }

    updated_weights, _ = adjust_weights(current_weights, distribution, threshold=5)

    # Beta should be capped at BETA_MAX (0.65)
    assert updated_weights["sonnet"]["beta"] <= 0.65

    print("✓ test_weight_caps passed")


def test_load_save_weights():
    """Test loading and saving weights to config file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config_path = Path(f.name)
        json.dump({}, f)

    # Save weights
    test_weights = {
        "sonnet": {"alpha": 0.90, "beta": 0.55, "gamma": 0.85},
    }
    save_litm_weights(config_path, test_weights)

    # Load weights
    loaded_weights = load_litm_weights(config_path)

    assert loaded_weights["sonnet"]["beta"] == 0.55
    assert loaded_weights["sonnet"]["alpha"] == 0.90

    # Check defaults are filled for missing models
    assert "opus" in loaded_weights
    assert loaded_weights["opus"]["beta"] == DEFAULT_WEIGHTS["opus"]["beta"]

    config_path.unlink()
    print("✓ test_load_save_weights passed")


def test_full_audit_dry_run():
    """Test full audit in dry-run mode."""
    db_path = create_test_db()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config_path = Path(f.name)
        json.dump({}, f)

    report = run_litm_audit(
        db_path=db_path,
        dispatch_config_path=config_path,
        lookback_days=7,
        threshold=3,
        dry_run=True
    )

    assert "total_misses" in report
    assert "distribution" in report
    assert "changes" in report
    assert report["dry_run"] is True

    # In dry-run mode, config should not be modified
    loaded_weights = load_litm_weights(config_path)
    assert loaded_weights == DEFAULT_WEIGHTS

    Path(db_path).unlink()
    config_path.unlink()
    print("✓ test_full_audit_dry_run passed")


def test_full_audit_with_apply():
    """Test full audit with weight application."""
    db_path = create_test_db()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config_path = Path(f.name)
        json.dump({}, f)

    report = run_litm_audit(
        db_path=db_path,
        dispatch_config_path=config_path,
        lookback_days=7,
        threshold=3,  # Lower threshold to trigger changes
        dry_run=False
    )

    assert report["dry_run"] is False

    # If changes were made, config should be updated
    if report["changes"]:
        loaded_weights = load_litm_weights(config_path)
        # At least one model should have different weights
        changed = any(
            loaded_weights[model]["beta"] != DEFAULT_WEIGHTS[model]["beta"]
            for model in loaded_weights
            if model in DEFAULT_WEIGHTS
        )
        assert changed, "Expected at least one weight to be updated"

    Path(db_path).unlink()
    config_path.unlink()
    print("✓ test_full_audit_with_apply passed")


def create_drifted_db():
    """Create a DB whose agent_runs matches the REAL (drifted) schema.

    The production agent_runs table has NO output/tool_calls/status columns —
    only the structured run-metrics columns. This is the schema-drift case
    that crashed PHASE 4.9 (bug #2532). The audit must skip it gracefully.
    """
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            outcome TEXT,
            success INTEGER DEFAULT 0,
            error_summary TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO agent_runs (model, role, outcome, success) "
        "VALUES ('sonnet', 'developer', 'tests_passed', 1)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_drifted_schema_no_output_column_skips_gracefully():
    """detect_middle_attention_misses must not crash when output column absent."""
    db_path = create_drifted_db()
    try:
        misses = detect_middle_attention_misses(db_path, lookback_days=7)
        assert misses == [], f"Expected empty list on drifted schema, got {misses}"
    finally:
        Path(db_path).unlink()
    print("✓ test_drifted_schema_no_output_column_skips_gracefully passed")


def test_missing_agent_runs_table_skips_gracefully():
    """Audit must skip (return []) when agent_runs table does not exist."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    try:
        # Empty DB — no agent_runs table at all.
        misses = detect_middle_attention_misses(db_path, lookback_days=7)
        assert misses == [], f"Expected empty list when table missing, got {misses}"
    finally:
        Path(db_path).unlink()
    print("✓ test_missing_agent_runs_table_skips_gracefully passed")


def test_full_audit_on_drifted_schema_no_crash():
    """run_litm_audit must complete (no changes) on the drifted prod schema."""
    db_path = create_drifted_db()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config_path = Path(f.name)
        json.dump({}, f)
    try:
        report = run_litm_audit(
            db_path=db_path,
            dispatch_config_path=config_path,
            lookback_days=7,
            threshold=3,
            dry_run=False,
        )
        assert report["total_misses"] == 0
        assert report["changes"] == []
        # Weights untouched because there were no misses.
        assert load_litm_weights(config_path) == DEFAULT_WEIGHTS
    finally:
        Path(db_path).unlink()
        config_path.unlink()
    print("✓ test_full_audit_on_drifted_schema_no_crash passed")


def test_missing_status_column_still_detects():
    """When status column is absent but output exists, detection still runs."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY,
            model TEXT,
            output TEXT,
            created_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO agent_runs (model, output, created_at) VALUES (?, ?, ?)",
        ("opus", "I need to clarify what you mean.",
         (datetime.now() - timedelta(days=1)).isoformat()),
    )
    conn.commit()
    conn.close()
    try:
        misses = detect_middle_attention_misses(db_path, lookback_days=7)
        assert any(m[2] == "clarification" for m in misses), misses
    finally:
        Path(db_path).unlink()
    print("✓ test_missing_status_column_still_detects passed")


if __name__ == "__main__":
    test_detect_middle_attention_misses()
    test_analyze_miss_distribution()
    test_adjust_weights()
    test_weight_caps()
    test_load_save_weights()
    test_full_audit_dry_run()
    test_full_audit_with_apply()
    test_drifted_schema_no_output_column_skips_gracefully()
    test_missing_agent_runs_table_skips_gracefully()
    test_full_audit_on_drifted_schema_no_crash()
    test_missing_status_column_still_detects()
    print("\n✅ All LITM tuner tests passed!")
