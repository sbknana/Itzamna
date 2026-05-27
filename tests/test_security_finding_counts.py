"""Regression tests for SECURITY-REVIEW finding-count extraction (task 2315).

Bug: the orchestrator security-review section header used to count
CRITICAL/HIGH severity by substring-scanning raw agent stdout. That counted
severity mentions in prose ("[S1] LOW — this is NOT a CRITICAL because…") as
actual findings, producing false-alarm headers that did not match the final
SECURITY-REVIEW-NNNN.md artifact.

Fix: parse the structured finding headers in the saved
SECURITY-REVIEW-NNNN.md artifact instead. If the artifact does not exist,
emit a distinct "missing artifact" warning rather than counterfeit counts.

These tests guard:
  - Counts from the artifact match the real finding headers
  - Raw-stdout substring counts of CRITICAL/HIGH are NOT used
  - Missing artifact => no fake counts, missing-artifact warning instead
  - Zero-findings (approve) review reports zero, not a stdout-derived guess
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from equipa.loops import _count_findings_in_review_file, run_security_review


# ---------- Direct parser unit tests ----------


def _write(tmp_path: Path, task_id: int, body: str) -> Path:
    path = tmp_path / f"SECURITY-REVIEW-{task_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_parser_counts_findings_by_severity_header(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        2315,
        "# Security Review\n"
        "## Findings\n"
        "### [S1] CRITICAL — SQL injection in login handler\n"
        "Body text mentioning the word HIGH and MEDIUM in prose.\n"
        "### [S2] HIGH — Path traversal in upload endpoint\n"
        "### [S3] HIGH — Open redirect on /go\n"
        "### [S4] MEDIUM — Missing CSRF\n"
        "### [S5] LOW — Verbose error responses\n"
        "### [S6] INFO — Logging note\n",
    )
    assert _count_findings_in_review_file(path) == {
        "CRITICAL": 1,
        "HIGH": 2,
        "MEDIUM": 1,
        "LOW": 1,
        "INFO": 1,
    }


def test_parser_ignores_prose_mentions_of_severities(tmp_path: Path) -> None:
    # Stdout-style content: CRITICAL appears 5 times but only in non-finding
    # contexts (rejected findings, narrative prose). The artifact's structured
    # finding headers list 0 CRITICAL, 0 HIGH.
    path = _write(
        tmp_path,
        2315,
        "# Security Review\n"
        "Summary: 0 CRITICAL, 0 HIGH, 1 MEDIUM, 0 LOW, 0 INFO.\n"
        "## Discussion\n"
        "I considered marking [X] as CRITICAL but it is NOT CRITICAL.\n"
        "Another finding could be HIGH but I downgraded to MEDIUM.\n"
        "Reviewer note: nothing here rises to CRITICAL or HIGH severity.\n"
        "## Findings\n"
        "### [S1] MEDIUM — Race condition in token issuance\n"
        "This is HIGH risk on multi-tenant deployments, downgraded to MEDIUM.\n",
    )
    counts = _count_findings_in_review_file(path)
    assert counts == {
        "CRITICAL": 0,
        "HIGH": 0,
        "MEDIUM": 1,
        "LOW": 0,
        "INFO": 0,
    }


def test_parser_returns_none_when_artifact_missing(tmp_path: Path) -> None:
    assert _count_findings_in_review_file(tmp_path / "SECURITY-REVIEW-9999.md") is None


def test_parser_handles_zero_findings_approve_review(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        2310,
        "# Security Review\n"
        "**APPROVE.**\n"
        "No CRITICAL, HIGH, MEDIUM, or LOW findings.\n"
        "## Findings\n"
        "### [S1] INFO — Defense-in-depth suggestion\n"
        "### [S2] INFO — Documentation nit\n"
        "### [S3] INFO — Test coverage observation\n",
    )
    assert _count_findings_in_review_file(path) == {
        "CRITICAL": 0,
        "HIGH": 0,
        "MEDIUM": 0,
        "LOW": 0,
        "INFO": 3,
    }


def test_parser_accepts_en_dash_and_hyphen_separators(tmp_path: Path) -> None:
    # Real artifacts have shown em-dash (—), en-dash (–), and hyphen (-).
    # The parser anchors on the bracketed tag + severity, so the separator
    # character does not matter — verify that.
    path = _write(
        tmp_path,
        2315,
        "### [A1] CRITICAL — em-dash separator\n"
        "### [A2] HIGH – en-dash separator\n"
        "### [A3] HIGH - plain hyphen separator\n",
    )
    counts = _count_findings_in_review_file(path)
    assert counts["CRITICAL"] == 1
    assert counts["HIGH"] == 2


# ---------- Integration tests against run_security_review ----------


class _StubLog:
    """Captures `log(msg, output)` calls so we can assert on what was emitted."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str, output: Any = None) -> None:
        self.lines.append(msg)


def _run_security_review(
    task: dict[str, Any],
    project_dir: Path,
    *,
    result_text: str = "",
    success: bool = True,
) -> list[str]:
    """Invoke run_security_review with all external deps stubbed."""
    stub_log = _StubLog()

    async def fake_run_agent(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": success,
            "result_text": result_text,
            "duration": 0.1,
            "errors": [],
        }

    class _Args:
        dispatch_config = None

    with (
        patch("equipa.loops.log", new=stub_log),
        patch("equipa.loops.run_agent", new=fake_run_agent),
        patch("equipa.loops.load_dispatch_config", return_value={}),
        patch("equipa.loops.get_role_turns", return_value=20),
        patch("equipa.loops.get_role_model", return_value="opus"),
        patch("equipa.loops.build_system_prompt", return_value="prompt"),
        patch("equipa.loops._extract_security_findings", return_value=[]),
    ):
        # build_cli_command is a context manager that needs to yield something
        # with a `.cmd` attribute; the actual content does not matter here.
        from contextlib import contextmanager
        from types import SimpleNamespace

        @contextmanager
        def fake_build_cli_command(*args: Any, **kwargs: Any):
            yield SimpleNamespace(cmd=["claude"])

        with patch("equipa.loops.build_cli_command", new=fake_build_cli_command):
            asyncio.run(
                run_security_review(
                    task=task,
                    project_dir=str(project_dir),
                    project_context={},
                    args=_Args(),
                    output=None,
                )
            )
    return stub_log.lines


def test_count_matches_review_file_when_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        4242,
        "### [S1] CRITICAL — real finding\n"
        "### [S2] CRITICAL — real finding\n"
        "### [S3] HIGH — real finding\n"
        "### [S4] HIGH — real finding\n"
        "### [S5] HIGH — real finding\n",
    )
    lines = _run_security_review(
        {"id": 4242, "project_id": 1, "title": "x", "description": "y"},
        tmp_path,
        result_text="agent stdout text",
    )
    warnings = [ln for ln in lines if "WARNING: Found" in ln]
    assert warnings == [
        "  WARNING: Found 2 CRITICAL and 3 HIGH severity findings",
    ]


def test_count_extraction_from_stdout_is_NOT_used(tmp_path: Path) -> None:
    # Artifact: 0 CRITICAL / 0 HIGH (only LOW + INFO).
    # Stdout: contains the words CRITICAL and HIGH many times in non-finding
    # contexts. The old code counted these (and reported a fake 5+/5+ header);
    # the new code must report 0/0 based on the artifact.
    _write(
        tmp_path,
        4243,
        "### [S1] LOW — minor finding\n"
        "### [S2] INFO — observation\n",
    )
    noisy_stdout = (
        "I considered CRITICAL but downgraded. "
        "Another CRITICAL? No, NOT CRITICAL. "
        "This could be HIGH but is HIGH-ish at most, MEDIUM realistically. "
        "Headers like CRITICAL: and HIGH: appear here too. "
        "Final call: 0 CRITICAL, 0 HIGH."
    )
    # Sanity: stdout DOES contain CRITICAL/HIGH multiple times.
    assert noisy_stdout.lower().count("critical") >= 4
    assert noisy_stdout.lower().count("high") >= 3

    lines = _run_security_review(
        {"id": 4243, "project_id": 1, "title": "x", "description": "y"},
        tmp_path,
        result_text=noisy_stdout,
    )
    # No "WARNING: Found N CRITICAL and M HIGH" line should appear because
    # the artifact has 0 of each. Instead we should see the zero-findings
    # confirmation line.
    fake_count_lines = [ln for ln in lines if "WARNING: Found" in ln]
    assert fake_count_lines == [], f"unexpected fake count line: {fake_count_lines}"
    assert any("No critical or high severity findings" in ln for ln in lines)


def test_missing_review_file_emits_warning(tmp_path: Path) -> None:
    # No SECURITY-REVIEW-NNNN.md written — agent failed to save the artifact.
    lines = _run_security_review(
        {"id": 4244, "project_id": 1, "title": "x", "description": "y"},
        tmp_path,
        result_text="agent ran but did not save the artifact",
    )
    artifact_warnings = [
        ln for ln in lines if "artifact missing" in ln
    ]
    assert len(artifact_warnings) == 1, f"expected one missing-artifact warning, got {lines}"
    assert "SECURITY-REVIEW-4244.md" in artifact_warnings[0]
    # Crucially: no counterfeit "Found N CRITICAL and M HIGH" line.
    assert not any("WARNING: Found" in ln for ln in lines)


def test_missing_review_file_writes_fallback_dump(tmp_path: Path) -> None:
    """Task 2412: when the agent fails to write the structured artifact, the
    orchestrator preserves the raw result_text on disk so findings are NOT
    lost (the historical bug 2412 dropped them silently)."""
    from equipa.loops import SECURITY_REVIEW_FALLBACK_MARKER

    raw_text = (
        "I found a SQL injection in login() that would let an attacker bypass auth. "
        "Severity: CRITICAL. File: auth.py:42."
    )
    lines = _run_security_review(
        {"id": 4246, "project_id": 1, "title": "x", "description": "y"},
        tmp_path,
        result_text=raw_text,
    )
    # Task 2476: fallback dumps now land under .equipa-artifacts/.
    review_path = tmp_path / ".equipa-artifacts" / "SECURITY-REVIEW-4246.md"
    assert review_path.exists(), (
        f"expected orchestrator to write fallback file {review_path}; "
        f"log lines: {lines}"
    )
    body = review_path.read_text(encoding="utf-8")
    # Fallback marker is present so this file is NOT mistaken for a structured artifact.
    assert SECURITY_REVIEW_FALLBACK_MARKER in body
    # The raw agent output is preserved verbatim.
    assert raw_text in body
    # Log line confirms the save.
    assert any("fallback dump" in ln for ln in lines)


def test_fallback_dump_does_not_satisfy_merge_gate(tmp_path: Path) -> None:
    """The fallback dump must NOT be treated as a structured artifact, so the
    fail-closed merge gate (dispatch._security_review_blocks_merge) still
    fires. Verified at the parser level: _count_findings_in_review_file
    returns None for a fallback file even though the file exists."""
    from equipa.loops import (
        SECURITY_REVIEW_FALLBACK_MARKER,
        _count_findings_in_review_file,
    )

    fallback = tmp_path / "SECURITY-REVIEW-4247.md"
    fallback.write_text(
        "# SECURITY-REVIEW fallback (orchestrator-saved, task 4247)\n"
        f"{SECURITY_REVIEW_FALLBACK_MARKER}\n\n"
        # Even with a structured-looking finding header in the dump, the marker
        # must dominate — fallback dumps capture untrusted raw stdout and must
        # never count as a real artifact.
        "### [F1] CRITICAL — this looks like a finding but it's only raw stdout\n",
        encoding="utf-8",
    )
    assert _count_findings_in_review_file(fallback) is None


def test_fallback_does_not_overwrite_existing_artifact(tmp_path: Path) -> None:
    """If the agent wrote a real artifact after our read, never clobber it."""
    from equipa.loops import _write_security_review_fallback

    review_path = tmp_path / "SECURITY-REVIEW-4248.md"
    original = "### [S1] HIGH — real finding\n"
    review_path.write_text(original, encoding="utf-8")
    _write_security_review_fallback(review_path, 4248, "raw text", output=None)
    assert review_path.read_text(encoding="utf-8") == original


def test_security_review_prompt_uses_task_scoped_filename() -> None:
    """Task 2412: the reviewer prompt MUST name the exact task-scoped
    filename (SECURITY-REVIEW-{task_id}.md). Historical bug: instruction said
    a generic 'SECURITY-REVIEW.md' so the agent wrote the wrong filename and
    the orchestrator dropped the findings."""
    from unittest.mock import MagicMock, patch

    captured: dict[str, Any] = {}

    async def fake_run_agent(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"success": True, "result_text": "", "duration": 0.1, "errors": []}

    def fake_build_system_prompt(security_task, *a, **kw):
        # Capture the description passed to the prompt builder so we can
        # assert the filename is embedded correctly.
        captured["description"] = security_task["description"]
        return "prompt"

    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def fake_build_cli_command(*args: Any, **kwargs: Any):
        yield SimpleNamespace(cmd=["claude"])

    class _Args:
        dispatch_config = None

    with (
        patch("equipa.loops.log", new=lambda *a, **k: None),
        patch("equipa.loops.run_agent", new=fake_run_agent),
        patch("equipa.loops.load_dispatch_config", return_value={}),
        patch("equipa.loops.get_role_turns", return_value=20),
        patch("equipa.loops.get_role_model", return_value="opus"),
        patch("equipa.loops.build_system_prompt", side_effect=fake_build_system_prompt),
        patch("equipa.loops._extract_security_findings", return_value=[]),
        patch("equipa.loops.build_cli_command", new=fake_build_cli_command),
    ):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(
                run_security_review(
                    task={"id": 2412, "project_id": 23, "title": "x", "description": "y"},
                    project_dir=td,
                    project_context={},
                    args=_Args(),
                    output=None,
                )
            )
    desc = captured["description"]
    assert "SECURITY-REVIEW-2412.md" in desc, (
        f"expected task-scoped filename in prompt; got: {desc!r}"
    )
    # Task 2476: the prompt must point at .equipa-artifacts/, NOT repo root.
    assert ".equipa-artifacts/SECURITY-REVIEW-2412.md" in desc, (
        f"expected .equipa-artifacts/ path in prompt; got: {desc!r}"
    )
    # The bug-2412 mistake (generic filename) MUST not be the only instruction.
    # The exact-path language must appear.
    assert (
        "EXACT path" in desc
        or "exact path" in desc
        or "EXACT filename" in desc
        or "exact filename" in desc
    )


def test_zero_findings_reports_zero(tmp_path: Path) -> None:
    _write(
        tmp_path,
        4245,
        "# Security Review\n"
        "**APPROVE.** 0 CRITICAL, 0 HIGH, 0 MEDIUM, 0 LOW, 0 INFO.\n"
        "## Findings\n"
        "_No findings._\n",
    )
    lines = _run_security_review(
        {"id": 4245, "project_id": 1, "title": "x", "description": "y"},
        tmp_path,
        result_text="agent stdout",
    )
    assert any("No critical or high severity findings" in ln for ln in lines)
    assert not any("WARNING: Found" in ln for ln in lines)
    assert not any("artifact missing" in ln for ln in lines)
