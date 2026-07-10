"""Unit + hermetic coverage for the single ground-truth merge gate (task #2706).

Task #2706 closed the merge-gate caller-trust hole by moving the gate policy
into ONE pure function — :func:`equipa.security_gate.decide_merge_gate` — that
computes a single :class:`GateDecision` from ground truth (the real branch
diff + the on-disk artifact) instead of trusting caller-supplied booleans.

The sibling ``test_security_gate_blocks_merge.py`` exercises the policy
*transitively* through ``dispatch._gated_merge_task`` (which needs a real git
repo). This module complements it by:

  1. Unit-testing ``decide_merge_gate`` DIRECTLY with an injected spy artifact
     reader, so every decision branch — and the fact that a caller can no
     longer inject a per-task trust flag — is pinned without git.
  2. Adding the two operator-policy branches that were introduced by #2706 but
     were not yet exercised end-to-end: the ``security_review_enabled=False``
     operator opt-out and the ``block_on_missing=False`` fail-open escape
     hatch, both driven through the unified ``_gated_merge_task``.

Hermetic: the git-backed tests isolate from the developer's global git config
(``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``) and cap every subprocess at
``timeout=10`` (task #2451 GATE-08 convention, mirrored here).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from equipa.security_gate import (
    GateDecision,
    decide_merge_gate,
    is_doc_only_diff,
)


class _SpyArtifactReader:
    """Records how (and whether) ``decide_merge_gate`` calls the artifact reader.

    Stands in for ``dispatch._security_review_blocks_merge`` so the pure
    decision logic can be tested in isolation. The injected callable's contract
    is ``(project_dir, task_id, *, block_on_missing) -> (blocks, counts)``.
    """

    def __init__(self, result: tuple[bool, dict | None]) -> None:
        self._result = result
        self.calls: list[dict] = []

    def __call__(
        self, project_dir: str, task_id: int, *, block_on_missing: bool,
    ) -> tuple[bool, dict | None]:
        self.calls.append(
            {
                "project_dir": project_dir,
                "task_id": task_id,
                "block_on_missing": block_on_missing,
            }
        )
        return self._result


# --------------------------------------------------------------------------
# Pure-function unit tests: decide_merge_gate
# --------------------------------------------------------------------------


def test_review_disabled_opts_out_without_reading_artifact():
    """``security_review_enabled=False`` is the operator's explicit opt-out.

    No artifact is ever produced when review is disabled, so the gate must not
    block and must not even consult the artifact reader — expect_artifact=False
    so the downstream defensive invariant does not demand a file that will
    never exist.
    """
    spy = _SpyArtifactReader((True, {"HIGH": 1}))

    decision = decide_merge_gate(
        ["feature.py"],  # a CODE diff — proves disable wins over doc-only-ness
        security_review_blocks_merge=spy,
        project_dir="/tmp/repo",
        task_id=2706,
        security_review_enabled=False,
    )

    assert decision.blocks_merge is False
    assert decision.expect_artifact is False
    assert decision.doc_only is False
    assert decision.counts is None
    assert decision.reason == "security-review-disabled"
    assert spy.calls == [], "artifact reader must not run when review disabled"


def test_doc_only_is_rederived_from_real_file_list_not_a_flag():
    """doc-only-ness is computed from the ACTUAL changed-file list here.

    The whole point of #2706: no caller asserts ``doc_only`` — the gate calls
    ``is_doc_only_diff`` on the real diff itself. A pure-doc file list must
    yield doc_only=True, a non-blocking merge, and NO artifact read.
    """
    files = ["docs/guide.md", "README.rst", "notes.txt"]
    assert is_doc_only_diff(files) is True  # sanity: the input really is doc-only
    spy = _SpyArtifactReader((True, {"CRITICAL": 1}))

    decision = decide_merge_gate(
        files,
        security_review_blocks_merge=spy,
        project_dir="/tmp/repo",
        task_id=2706,
    )

    assert decision.doc_only is True
    assert decision.blocks_merge is False
    assert decision.expect_artifact is False
    assert decision.reason == "doc-only-diff"
    assert spy.calls == [], "doc-only diffs must not consult the artifact reader"


def test_code_diff_reads_artifact_and_blocks_on_high():
    """A code diff routes through the artifact reader and honours a block.

    The reader receives exactly the ground-truth coordinates (project_dir,
    task_id, block_on_missing) — never a caller-supplied gate decision — and
    its ``(blocks, counts)`` result is surfaced verbatim.
    """
    counts = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    spy = _SpyArtifactReader((True, counts))

    decision = decide_merge_gate(
        ["equipa/feature.py"],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    assert decision.blocks_merge is True
    assert decision.doc_only is False
    assert decision.expect_artifact is True
    assert decision.counts == counts
    assert decision.reason == "security-review-blocked"
    assert spy.calls == [
        {"project_dir": "/srv/repo", "task_id": 2706, "block_on_missing": True}
    ]


def test_code_diff_with_clean_artifact_permits_merge():
    """A code diff whose artifact reports no CRITICAL/HIGH merges cleanly."""
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 2, "LOW": 1, "INFO": 3}
    spy = _SpyArtifactReader((False, counts))

    decision = decide_merge_gate(
        ["equipa/feature.py"],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    assert decision.blocks_merge is False
    assert decision.doc_only is False
    assert decision.expect_artifact is True
    assert decision.counts == counts
    assert decision.reason == "clean"
    assert len(spy.calls) == 1


def test_empty_file_list_fails_closed_and_is_treated_as_code():
    """An empty diff (git lookup failed) MUST be treated as code, never doc-only.

    ``is_doc_only_diff([])`` returns False by design, so the gate consults the
    artifact reader (fail-closed) rather than silently skipping the gate — the
    exact silent-skip class of bug the task requires us to keep closed.
    """
    spy = _SpyArtifactReader((True, None))

    decision = decide_merge_gate(
        [],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    assert decision.doc_only is False
    assert decision.expect_artifact is True
    assert decision.blocks_merge is True
    assert len(spy.calls) == 1, "empty (failed) diff must still hit the gate"


def test_mixed_doc_and_code_diff_is_not_doc_only():
    """A single non-doc file in the diff defeats the doc-only short-circuit."""
    spy = _SpyArtifactReader((False, {"HIGH": 0}))

    decision = decide_merge_gate(
        ["CHANGELOG.md", "equipa/dispatch.py"],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    assert decision.doc_only is False
    assert decision.expect_artifact is True
    assert len(spy.calls) == 1


def test_block_on_missing_flag_is_threaded_to_the_reader():
    """The ``block_on_missing`` fail-open escape hatch reaches the reader intact."""
    spy = _SpyArtifactReader((False, None))

    decide_merge_gate(
        ["equipa/feature.py"],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
        block_on_missing=False,
    )

    assert spy.calls == [
        {"project_dir": "/srv/repo", "task_id": 2706, "block_on_missing": False}
    ]


def test_changed_files_are_snapshotted_into_the_decision():
    """The decision stores a COPY of the diff, immune to later caller mutation."""
    files = ["equipa/feature.py"]
    spy = _SpyArtifactReader((False, {"HIGH": 0}))

    decision = decide_merge_gate(
        files,
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    files.append("injected_after_the_fact.py")
    assert decision.changed_files == ["equipa/feature.py"]


def test_gate_decision_is_frozen():
    """A computed decision cannot be mutated after the fact (frozen dataclass)."""
    spy = _SpyArtifactReader((False, None))
    decision = decide_merge_gate(
        ["equipa/feature.py"],
        security_review_blocks_merge=spy,
        project_dir="/srv/repo",
        task_id=2706,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.blocks_merge = False  # type: ignore[misc]


def test_decide_merge_gate_signature_has_no_per_task_trust_flag():
    """Pin the no-caller-trust contract at the pure-function boundary.

    The removed hole was the tri-state ``review_blocks_merge`` /
    ``expect_artifact`` / ``doc_only`` caller signals. ``decide_merge_gate``
    must accept NONE of them: doc-only-ness and the block decision are derived
    internally. Only the injected reader (``security_review_blocks_merge``,
    a callable, not a bool) and the two GLOBAL operator-policy flags remain.
    """
    params = set(inspect.signature(decide_merge_gate).parameters)
    forbidden = {"expect_artifact", "doc_only", "review_blocks_merge",
                 "review_skipped_doc_only"}
    assert forbidden.isdisjoint(params), (
        f"decide_merge_gate must not accept caller-trust flags; found "
        f"{forbidden & params}"
    )
    # The reader is injected as a callable and the operator flags are present.
    assert "security_review_blocks_merge" in params
    assert "security_review_enabled" in params
    assert "block_on_missing" in params


# --------------------------------------------------------------------------
# Hermetic operator-policy tests through the unified gate (_gated_merge_task)
# --------------------------------------------------------------------------


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=_isolated_env(),
    )
    return result.stdout.strip()


@pytest.fixture
def code_diff_repo(tmp_path: Path) -> dict:
    """A repo whose forge-task-9999 branch has a CODE diff and NO artifact."""
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
    (repo / "feature.py").write_text("# real code, would need a review\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feat: code change")
    _git(repo, "checkout", "-q", "master")
    # No SECURITY-REVIEW-9999.md on disk on purpose.
    assert not (repo / "SECURITY-REVIEW-9999.md").exists()
    return {"repo": repo, "master_head": master_head}


def test_operator_disabled_review_merges_code_diff_without_artifact(
    code_diff_repo, monkeypatch,
):
    """``security_review_enabled=False`` (operator opt-out) merges a code diff
    even with no artifact — no review was ever requested, so none is expected.

    This is the OPERATOR-GLOBAL policy branch, distinct from a per-task trust
    flag: it is only ever set from the operator's own feature-flag config at
    the call site, never from anything an agent/reviewer supplies.
    """
    from equipa import dispatch

    repo = code_diff_repo["repo"]
    master_head = code_diff_repo["master_head"]
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
        security_review_enabled=False,
    ))

    assert result == "merged"
    assert _git(repo, "rev-parse", "HEAD") != master_head


def test_default_operator_policy_blocks_same_code_diff_without_artifact(
    code_diff_repo, monkeypatch,
):
    """Contrast to the opt-out: with review ENABLED (default) the identical
    code-diff-without-artifact case fails closed and blocks.

    Pairing this with the opt-out test proves the ``security_review_enabled``
    flag is the ONLY pivot, and that the default posture is the strict one —
    the gate is never looser than today unless the operator explicitly opts
    out.
    """
    from equipa import dispatch

    repo = code_diff_repo["repo"]
    master_head = code_diff_repo["master_head"]
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
        # security_review_enabled defaults to True.
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head


def test_block_on_missing_false_is_still_caught_by_defensive_invariant(
    code_diff_repo, monkeypatch,
):
    """A code diff with a MISSING artifact stays BLOCKED even with the operator
    fail-open flag ``block_on_missing=False`` — the defensive invariant wins.

    This documents an important layering fact discovered while testing task
    #2706: the ``security_review_block_on_missing_artifact`` fail-open escape
    hatch only relaxes the POLICY layer (``decide_merge_gate`` returns
    ``blocks_merge=False``). But for any non-doc diff the derived
    ``expect_artifact`` is True, so the independent defensive invariant inside
    ``_merge_task_branch`` re-reads the artifact, finds it missing, and raises
    ``SecurityGateBypassError`` — which ``_gated_merge_task`` reports as
    ``blocked``. HEAD never advances.

    Net effect: for a CODE diff the fail-open flag is effectively inert; the
    unified gate is STRICTER than the flag alone would suggest. That direction
    satisfies the task-#2706 constraint that the gate "stay at least as strict
    as today (never looser)". The flag remains observable only for the
    ``expect_artifact=False`` paths (doc-only / review-disabled), where no
    artifact is needed in the first place. Flagged for the orchestrator in the
    DECISIONS block as a semantic observation, not a test-forced code change.
    """
    from equipa import dispatch

    repo = code_diff_repo["repo"]
    master_head = code_diff_repo["master_head"]
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")

    result = asyncio.run(dispatch._gated_merge_task(
        repo=str(repo),
        branch="forge-task-9999",
        outcome="tests_passed",
        task_id=9999,
        project_context={"id": 23},
        block_on_missing=False,
    ))

    assert result == "blocked"
    assert _git(repo, "rev-parse", "HEAD") == master_head
