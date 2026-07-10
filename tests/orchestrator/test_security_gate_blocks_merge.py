"""Regression fixtures for task #2451 unified merge gate.

Covers the four gate-related invariants the orchestrator is required to
uphold across both single-task (``cli._gated_post_merge``) and parallel
(``dispatch._gated_merge_task``) dispatch modes:

  1. CRITICAL/HIGH findings block the merge.
  2. Missing / unparseable / fallback-marker artifacts fail-closed (the
     defensive invariant in ``_merge_task_branch``).
  3. ``review_blocks_merge=False`` does NOT short-circuit the artifact
     re-read — a HIGH on disk still blocks even if the caller asserts
     "do not block".
  4. Loosened header regex still detects findings written in non-canonical
     formats (no brackets, bold severity).

Subprocess calls pass ``timeout=10`` and isolate from the developer's
global git config (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``) so the
tests run hermetically (task #2451 GATE-08).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest


def _isolated_env() -> dict[str, str]:
    """Return os.environ overlay isolating git from developer config."""
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=_isolated_env(),
    )
    return result.stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd)


@pytest.fixture
def gated_repo(tmp_path: Path) -> dict:
    """Create a repo with master + forge-task-9999 and a SECURITY-REVIEW-9999.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@forgeborn.local")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    master_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "forge-task-9999")
    (repo / "feature.py").write_text("# feature\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feat: feature")
    _git(repo, "checkout", "-q", "master")
    return {"repo": repo, "master_head": master_head}


def _write_review(repo: Path, task_id: int, severity: str) -> None:
    review = repo / f"SECURITY-REVIEW-{task_id}.md"
    review.write_text(
        f"""# Security Review

## Findings

### [F-01] {severity} — Example finding
Some issue.

## Summary
Total: 1 finding ({severity}: 1)
"""
    )


def test_single_task_path_blocks_high(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head
    branches = _git(repo, "branch", "--list", "forge-task-9999")
    assert "forge-task-9999" in branches


def test_parallel_path_blocks_critical(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "CRITICAL")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_defensive_invariant_raises(gated_repo, monkeypatch):
    from equipa import dispatch
    from equipa.security_gate import SecurityGateBypassError

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    with pytest.raises(SecurityGateBypassError):
        asyncio.run(
            dispatch._merge_task_branch(str(repo), 9999, "forge-task-9999")
        )

    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_happy_path_merges_when_clean(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    # Clean review — explicit zero-counts footer (Phase D parser path).
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        "# Security Review\n\nNo blocking findings.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "merged"
    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head != master_head


def test_outcome_not_eligible_skipped(gated_repo, monkeypatch):
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_failed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "skipped"
    assert _git(repo, "rev-parse", "HEAD") == master_head


# ---- Task #2451 Phase F: new fixtures ----


def test_missing_artifact_fails_closed(gated_repo, monkeypatch):
    """GATE-01: defensive invariant must raise when artifact is missing."""
    from equipa import dispatch
    from equipa.security_gate import SecurityGateBypassError

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    # Artifact intentionally NOT written.
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    with pytest.raises(SecurityGateBypassError):
        asyncio.run(
            dispatch._merge_task_branch(str(repo), 9999, "forge-task-9999")
        )

    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_fallback_marker_fails_closed(gated_repo, monkeypatch):
    """GATE-01 + GATE-10: fallback-marker dump must block the merge."""
    from equipa import dispatch
    from equipa.security_gate import SecurityGateBypassError
    from equipa.loops import SECURITY_REVIEW_FALLBACK_MARKER

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        f"# SECURITY-REVIEW fallback dump\n{SECURITY_REVIEW_FALLBACK_MARKER}\n"
        "Reviewer crashed; raw output preserved.\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    with pytest.raises(SecurityGateBypassError):
        asyncio.run(
            dispatch._merge_task_branch(str(repo), 9999, "forge-task-9999")
        )

    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_single_task_path_rereads_artifact_and_blocks_high(
    gated_repo, monkeypatch,
):
    """GATE-02 (task #2706): the single-task path has NO caller flag left.

    Pre-#2706 this test passed ``review_blocks_merge=False`` to prove the
    caller could not short-circuit the on-disk re-check. That flag is now
    removed entirely: ``_gated_post_merge`` forwards only repo/branch/
    outcome/task_id, and the unified gate computes its own GateDecision from
    the real diff (feature.py -> not doc-only) + the on-disk HIGH artifact,
    and blocks. There is no argument a caller could set to bypass it.
    """
    from equipa import cli

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    _write_review(repo, 9999, "HIGH")
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(cli._gated_post_merge(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_loosened_parser_detects_non_canonical_header(gated_repo, monkeypatch):
    """GATE-04: malformed header `### F-01 HIGH:` (no brackets) blocks merge.

    Pre-Phase-D the regex required bracketed IDs; reviewer drift would silently
    drop to zero counts and the gate would fail open. The loosened regex must
    recognise this variant.
    """
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        "# Security Review\n\n"
        "### F-01 HIGH: client-trusted price\n"
        "Drift-format finding without brackets.\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_doc_only_diff_merges_without_artifact(tmp_path, monkeypatch):
    """Task #2706: doc-only detection is DRIVEN BY THE REAL DIFF, not a flag.

    The reviewer is skipped on doc-only diffs (task #2358), so no
    SECURITY-REVIEW-NNNN.md is written. Pre-#2706 the caller had to pass
    ``expect_artifact=False`` to tell the invariant not to demand one — a
    caller-trust hole. That parameter is now removed: the gate diffs the
    real forge-task-9999 branch (docs.md only), calls the EXISTING
    ``is_doc_only_diff`` itself, derives doc_only=True, and merges without an
    artifact. No caller flag is involved.
    """
    from equipa import dispatch

    repo = tmp_path / "repo_doc_only"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@forgeborn.local")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    master_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "forge-task-9999")
    (repo / "docs.md").write_text("# Docs\n\nProse only.\n")
    _git(repo, "add", "docs.md")
    _git(repo, "commit", "-q", "-m", "docs: prose only")
    _git(repo, "checkout", "-q", "master")

    # Intentionally NO SECURITY-REVIEW-9999.md on disk.
    assert not (repo / "SECURITY-REVIEW-9999.md").exists()
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "merged"
    assert _git(repo, "rev-parse", "HEAD") != master_head


def test_code_diff_missing_artifact_blocks_via_ground_truth(
    gated_repo, monkeypatch,
):
    """Task #2706 (inverse of doc-only): a code diff with NO artifact and NO
    caller flag MUST block.

    This is the ground-truth path through ``_gated_merge_task`` (not a
    direct ``_merge_task_branch`` call). The forge-task-9999 branch contains
    feature.py, so the gate re-derives doc_only=False from the real diff,
    the artifact is missing, and the fail-closed policy blocks. Proves the
    gate never ASSUMES doc-only when the reviewer produced nothing — the old
    ``expect_artifact=False`` caller escape hatch is gone.
    """
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    assert not (repo / "SECURITY-REVIEW-9999.md").exists()
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_gate_signatures_have_no_caller_bypass_flag():
    """Task #2706: there is NO flag left for a caller to disable the gate.

    The whole class of bug (#362/#378 + the doc-only short-circuit) came
    from the gate trusting caller-supplied ``review_blocks_merge`` /
    ``expect_artifact`` signals. This test locks in their removal by
    inspecting the public gate entrypoints' signatures directly, so a future
    refactor cannot silently re-introduce a bypass parameter.
    """
    import inspect

    from equipa import cli, dispatch

    gated_params = set(
        inspect.signature(dispatch._gated_merge_task).parameters
    )
    post_params = set(
        inspect.signature(cli._gated_post_merge).parameters
    )

    forbidden = {"review_blocks_merge", "expect_artifact", "review_skipped_doc_only"}
    assert forbidden.isdisjoint(gated_params), (
        f"_gated_merge_task must not accept caller-trust flags; found "
        f"{forbidden & gated_params}"
    )
    assert forbidden.isdisjoint(post_params), (
        f"_gated_post_merge must not accept caller-trust flags; found "
        f"{forbidden & post_params}"
    )


def test_non_doc_diff_blocks_when_artifact_missing(gated_repo, monkeypatch):
    """Phase H (F-01): expect_artifact=True still fails closed on missing artifact.

    Locks in the fail-closed direction: a code diff (.py) with NO security
    review on disk MUST still raise — the doc-only short-circuit is the
    ONLY way to bypass the defensive invariant. The gated_repo fixture
    creates a forge-task-9999 branch with a feature.py file already.
    """
    from equipa import dispatch
    from equipa.security_gate import SecurityGateBypassError

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    # Artifact intentionally NOT written; diff includes feature.py.
    assert not (repo / "SECURITY-REVIEW-9999.md").exists()
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    with pytest.raises(SecurityGateBypassError):
        asyncio.run(dispatch._merge_task_branch(
            str(repo), 9999, "forge-task-9999",
            expect_artifact=True,
        ))

    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_counts_footer_preferred_over_headers(gated_repo, monkeypatch):
    """GATE-04: explicit Counts footer wins over header counting."""
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    # Body has zero formal finding headers, but the footer says 1 HIGH —
    # the parser MUST honour the tamper-evident footer.
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        "# Security Review\n\nPlain prose findings, no formal headers.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_phase_i_prose_headers_yield_to_counts_footer(gated_repo, monkeypatch):
    """Phase I-b (F-02): the Counts footer overrides false-positive prose.

    Concrete F-02 example: a section heading like
    ``### Summary of HIGH-impact findings`` matched the loosened Phase-D
    finding-header regex and falsely contributed +1 HIGH. With the
    mandated tamper-evident footer (CRITICAL: 0 | HIGH: 0 | ...) the
    parser must prefer the footer numbers and let the merge proceed.
    """
    from equipa import dispatch

    repo = gated_repo["repo"]
    master_head = gated_repo["master_head"]
    (repo / "SECURITY-REVIEW-9999.md").write_text(
        "# Security Review\n\n"
        "### Summary of HIGH-impact findings\n"
        "Nothing critical or high was found in this audit.\n\n"
        "### CRITICAL section (no findings)\n"
        "Reviewed; no critical issues identified.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    )
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
    ))

    assert result == "merged"
    assert _git(repo, "rev-parse", "HEAD") != master_head
