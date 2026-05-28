"""Tests for task #2479: branch-agnostic default-branch detection.

Covers:

* The new ``get_default_branch`` utility resolves repos that use ``main``,
  repos that use ``master``, and an arbitrary current branch.
* The four production call sites previously hardcoded to ``"master"``
  (``security_gate.get_changed_files_for_branch`` default, the cli call,
  the dispatch call, and the ``git push -u origin <branch>`` fallback in
  ``git_ops.setup_single_repo``) operate correctly against a repo whose
  default branch is ``main``.
* A grep-based regression guard ensures production code never reintroduces
  the literal string ``"master"`` outside of explicit fallback detection.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from equipa import git_ops, security_gate
from equipa.git_ops import (
    DefaultBranchDetectionError,
    _clear_default_branch_cache,
    get_default_branch,
    setup_single_repo,
)


# --- Helpers ----------------------------------------------------------------


def _init_repo(path: Path, initial_branch: str) -> None:
    """Initialize a git repo at ``path`` with ``initial_branch`` as default
    and a single committed file so ``HEAD`` is valid.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", initial_branch, str(path)],
        check=True, capture_output=True,
    )
    # Identity for the commit (avoid relying on global git config).
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@forgeborn.dev"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Forgeborn Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


@pytest.fixture(autouse=True)
def _reset_default_branch_cache() -> None:
    """Each test sees a fresh cache so repos at reused tmp_path keys work."""
    _clear_default_branch_cache()
    yield
    _clear_default_branch_cache()


# --- get_default_branch -----------------------------------------------------


class TestGetDefaultBranch:
    def test_main_repo_returns_main(self, tmp_path: Path) -> None:
        repo = tmp_path / "main_repo"
        _init_repo(repo, "main")
        assert get_default_branch(repo) == "main"

    def test_master_repo_returns_master(self, tmp_path: Path) -> None:
        repo = tmp_path / "master_repo"
        _init_repo(repo, "master")
        assert get_default_branch(repo) == "master"

    def test_unusual_branch_returns_current(self, tmp_path: Path) -> None:
        """Repo with neither ``main`` nor ``master`` falls through to the
        current-branch probe (``rev-parse --abbrev-ref HEAD``).
        """
        repo = tmp_path / "trunk_repo"
        _init_repo(repo, "trunk")
        assert get_default_branch(repo) == "trunk"

    def test_origin_head_wins_when_set(self, tmp_path: Path) -> None:
        """If ``refs/remotes/origin/HEAD`` is set it short-circuits the
        local-branch heuristic.
        """
        upstream = tmp_path / "upstream"
        _init_repo(upstream, "main")
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "--quiet", str(upstream), str(clone)],
            check=True, capture_output=True,
        )
        # `git clone` already populates origin/HEAD; confirm the helper
        # picks it up rather than falling through to the local-branch step.
        assert get_default_branch(clone) == "main"

    def test_result_is_cached_per_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "cached_repo"
        _init_repo(repo, "main")
        first = get_default_branch(repo)
        # Rename the local branch so a re-probe would return a different
        # value. The cache should still return the original.
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-m", "main", "renamed"],
            check=True, capture_output=True,
        )
        second = get_default_branch(repo)
        assert first == "main"
        assert second == "main"  # cached, not re-detected

    def test_clear_cache_forces_redetect(self, tmp_path: Path) -> None:
        repo = tmp_path / "recached_repo"
        _init_repo(repo, "main")
        assert get_default_branch(repo) == "main"
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-m", "main", "trunk"],
            check=True, capture_output=True,
        )
        _clear_default_branch_cache()
        assert get_default_branch(repo) == "trunk"

    def test_non_repo_falls_back_to_master(self, tmp_path: Path) -> None:
        """A directory with no git metadata falls all the way through to
        the legacy ``"master"`` fallback rather than raising.
        """
        empty = tmp_path / "not_a_repo"
        empty.mkdir()
        assert get_default_branch(empty) == "master"


# --- Call site integration: main-default repo -------------------------------


class TestCallSitesOnMainDefaultRepo:
    """Each of the 4 sites that previously hardcoded ``"master"`` must now
    operate against a repo whose default branch is ``main``.
    """

    def _make_main_repo_with_feature_branch(self, tmp_path: Path) -> Path:
        """Repo on `main` with a `feature` branch one commit ahead."""
        repo = tmp_path / "repo"
        _init_repo(repo, "main")
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "feature.py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "feat"],
            check=True, capture_output=True,
        )
        return repo

    def test_security_gate_changed_files_uses_main(
        self, tmp_path: Path,
    ) -> None:
        """``get_changed_files_for_branch`` with default args must diff
        against `main`, not against a nonexistent `master`.
        """
        repo = self._make_main_repo_with_feature_branch(tmp_path)
        files = asyncio.run(
            security_gate.get_changed_files_for_branch(str(repo)),
        )
        assert files == ["feature.py"]

    def test_security_gate_explicit_master_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        """Sanity: if a caller explicitly passes ``base_ref="master"`` on
        a main-only repo, the helper degrades gracefully (empty list)
        rather than raising — preserves existing fail-safe contract.
        """
        repo = self._make_main_repo_with_feature_branch(tmp_path)
        files = asyncio.run(
            security_gate.get_changed_files_for_branch(
                str(repo), base_ref="master",
            ),
        )
        assert files == []

    def test_get_default_branch_main_for_call_sites(
        self, tmp_path: Path,
    ) -> None:
        """The single source of truth all 4 production sites consult must
        return `main` on a main-default repo. Guards against any future
        regression where a call site bypasses ``get_default_branch``.
        """
        repo = self._make_main_repo_with_feature_branch(tmp_path)
        assert get_default_branch(repo) == "main"

    def test_master_still_supported(self, tmp_path: Path) -> None:
        """The new code must keep working on the existing master-default
        repos (constraint #4 in the task description).
        """
        repo = tmp_path / "legacy"
        _init_repo(repo, "master")
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        (repo / "feature.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "feature.py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "feat"],
            check=True, capture_output=True,
        )
        assert get_default_branch(repo) == "master"
        files = asyncio.run(
            security_gate.get_changed_files_for_branch(str(repo)),
        )
        assert files == ["feature.py"]


# --- Regression: no hardcoded "master" in production code -------------------


class TestNoHardcodedMasterInProduction:
    """Counted, line-anchored guard (S4 of SECURITY-REVIEW-2479).

    Earlier versions of this test exempted entire files (``equipa/git_ops.py``,
    ``equipa/single_agent_guard.py``, ``equipa/monitoring.py``,
    ``equipa/dispatch.py``) which let a developer silently add new
    hardcoded ``"master"`` references inside those files.

    The replacement enforces:
      * **Other files**  — zero occurrences of the literal ``"master"``
        outside comments. Any new offender fails the test immediately.
      * **Allow-listed files** — an EXACT expected count of code-line
        occurrences. The count is what these modules carry today as
        legitimate fallback / detection sites. Adding a new
        ``"master"`` to one of them bumps the count and fails the test
        loudly; the developer must either remove the new reference
        (call ``get_default_branch`` instead) or — if it is genuinely
        a new legitimate fallback — bump the expected count here.

    Comments and lines inside triple-quoted docstrings are exempt from
    the count.
    """

    # Modules that are KNOWN to ship legitimate fallback references plus
    # their expected COUNT of code-line ``"master"`` occurrences. Bumping
    # a count is a deliberate decision that requires editing this map.
    EXPECTED_MASTER_COUNT: dict[str, int] = {
        "equipa/git_ops.py": 4,
        "equipa/single_agent_guard.py": 1,
        "equipa/monitoring.py": 1,
        "equipa/dispatch.py": 2,
    }

    @staticmethod
    def _count_master_occurrences(text: str) -> list[tuple[int, str]]:
        """Count ``"master"`` / ``'master'`` occurrences in CODE lines.

        Skips:
          * Lines whose first non-whitespace char is ``#`` (comments).
          * Lines inside a triple-quoted string (best-effort tracking of
            ``\"\"\"`` and ``'''`` delimiters).

        Returns a list of (lineno, stripped_line) for every code-line
        hit so failure messages can show exactly what tripped the guard.
        """
        hits: list[tuple[int, str]] = []
        in_triple_double = False
        in_triple_single = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Toggle triple-quote state (best-effort: a line containing
            # an odd number of triple-quote markers flips the state).
            tdq = line.count('"""')
            tsq = line.count("'''")
            line_starts_in_string = in_triple_double or in_triple_single
            if tdq % 2 == 1:
                in_triple_double = not in_triple_double
            if tsq % 2 == 1:
                in_triple_single = not in_triple_single
            if line_starts_in_string:
                continue
            if stripped.startswith("#"):
                continue
            if '"master"' in line or "'master'" in line:
                hits.append((lineno, stripped))
        return hits

    def test_no_new_hardcoded_master(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        equipa_dir = repo_root / "equipa"
        offenders: list[str] = []
        count_drift: list[str] = []
        for py_file in sorted(equipa_dir.rglob("*.py")):
            rel = py_file.relative_to(repo_root).as_posix()
            text = py_file.read_text(encoding="utf-8", errors="replace")
            hits = self._count_master_occurrences(text)
            expected = self.EXPECTED_MASTER_COUNT.get(rel)
            if expected is None:
                for lineno, stripped in hits:
                    offenders.append(f"{rel}:{lineno}: {stripped}")
            else:
                if len(hits) != expected:
                    formatted_hits = "\n".join(
                        f"  {rel}:{ln}: {s}" for ln, s in hits
                    ) or "  (no hits found)"
                    count_drift.append(
                        f"{rel}: expected {expected} \"master\" "
                        f"occurrences, found {len(hits)}.\n"
                        f"{formatted_hits}\n"
                        f"If you added a new legitimate fallback, bump "
                        f"EXPECTED_MASTER_COUNT[{rel!r}]; otherwise "
                        f"call get_default_branch() instead."
                    )
        assert not offenders, (
            "Hardcoded \"master\" found in production code outside the "
            "approved fallback sites — call get_default_branch() instead:\n"
            + "\n".join(offenders)
        )
        assert not count_drift, (
            "EXPECTED_MASTER_COUNT drift:\n" + "\n".join(count_drift)
        )

    def test_allow_list_files_actually_exist(self) -> None:
        """Catch typos in the allow list — if a file is renamed, the
        regression guard would silently let new offenders through.
        """
        repo_root = Path(__file__).resolve().parents[1]
        for rel in self.EXPECTED_MASTER_COUNT:
            assert (repo_root / rel).exists(), (
                f"Allow-list entry {rel!r} does not exist; update "
                "TestNoHardcodedMasterInProduction.EXPECTED_MASTER_COUNT."
            )

    def test_get_default_branch_is_exported(self) -> None:
        """Sanity: the utility must be importable from its documented
        location so future call sites can adopt it.
        """
        assert callable(git_ops.get_default_branch)
        assert callable(git_ops._clear_default_branch_cache)


# --- S1: setup_single_repo asymmetric push fallback -------------------------


class TestSetupSingleRepoPushFallback:
    """S1 of SECURITY-REVIEW-2479: when the first ``push -u origin
    <default_branch>`` fails AND we fall back to ``main``, the second
    push's returncode must also be checked. If both fail the function
    must return ``(False, message)`` with BOTH stderr blobs captured.
    """

    def test_both_pushes_fail_returns_failure_with_both_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # (a) Remote default branch is ``main`` (bare repo init -b main).
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(remote)],
            check=True, capture_output=True,
        )
        # (b) Local repo on ``master`` only — NO origin remote yet, so
        # setup_single_repo will follow the "gh repo create failed with
        # already-exists" branch where the push fallback lives.
        local = tmp_path / "local"
        _init_repo(local, "master")

        # Stub the gh CLI call so setup_single_repo enters the
        # "already exists" branch where the push fallback lives.
        from equipa import git_ops as _git_ops

        def _fake_gh_run(args, cwd, timeout=120):
            return subprocess.CompletedProcess(
                args=["gh", *args], returncode=1,
                stdout="", stderr="GraphQL: Name already exists on this account",
            )
        monkeypatch.setattr(_git_ops, "_gh_run", _fake_gh_run)

        # Force both pushes to fail so we can assert both stderrs land
        # in the returned message. We do this by replacing git_run for
        # any ``push`` invocation with a synthetic failure response;
        # other git invocations (remote add, etc.) pass through to the
        # real subprocess.
        real_git_run = _git_ops.git_run
        push_calls: list[list[str]] = []

        def _fake_git_run(args, cwd, timeout=120):
            if args and args[0] == "push":
                push_calls.append(list(args))
                branch = args[-1]
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=1, stdout="",
                    stderr=f"fatal: push refused for {branch}",
                )
            return real_git_run(args, cwd, timeout=timeout)
        monkeypatch.setattr(_git_ops, "git_run", _fake_git_run)

        ok, msg = setup_single_repo(
            codename="dummy", project_dir=local, owner="forgeborn",
        )

        # (d) Both pushes attempted; failure propagates; both stderr captured.
        assert ok is False, f"expected failure, got success: {msg}"
        assert "master" in msg, msg
        assert "main" in msg, msg
        assert "push refused for master" in msg, msg
        assert "push refused for main" in msg, msg
        # Sanity: BOTH pushes were attempted in order (master then main).
        pushed_branches = [c[-1] for c in push_calls]
        assert pushed_branches == ["master", "main"], pushed_branches


# --- S2: cache TTL invalidation ---------------------------------------------


class TestCacheTtlInvalidation:
    """S2 of SECURITY-REVIEW-2479: cached default branch entries must
    expire so a long-lived orchestrator process picks up a rename
    (master -> main) within 5 minutes.
    """

    def test_cached_value_expires_after_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "ttl_repo"
        _init_repo(repo, "main")

        fake_now = [1000.0]

        def _fake_monotonic() -> float:
            return fake_now[0]
        monkeypatch.setattr(git_ops.time, "monotonic", _fake_monotonic)

        first = get_default_branch(repo)
        assert first == "main"

        # Rename underneath the cache.
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-m", "main", "trunk"],
            check=True, capture_output=True,
        )

        # Still within TTL -> cached "main" is returned.
        fake_now[0] += 60.0
        assert get_default_branch(repo) == "main"

        # Past TTL -> re-detected.
        fake_now[0] += 301.0
        assert get_default_branch(repo) == "trunk"


# --- S3: warning + strict mode ---------------------------------------------


class TestStrictMode:
    """S3 of SECURITY-REVIEW-2479: ``get_default_branch`` must
    optionally raise ``DefaultBranchDetectionError`` when every
    detection strategy fails, and must log a WARNING in the
    backward-compatible (``strict=False``) path.
    """

    def test_strict_raises_on_total_failure(self, tmp_path: Path) -> None:
        empty = tmp_path / "not_a_repo"
        empty.mkdir()
        with pytest.raises(DefaultBranchDetectionError):
            get_default_branch(empty, strict=True)

    def test_non_strict_falls_back_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        empty = tmp_path / "not_a_repo2"
        empty.mkdir()
        with caplog.at_level("WARNING", logger="equipa.git_ops"):
            assert get_default_branch(empty) == "master"
        warned = [
            r for r in caplog.records
            if "legacy fallback 'master'" in r.getMessage()
        ]
        assert warned, (
            "expected a WARNING-level log about the legacy 'master' "
            f"fallback, got: {[r.getMessage() for r in caplog.records]}"
        )
