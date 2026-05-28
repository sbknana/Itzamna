#!/usr/bin/env python3
"""Parameterized open_question insert for the Athena truth-sync runner.

This script replaces the inline bash ``sqlite3`` CLI invocation that
shell-interpolated a user-controlled ``$summary`` string into the SQL
text (per SECURITY-REVIEW-2480 S4 MEDIUM, addressed in task #2483).

Why a separate script:
  * ``sqlite3.connect`` + parameter placeholders (``?``) provide
    sqlite-native escaping; no quoting bugs, no SQL injection surface.
  * Keeps the bash runner free of inline Python code-strings, which
    were the other half of the S4 / S2 findings.
  * Testable in isolation -- unit tests can drive the script with a
    temporary DB without standing up the full cron pipeline.

Usage::

    scripts/athena_open_question.py \
        --db /path/to/theforge.db \
        --project-id 23 \
        --question "Athena truth-sync repaired drift" \
        --context "<diff stat / drift output, free-form text>"

Exit codes:
  0 -- insert succeeded (or skipped because table is missing).
  1 -- insert failed (DB not present, write error, sqlite error).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


INSERT_SQL = (
    "INSERT INTO open_questions (project_id, question, context, resolved) "
    "VALUES (?, ?, ?, 0)"
)


def insert_open_question(
    db_path: Path,
    *,
    project_id: int,
    question: str,
    context: str,
) -> int:
    """Insert a single open_question row. Returns 0 on success, 1 on error."""

    if not db_path.is_file():
        print(f"error: TheForge DB not found at {db_path}", file=sys.stderr)
        return 1

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                INSERT_SQL,
                (project_id, question, context),
            )
            conn.commit()
    except sqlite3.Error as err:
        print(f"error: sqlite insert failed: {err}", file=sys.stderr)
        return 1
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Insert a parameterized open_questions row into TheForge. "
            "Used by scripts/athena_truth_sync.sh to record drift "
            "repairs or persistent drift for human review."
        )
    )
    parser.add_argument("--db", type=Path, required=True, help="TheForge SQLite DB path.")
    parser.add_argument(
        "--project-id",
        type=int,
        required=True,
        help="TheForge project_id (23 for Equipa).",
    )
    parser.add_argument(
        "--question",
        type=str,
        required=True,
        help="Short summary stored in the `question` column.",
    )
    parser.add_argument(
        "--context",
        type=str,
        default="",
        help="Free-form context body stored in the `context` column.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return insert_open_question(
        args.db,
        project_id=args.project_id,
        question=args.question,
        context=args.context,
    )


if __name__ == "__main__":
    sys.exit(main())
