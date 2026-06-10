#!/usr/bin/env python3
"""ForgeSmith LITM Weight Tuner — Self-tuning Lost-in-the-Middle attention weights.

Tracks missed-attention events weekly and auto-adjusts LITM positional weights
per model (opus/sonnet/haiku) to optimize prompt ordering.

Default weights (Claude): alpha=0.92 (start), beta=0.50 (middle), gamma=0.88 (end)
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Columns this audit needs from agent_runs to detect text-based attention
# misses. The schema drifted (agent_runs never had a free-text output column),
# so we introspect at runtime and skip gracefully when they are absent rather
# than crashing the nightly run with `sqlite3.OperationalError: no such column`.
REQUIRED_EVIDENCE_COLUMNS = ("output",)
OPTIONAL_EVIDENCE_COLUMNS = ("tool_calls",)


# --- Weight Bounds ---
ALPHA_MAX = 0.95  # Start attention cap
BETA_MAX = 0.65   # Middle attention cap
GAMMA_MAX = 0.92  # End attention cap
WEIGHT_NUDGE = 0.02  # Weekly adjustment increment


# --- Default Weights ---
DEFAULT_WEIGHTS = {
    "claude": {"alpha": 0.92, "beta": 0.50, "gamma": 0.88},
    "sonnet": {"alpha": 0.92, "beta": 0.50, "gamma": 0.88},
    "opus": {"alpha": 0.92, "beta": 0.50, "gamma": 0.88},
    "haiku": {"alpha": 0.92, "beta": 0.50, "gamma": 0.88},
}


def load_litm_weights(dispatch_config_path: Path) -> Dict[str, Dict[str, float]]:
    """Load LITM weights from dispatch_config.json."""
    if not dispatch_config_path.exists():
        return DEFAULT_WEIGHTS.copy()

    try:
        with open(dispatch_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        weights = config.get("litm_weights", {})

        # Fill missing models with defaults
        for model, defaults in DEFAULT_WEIGHTS.items():
            if model not in weights:
                weights[model] = defaults.copy()

        return weights
    except (json.JSONDecodeError, IOError):
        return DEFAULT_WEIGHTS.copy()


def save_litm_weights(dispatch_config_path: Path, weights: Dict[str, Dict[str, float]]) -> None:
    """Save LITM weights to dispatch_config.json."""
    config = {}

    if dispatch_config_path.exists():
        try:
            with open(dispatch_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    config["litm_weights"] = weights

    with open(dispatch_config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for a table (empty set if absent)."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def detect_middle_attention_misses(db_path: str, lookback_days: int = 7) -> List[Tuple[str, str, str]]:
    """Detect missed-attention events from agent output.

    Returns list of (model, agent_run_id, evidence_type) tuples.

    Evidence types:
    - 're_read': Agent re-reads a file already in compaction history
    - 'clarification': Agent asks question answered in mid-prompt context

    The detection relies on a free-text ``output`` column on ``agent_runs``.
    That column does not exist in the current schema (schema drift), so we
    introspect the table first and skip the audit gracefully — returning an
    empty list — when the required evidence columns are missing. This keeps
    the nightly ``--auto`` run from crashing at PHASE 4.9.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        columns = _table_columns(conn, "agent_runs")
        if not columns:
            logger.warning("LITM audit skipped: agent_runs table not found in %s", db_path)
            return []

        missing = [c for c in REQUIRED_EVIDENCE_COLUMNS if c not in columns]
        if missing:
            logger.warning(
                "LITM audit skipped: agent_runs is missing evidence column(s) %s "
                "(schema drift — text-based attention detection unavailable)",
                ", ".join(missing),
            )
            return []

        # Build the SELECT from columns that actually exist, so optional
        # evidence (tool_calls) and the status filter degrade gracefully.
        select_cols = ["ar.id", "ar.model", "ar.output"]
        has_tool_calls = "tool_calls" in columns
        if has_tool_calls:
            select_cols.append("ar.tool_calls")

        where_clauses = ["ar.created_at >= ?", "ar.output IS NOT NULL"]
        if "status" in columns:
            where_clauses.append("ar.status IN ('completed', 'early_terminated')")

        query = (
            f"SELECT {', '.join(select_cols)} FROM agent_runs ar "
            f"WHERE {' AND '.join(where_clauses)}"
        )

        cutoff = datetime.now() - timedelta(days=lookback_days)
        cursor = conn.execute(query, (cutoff.isoformat(),))
        rows = cursor.fetchall()
    finally:
        conn.close()

    misses = []

    for row in rows:
        agent_run_id = row["id"]
        model = row["model"] or "sonnet"
        output = row["output"] or ""
        tool_calls = row["tool_calls"] if "tool_calls" in row.keys() else ""
        tool_calls = tool_calls or ""

        # Pattern 1: Re-reading files mentioned in compaction checkpoint
        # Look for "compaction checkpoint" or "anti-compaction" references
        # followed by file reads in tool_calls
        if "compaction checkpoint" in output.lower() or "anti-compaction" in output.lower():
            # Check if tool_calls contains Read operations
            if '"tool": "Read"' in tool_calls or '"name": "Read"' in tool_calls:
                misses.append((model, agent_run_id, "re_read"))

        # Pattern 2: Clarification questions indicating missed context
        clarification_patterns = [
            r"I need to (ask|clarify|confirm)",
            r"Could you (clarify|explain|confirm)",
            r"What (do you mean|does this mean)",
            r"I\'m (not sure|unclear|uncertain) (what|how|whether)",
            r"Can you provide more (context|details|information)",
        ]

        for pattern in clarification_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                misses.append((model, agent_run_id, "clarification"))
                break

    return misses


def analyze_miss_distribution(misses: List[Tuple[str, str, str]]) -> Dict[str, Dict[str, int]]:
    """Analyze missed-attention events by model and type.

    Returns: {model: {evidence_type: count}}
    """
    distribution = {}

    for model, _, evidence_type in misses:
        if model not in distribution:
            distribution[model] = {"re_read": 0, "clarification": 0, "total": 0}

        distribution[model][evidence_type] = distribution[model].get(evidence_type, 0) + 1
        distribution[model]["total"] += 1

    return distribution


def adjust_weights(
    current_weights: Dict[str, Dict[str, float]],
    distribution: Dict[str, Dict[str, int]],
    threshold: int = 5
) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
    """Auto-adjust LITM weights based on miss distribution.

    Args:
        current_weights: Current weights per model
        distribution: Miss counts per model
        threshold: Minimum misses to trigger adjustment

    Returns:
        (updated_weights, change_log)
    """
    updated_weights = {}
    change_log = []

    for model, weights in current_weights.items():
        new_weights = weights.copy()
        miss_count = distribution.get(model, {}).get("total", 0)

        if miss_count >= threshold:
            # Increase beta (middle attention) by WEIGHT_NUDGE
            old_beta = new_weights["beta"]
            new_weights["beta"] = min(old_beta + WEIGHT_NUDGE, BETA_MAX)

            if new_weights["beta"] != old_beta:
                change_log.append(
                    f"{model}: beta {old_beta:.2f} → {new_weights['beta']:.2f} "
                    f"({miss_count} middle-attention misses)"
                )

        updated_weights[model] = new_weights

    return updated_weights, change_log


def run_litm_audit(
    db_path: str,
    dispatch_config_path: Path,
    lookback_days: int = 7,
    threshold: int = 5,
    dry_run: bool = False
) -> Dict:
    """Run weekly LITM weight audit and adjustment.

    Returns:
        Report dict with misses, distribution, changes, updated weights
    """
    # Load current weights
    current_weights = load_litm_weights(dispatch_config_path)

    # Detect missed-attention events
    misses = detect_middle_attention_misses(db_path, lookback_days)

    # Analyze distribution
    distribution = analyze_miss_distribution(misses)

    # Adjust weights
    updated_weights, change_log = adjust_weights(current_weights, distribution, threshold)

    # Save if not dry-run
    if not dry_run and change_log:
        save_litm_weights(dispatch_config_path, updated_weights)

    return {
        "timestamp": datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "total_misses": len(misses),
        "distribution": distribution,
        "current_weights": current_weights,
        "updated_weights": updated_weights,
        "changes": change_log,
        "dry_run": dry_run,
    }


def main():
    """CLI entry point for standalone testing."""
    import argparse

    parser = argparse.ArgumentParser(description="LITM Weight Tuner")
    parser.add_argument("--db", default="theforge.db", help="Path to TheForge DB")
    parser.add_argument("--config", default="dispatch_config.json", help="Path to dispatch config")
    parser.add_argument("--days", type=int, default=7, help="Lookback days")
    parser.add_argument("--threshold", type=int, default=5, help="Min misses to adjust")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")

    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    config_path = Path(args.config).resolve()

    if not db_path.exists():
        print(f"Error: Database not found: {db_path}")
        return 1

    report = run_litm_audit(
        str(db_path),
        config_path,
        lookback_days=args.days,
        threshold=args.threshold,
        dry_run=args.dry_run,
    )

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
