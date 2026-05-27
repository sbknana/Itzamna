"""Regression tests for task 2476 — review-agent output artifacts MUST
route to ``.equipa-artifacts/<TYPE>-<TASK_ID>.md`` (relative to the
project root) and NEVER to the project root itself.

Bug recap: every EQUIPA dispatch had been writing ``SECURITY-REVIEW-
<task_id>.md``, ``CODE-REVIEW-<task_id>.md``, ``PLAN-<id>.md``, and
``RETRY-IMPLEMENTATION-<id>.md`` at the *root* of the target repo. Over
hundreds of dispatches this polluted every project EQUIPA ran against
with one-off review files that frequently leaked into commits. Task
2476 funnels them all into ``.equipa-artifacts/`` so they stay out of
the project's tracked surface area (`.gitignore` already excludes the
directory).

These tests assert the contract at three layers:

1. ``review_artifact_relpath()`` / ``review_artifact_path()`` produce
   paths under ``.equipa-artifacts/``.
2. ``ensure_artifacts_dir()`` is idempotent and creates the directory.
3. ``run_security_review`` and ``run_code_review`` build prompt strings
   that name the new path — preventing future drift back to repo-root
   templates (the historical failure mode).

The existing lesson "EQUIPA agents MUST save output files"
(lessons_learned, times_seen >= 5) is preserved: these tests verify the
WHERE has changed, not the WHETHER. The save-output instruction is
still present in every reviewer prompt — see the assertions on the
captured prompt description.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    ensure_artifacts_dir,
    find_review_artifact,
    review_artifact_path,
    review_artifact_relpath,
    run_code_review,
    run_security_review,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_artifacts_dir_name_is_dot_equipa_artifacts() -> None:
    """The constant is the single source of truth — pinning it prevents a
    well-meaning rename from silently breaking every downstream gate."""
    assert ARTIFACTS_DIR_NAME == ".equipa-artifacts"


@pytest.mark.parametrize(
    ("kind", "task_id", "expected"),
    [
        ("SECURITY-REVIEW", 2476, ".equipa-artifacts/SECURITY-REVIEW-2476.md"),
        ("CODE-REVIEW", 1, ".equipa-artifacts/CODE-REVIEW-1.md"),
        ("PLAN", 9999, ".equipa-artifacts/PLAN-9999.md"),
        (
            "RETRY-IMPLEMENTATION",
            42,
            ".equipa-artifacts/RETRY-IMPLEMENTATION-42.md",
        ),
        ("DEPENDENCY-AUDIT", 7, ".equipa-artifacts/DEPENDENCY-AUDIT-7.md"),
    ],
)
def test_review_artifact_relpath_routes_to_artifacts_dir(
    kind: str, task_id: int, expected: str,
) -> None:
    """The repo-relative path string injected into agent prompts must be
    rooted at ``.equipa-artifacts/`` — never at the repo root."""
    result = review_artifact_relpath(kind, task_id)
    assert result == expected
    assert result.startswith(f"{ARTIFACTS_DIR_NAME}/")
    assert "/" in result  # NOT a bare filename


def test_review_artifact_path_resolves_under_project_dir(tmp_path: Path) -> None:
    p = review_artifact_path(tmp_path, "SECURITY-REVIEW", 2476)
    assert p == tmp_path / ".equipa-artifacts" / "SECURITY-REVIEW-2476.md"
    # Must NOT be at repo root.
    assert p.parent.name == ".equipa-artifacts"


def test_ensure_artifacts_dir_is_idempotent(tmp_path: Path) -> None:
    a = ensure_artifacts_dir(tmp_path)
    assert a.is_dir()
    assert a.name == ".equipa-artifacts"
    # Second call must not raise (idempotent).
    b = ensure_artifacts_dir(tmp_path)
    assert b == a


def test_find_review_artifact_prefers_new_path_over_legacy(
    tmp_path: Path,
) -> None:
    """When both legacy (repo-root) and new (.equipa-artifacts/) files
    exist for the same task, the new path wins."""
    legacy = tmp_path / "SECURITY-REVIEW-2476.md"
    legacy.write_text("legacy", encoding="utf-8")
    new = ensure_artifacts_dir(tmp_path) / "SECURITY-REVIEW-2476.md"
    new.write_text("new", encoding="utf-8")

    found = find_review_artifact(tmp_path, "SECURITY-REVIEW", 2476)
    assert found == new
    assert found.read_text(encoding="utf-8") == "new"


def test_find_review_artifact_falls_back_to_legacy_for_inflight(
    tmp_path: Path,
) -> None:
    """During the transition, an artifact already written at the
    repo-root path (by an older agent) must still be discoverable so the
    fail-closed merge gate parses it instead of fail-blocking."""
    legacy = tmp_path / "SECURITY-REVIEW-2476.md"
    legacy.write_text("legacy artifact", encoding="utf-8")
    # No .equipa-artifacts/ file present.
    found = find_review_artifact(tmp_path, "SECURITY-REVIEW", 2476)
    assert found == legacy


def test_find_review_artifact_defaults_to_new_path_when_missing(
    tmp_path: Path,
) -> None:
    """When NEITHER file exists, return the new path so write callers
    land at the right place."""
    found = find_review_artifact(tmp_path, "SECURITY-REVIEW", 9999)
    assert found == tmp_path / ".equipa-artifacts" / "SECURITY-REVIEW-9999.md"


# ---------------------------------------------------------------------------
# Prompt construction — the contract the agent sees
# ---------------------------------------------------------------------------


class _Args:
    dispatch_config = None


@contextmanager
def _fake_build_cli_command(*args: Any, **kwargs: Any):
    yield SimpleNamespace(cmd=["claude"])


async def _fake_run_agent(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"success": True, "result_text": "", "duration": 0.1, "errors": []}


def _capture_prompt_description(fn, task: dict[str, Any], project_dir: str):
    """Run a review function and capture the description passed into the
    system-prompt builder."""
    captured: dict[str, str] = {}

    def fake_build_system_prompt(reviewer_task, *a, **kw):
        captured["description"] = reviewer_task["description"]
        return "prompt"

    with (
        patch("equipa.loops.log", new=lambda *a, **k: None),
        patch("equipa.loops.run_agent", new=_fake_run_agent),
        patch("equipa.loops.load_dispatch_config", return_value={}),
        patch("equipa.loops.get_role_turns", return_value=20),
        patch("equipa.loops.get_role_model", return_value="opus"),
        patch(
            "equipa.loops.build_system_prompt",
            side_effect=fake_build_system_prompt,
        ),
        patch("equipa.loops._extract_security_findings", return_value=[]),
        patch("equipa.loops.build_cli_command", new=_fake_build_cli_command),
    ):
        asyncio.run(
            fn(
                task=task,
                project_dir=project_dir,
                project_context={},
                args=_Args(),
                output=None,
            )
        )
    return captured["description"]


def test_security_reviewer_prompt_targets_equipa_artifacts(tmp_path: Path):
    """Regression: the security-reviewer prompt must inject the
    .equipa-artifacts/ path, NOT a repo-root filename. If a future
    template change drops the prefix, this test fails before the bad
    prompt reaches an agent."""
    desc = _capture_prompt_description(
        run_security_review,
        {"id": 2476, "project_id": 23, "title": "x", "description": "y"},
        str(tmp_path),
    )
    assert ".equipa-artifacts/SECURITY-REVIEW-2476.md" in desc
    # The preserved save-output instruction (lessons_learned >= 5):
    # MUST and "Write your findings" must still appear so the WHETHER
    # is not weakened — task 2476 only changes the WHERE.
    assert "Write your findings" in desc
    # No bare repo-root mention as the destination.
    # We tolerate the substring "SECURITY-REVIEW-2476.md" appearing once
    # inside the .equipa-artifacts/ path token, but no second naked use.
    naked = desc.replace(".equipa-artifacts/SECURITY-REVIEW-2476.md", "")
    assert "SECURITY-REVIEW-2476.md" not in naked, (
        "prompt still names the bare repo-root filename somewhere — "
        "every reference must be prefixed with .equipa-artifacts/"
    )


def test_code_reviewer_prompt_targets_equipa_artifacts(tmp_path: Path):
    """Same contract for the code-reviewer prompt — its CODE-REVIEW-<id>
    file must route into .equipa-artifacts/ too."""
    desc = _capture_prompt_description(
        run_code_review,
        {"id": 2476, "project_id": 23, "title": "x", "description": "y"},
        str(tmp_path),
    )
    assert ".equipa-artifacts/CODE-REVIEW-2476.md" in desc
    assert "Write findings" in desc
    naked = desc.replace(".equipa-artifacts/CODE-REVIEW-2476.md", "")
    assert "CODE-REVIEW-2476.md" not in naked


def test_orchestrator_precreates_artifacts_dir_on_security_review(
    tmp_path: Path,
):
    """Acceptance criterion 3: the orchestrator must mkdir -p the
    artifacts dir before the reviewer agent runs, so the agent's Write
    call cannot fail with ENOENT."""
    _capture_prompt_description(
        run_security_review,
        {"id": 2476, "project_id": 23, "title": "x", "description": "y"},
        str(tmp_path),
    )
    assert (tmp_path / ".equipa-artifacts").is_dir()


def test_orchestrator_precreates_artifacts_dir_on_code_review(
    tmp_path: Path,
):
    _capture_prompt_description(
        run_code_review,
        {"id": 2476, "project_id": 23, "title": "x", "description": "y"},
        str(tmp_path),
    )
    assert (tmp_path / ".equipa-artifacts").is_dir()


# ---------------------------------------------------------------------------
# Static-text guard against template drift in the shipped prompt files
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "prompt_relpath",
    [
        "prompts/security-reviewer.md",
        "prompts/code-reviewer.md",
        "standing_orders/security-reviewer.md",
    ],
)
def test_shipped_prompts_reference_artifacts_dir(prompt_relpath: str) -> None:
    """The shipped prompt templates must mention .equipa-artifacts/ —
    this catches a future PR that reverts the prefix in the markdown
    file even if loops.py is unchanged."""
    body = (REPO_ROOT / prompt_relpath).read_text(encoding="utf-8")
    assert ".equipa-artifacts/" in body, (
        f"{prompt_relpath} dropped the .equipa-artifacts/ prefix"
    )
