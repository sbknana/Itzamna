#!/usr/bin/env python3
"""ForgeSmith Impact Analysis — Blast-radius assessment for prompt mutations.

Before ForgeSmith applies a prompt or config change, this module assesses:
- Which roles are affected
- Which task types are affected
- Estimated risk level (LOW / MEDIUM / HIGH)

HIGH-risk changes are blocked from auto-apply and require manual approval.

Pipeline position: called from apply_changes(), apply_opro_proposals(),
and store_evolved_prompt() before any mutation is applied.

Copyright 2026, Forgeborn
"""

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# --- Paths ---

SCRIPT_DIR = Path(__file__).resolve().parent
THEFORGE_DB = os.environ.get(
    "THEFORGE_DB",
    str(Path(__file__).resolve().parent.parent / "theforge.db"),
)
PROMPTS_DIR = SCRIPT_DIR / "prompts"

# --- Risk thresholds ---

# Number of recent runs for a role above which changes are considered
# high-blast-radius (many active tasks depend on this prompt)
HIGH_BLAST_RADIUS_THRESHOLD = 20

# Protected roles where prompt mutations carry inherent risk
HIGH_RISK_ROLES = frozenset({
    "developer",  # Most-used role, highest impact surface
})

# Change types that are inherently higher risk
HIGH_RISK_CHANGE_TYPES = frozenset({
    "gepa_evolution",  # Full prompt rewrite via LLM
    "opro_applied",    # LLM-proposed structural change
})

# Config keys that affect all roles (not role-specific)
GLOBAL_CONFIG_KEYS = frozenset({
    "max_concurrent", "model", "max_turns", "provider",
    "ollama_base_url", "ollama_model",
})


# --- DB Helpers ---

def get_db(write=False):
    """Connect to TheForge SQLite database."""
    db_path = os.environ.get("THEFORGE_DB", THEFORGE_DB)
    if write:
        conn = sqlite3.connect(db_path)
    else:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def log(msg):
    """Print timestamped message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [IMPACT] {msg}")


# ============================================================
# STEP 1: Determine affected roles
# ============================================================

def identify_affected_roles(change_type, target_file, old_value, new_value):
    """Determine which roles are affected by a proposed change.

    Returns a list of role names that would be impacted.
    """
    affected = set()

    if change_type == "config_tune":
        # Config changes target specific keys like "max_turns_developer"
        # or global keys like "max_concurrent"
        config_key = _extract_config_key(old_value, new_value, target_file)
        if config_key:
            role = _config_key_to_role(config_key)
            if role:
                affected.add(role)
            elif config_key in GLOBAL_CONFIG_KEYS:
                # Global config affects all roles
                affected.update(_get_all_active_roles())

    elif change_type in ("prompt_patch", "opro_applied", "opro_proposal"):
        # Prompt changes affect the role whose file is being modified
        role = _target_file_to_role(target_file)
        if role:
            affected.add(role)

    elif change_type == "gepa_evolution":
        # GEPA evolves a specific role's prompt
        role = _target_file_to_role(target_file)
        if role:
            affected.add(role)

    elif change_type == "blocked_resolution":
        # Blocked task resolutions don't affect role prompts directly
        pass

    return sorted(affected)


def _extract_config_key(old_value, new_value, target_file):
    """Try to determine the config key being changed.

    For config_tune changes, the key is often embedded in the rationale
    or derivable from old/new values. We check the dispatch config file
    to find which key matches.
    """
    config_path = Path(target_file) if target_file else None
    if not config_path or not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # Find keys where the current value matches old or new value
    for key, val in config.items():
        if str(val) == str(new_value):
            return key

    return None


def _config_key_to_role(config_key):
    """Extract role name from a config key like 'max_turns_developer'."""
    prefixes = ("max_turns_", "model_")
    for prefix in prefixes:
        if config_key.startswith(prefix):
            role_part = config_key[len(prefix):]
            # Restore hyphenated names (e.g., "security_reviewer" -> "security-reviewer")
            return role_part.replace("_", "-")
    return None


def _target_file_to_role(target_file):
    """Extract role name from a prompt file path like 'prompts/developer.md'."""
    if not target_file:
        return None
    path = Path(target_file)
    name = path.stem
    # Strip version suffixes (e.g., "developer_v2" -> "developer")
    name = re.sub(r"_v\d+$", "", name)
    return name if name and name != "_common" else None


def _get_all_active_roles():
    """Get all roles that have had recent agent runs."""
    conn = get_db()
    rows = conn.execute(
        """SELECT DISTINCT role FROM agent_runs
           WHERE started_at >= datetime('now', '-30 days')"""
    ).fetchall()
    conn.close()
    return [r["role"] for r in rows]


# ============================================================
# STEP 2: Determine affected task types
# ============================================================

def identify_affected_task_types(affected_roles):
    """Determine which task types are handled by the affected roles.

    Returns a list of task type strings.
    """
    if not affected_roles:
        return []

    conn = get_db()
    placeholders = ",".join("?" for _ in affected_roles)
    rows = conn.execute(
        f"""SELECT DISTINCT task_type FROM agent_episodes
            WHERE role IN ({placeholders})
              AND created_at >= datetime('now', '-30 days')
              AND task_type IS NOT NULL AND task_type != ''""",
        affected_roles,
    ).fetchall()
    conn.close()

    return sorted(set(r["task_type"] for r in rows))


# ============================================================
# STEP 3: Compute blast radius metrics
# ============================================================

def compute_blast_radius(affected_roles):
    """Compute blast radius metrics for the affected roles.

    Returns dict with:
    - recent_run_count: how many agent runs used these roles recently
    - active_task_count: how many tasks are currently in_progress for these roles
    - project_count: how many distinct projects are affected
    """
    if not affected_roles:
        return {"recent_run_count": 0, "active_task_count": 0, "project_count": 0}

    conn = get_db()
    placeholders = ",".join("?" for _ in affected_roles)

    # Recent runs using these roles
    run_row = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM agent_runs
            WHERE role IN ({placeholders})
              AND started_at >= datetime('now', '-7 days')""",
        affected_roles,
    ).fetchone()
    recent_run_count = run_row["cnt"] if run_row else 0

    # Active (in_progress) tasks for these roles
    # We approximate by counting tasks dispatched with these roles recently
    active_row = conn.execute(
        f"""SELECT COUNT(DISTINCT task_id) as cnt FROM agent_runs
            WHERE role IN ({placeholders})
              AND started_at >= datetime('now', '-1 day')""",
        affected_roles,
    ).fetchone()
    active_task_count = active_row["cnt"] if active_row else 0

    # Distinct projects affected
    project_row = conn.execute(
        f"""SELECT COUNT(DISTINCT project_id) as cnt FROM agent_runs
            WHERE role IN ({placeholders})
              AND started_at >= datetime('now', '-7 days')
              AND project_id IS NOT NULL""",
        affected_roles,
    ).fetchone()
    project_count = project_row["cnt"] if project_row else 0

    conn.close()

    return {
        "recent_run_count": recent_run_count,
        "active_task_count": active_task_count,
        "project_count": project_count,
    }


# ============================================================
# STEP 4: Assess risk level
# ============================================================

def assess_risk_level(change_type, affected_roles, blast_radius, diff_ratio=None):
    """Assess the risk level of a proposed change.

    Risk factors:
    - HIGH: affects HIGH_RISK_ROLES + high blast radius
    - HIGH: GEPA/OPRO structural changes to high-traffic roles
    - HIGH: global config changes
    - MEDIUM: affects multiple roles or moderate blast radius
    - LOW: single role with low blast radius

    Returns ("LOW" | "MEDIUM" | "HIGH", list_of_risk_factors).
    """
    risk_factors = []
    risk_score = 0  # Accumulate risk points

    # Factor 1: Change type inherent risk
    if change_type in HIGH_RISK_CHANGE_TYPES:
        risk_factors.append(f"change_type '{change_type}' is inherently higher risk")
        risk_score += 2

    # Factor 2: High-risk roles affected
    high_risk_affected = set(affected_roles) & HIGH_RISK_ROLES
    if high_risk_affected:
        risk_factors.append(
            f"affects high-risk role(s): {', '.join(sorted(high_risk_affected))}"
        )
        risk_score += 2

    # Factor 3: Multiple roles affected (global change)
    if len(affected_roles) > 2:
        risk_factors.append(f"affects {len(affected_roles)} roles (broad impact)")
        risk_score += 2

    # Factor 4: High blast radius (many recent runs)
    if blast_radius["recent_run_count"] >= HIGH_BLAST_RADIUS_THRESHOLD:
        risk_factors.append(
            f"high blast radius: {blast_radius['recent_run_count']} runs in last 7 days"
        )
        risk_score += 1

    # Factor 5: Multiple projects affected
    if blast_radius["project_count"] > 2:
        risk_factors.append(
            f"affects {blast_radius['project_count']} projects"
        )
        risk_score += 1

    # Factor 6: Large diff ratio (for GEPA evolutions)
    if diff_ratio is not None and diff_ratio > 0.15:
        risk_factors.append(
            f"large prompt diff ratio: {diff_ratio:.1%}"
        )
        risk_score += 1

    # Factor 7: Active tasks could be disrupted
    if blast_radius["active_task_count"] > 3:
        risk_factors.append(
            f"{blast_radius['active_task_count']} tasks dispatched in last 24h"
        )
        risk_score += 1

    # Determine risk level from accumulated score
    if risk_score >= 4:
        risk_level = "HIGH"
    elif risk_score >= 2:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return risk_level, risk_factors


# ============================================================
# MAIN ENTRY POINT: run_impact_analysis
# ============================================================

def run_impact_analysis(change_type, target_file, old_value, new_value,
                        rationale="", diff_ratio=None):
    """Run full change-impact analysis for a proposed mutation.

    Returns a dict with:
    - risk_level: "LOW" | "MEDIUM" | "HIGH"
    - affected_roles: list of role names
    - affected_task_types: list of task type strings
    - blast_radius: dict with run/task/project counts
    - risk_factors: list of human-readable risk factor strings
    - blocked: True if HIGH risk (requires manual approval)
    - assessment_summary: one-line summary for logging

    This is the primary function called from forgesmith.py before
    applying any change.
    """
    # Step 1: Identify affected roles
    affected_roles = identify_affected_roles(
        change_type, target_file, old_value, new_value
    )

    # Step 2: Identify affected task types
    affected_task_types = identify_affected_task_types(affected_roles)

    # Step 3: Compute blast radius
    blast_radius = compute_blast_radius(affected_roles)

    # Step 4: Assess risk
    risk_level, risk_factors = assess_risk_level(
        change_type, affected_roles, blast_radius, diff_ratio=diff_ratio
    )

    # Determine if this change should be blocked
    blocked = risk_level == "HIGH"

    # Build summary
    roles_str = ", ".join(affected_roles) if affected_roles else "none"
    types_str = ", ".join(affected_task_types) if affected_task_types else "none"
    assessment_summary = (
        f"[{risk_level}] {change_type} affects roles=[{roles_str}], "
        f"task_types=[{types_str}], "
        f"blast_radius={blast_radius['recent_run_count']} runs/7d"
    )

    if blocked:
        assessment_summary += " — BLOCKED (requires manual approval)"

    assessment = {
        "risk_level": risk_level,
        "affected_roles": affected_roles,
        "affected_task_types": affected_task_types,
        "blast_radius": blast_radius,
        "risk_factors": risk_factors,
        "blocked": blocked,
        "assessment_summary": assessment_summary,
        "analyzed_at": datetime.now().isoformat(),
    }

    log(assessment_summary)
    if risk_factors:
        for factor in risk_factors:
            log(f"  - {factor}")

    return assessment


def log_impact_assessment(change_id, assessment):
    """Store the impact assessment in the forgesmith_changes table.

    Updates the impact_assessment column for the given change record.
    """
    conn = get_db(write=True)
    conn.execute(
        """UPDATE forgesmith_changes
           SET impact_assessment = ?
           WHERE id = ?""",
        (json.dumps(assessment, default=str), change_id),
    )
    conn.commit()
    conn.close()


def ensure_impact_assessment_column():
    """Add impact_assessment column to forgesmith_changes if missing.

    Safety net — column is added by migration 4 (db_migrate.py).
    This provides defense-in-depth for standalone ForgeSmith runs.
    """
    conn = get_db(write=True)
    try:
        conn.execute(
            "SELECT impact_assessment FROM forgesmith_changes LIMIT 1"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE forgesmith_changes "
            "ADD COLUMN impact_assessment TEXT DEFAULT NULL"
        )
        conn.commit()
        log("Added impact_assessment column to forgesmith_changes table")
    finally:
        conn.close()
