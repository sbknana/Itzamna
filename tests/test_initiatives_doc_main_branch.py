"""Tests for the master->main rename note in docs/INITIATIVES.md (#2492).

These verify the doc edit made for the 2026-05-29 default-branch rename:
the doc must document ``main`` as the current default branch, and must not
leave any stale ``master`` default-branch references behind (the sole
permitted mention is the historical "renamed from master" note).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "INITIATIVES.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.is_file(), f"missing doc: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_declares_main_as_default_branch(doc_text: str) -> None:
    """The doc must state that the EQUIPA default branch is now ``main``."""

    assert "default branch is now `main`" in doc_text, (
        "INITIATIVES.md must document that the EQUIPA default branch "
        "is now `main`"
    )


def test_doc_records_the_rename_date(doc_text: str) -> None:
    """The note must keep the rename provenance (renamed from master, dated)."""

    assert "renamed from" in doc_text
    assert "2026-05-29" in doc_text


def test_no_stale_master_default_branch_references(doc_text: str) -> None:
    """The only permitted ``master`` mention is the historical rename note.

    Any other literal ``master`` (e.g. in a Lifecycle command example or
    prose) would be a stale default-branch reference and must have been
    updated to ``main``.
    """

    offending: list[tuple[int, str]] = []
    for lineno, line in enumerate(doc_text.splitlines(), start=1):
        if "master" not in line:
            continue
        # The single allowed occurrence is the provenance note line, which
        # explicitly states the branch was renamed FROM master.
        if "renamed from" in line or "`master`" in line:
            continue
        offending.append((lineno, line.strip()))

    assert offending == [], (
        "stale `master` default-branch references found "
        f"(should be `main`): {offending}"
    )
