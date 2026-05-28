"""EQUIPA git operations: repo setup, language detection, and git helpers.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
All functions are re-exported via equipa/__init__.py for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from equipa.constants import (
    GIT_DEFAULT_TIMEOUT,
    GITIGNORE_TEMPLATES,
    GITHUB_OWNER,
    PROJECT_DIRS,
)

logger = logging.getLogger(__name__)


class DefaultBranchDetectionError(RuntimeError):
    """Raised by ``get_default_branch(strict=True)`` when every detection
    strategy fails (no ``origin/HEAD``, no local ``main``/``master``, no
    valid ``HEAD``).

    Callers that need fail-closed behaviour (security_gate, dispatch) can
    opt in via ``strict=True``. Default behaviour remains backward-
    compatible: ``strict=False`` falls back to the legacy ``"master"``
    string while emitting a WARNING log so the silent fallback is at
    least observable.
    """


def _get_git_identity() -> tuple[str | None, str | None]:
    """Read git_author_name and git_author_email from dispatch_config.json.

    Returns (name, email) tuple. Either or both may be None if not configured.
    """
    import json as _json
    try:
        from equipa.constants import THEFORGE_DB
        config_path = Path(THEFORGE_DB).parent / "dispatch_config.json"
        if config_path.exists():
            data = _json.loads(config_path.read_text(encoding="utf-8"))
            return data.get("git_author_name"), data.get("git_author_email")
    except Exception:
        pass
    return None, None


def _git_identity_args() -> list[str]:
    """Return git -c args for author identity, or empty list if not configured."""
    name, email = _get_git_identity()
    args = []
    if name:
        args.extend(["-c", f"user.name={name}"])
    if email:
        args.extend(["-c", f"user.email={email}"])
    return args


def _is_git_repo(path: str | Path) -> bool:
    """Check if a directory is a git repository."""
    return (Path(path) / ".git").exists()


def detect_project_language(project_dir: str | Path) -> dict:
    """Detect languages and frameworks in a project by scanning for marker files.

    Returns a dict with:
        - languages: list of detected language strings
        - frameworks: list of detected framework strings
        - primary: the most likely primary language (string)

    The primary language is chosen by a priority order that favours explicit
    project manifests over file-extension scanning.
    """
    p = Path(project_dir)
    languages: list[str] = []
    frameworks: list[str] = []

    # --- Language detection via marker files ---

    # Python: pyproject.toml, setup.py, requirements.txt, Pipfile
    python_markers = ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"]
    if any((p / m).exists() for m in python_markers) or list(p.glob("*.py")):
        languages.append("python")
        if (p / "pyproject.toml").exists():
            try:
                content = (p / "pyproject.toml").read_text(
                    encoding="utf-8", errors="replace",
                )
                if "django" in content.lower():
                    frameworks.append("django")
                if "fastapi" in content.lower():
                    frameworks.append("fastapi")
                if "flask" in content.lower():
                    frameworks.append("flask")
            except OSError:
                pass

    # TypeScript: tsconfig.json
    has_tsconfig = (p / "tsconfig.json").exists()
    if has_tsconfig:
        languages.append("typescript")

    # JavaScript: package.json without tsconfig (pure JS)
    has_package_json = (p / "package.json").exists()
    if has_package_json and not has_tsconfig:
        if (p / "jsconfig.json").exists() or not has_tsconfig:
            languages.append("javascript")

    # Detect Node/JS frameworks from package.json
    if has_package_json:
        try:
            content = (p / "package.json").read_text(
                encoding="utf-8", errors="replace",
            )
            if '"next"' in content:
                frameworks.append("nextjs")
            if '"react"' in content:
                frameworks.append("react")
            if '"express"' in content:
                frameworks.append("express")
            if '"vue"' in content:
                frameworks.append("vue")
            if '"angular"' in content or '"@angular/core"' in content:
                frameworks.append("angular")
        except OSError:
            pass

    # Go: go.mod
    if (p / "go.mod").exists():
        languages.append("go")

    # Rust: Cargo.toml
    if (p / "Cargo.toml").exists():
        languages.append("rust")

    # C#/.NET: *.csproj, *.sln
    if (
        list(p.glob("*.csproj"))
        or list(p.glob("*.sln"))
        or list(p.glob("**/*.csproj"))
    ):
        languages.append("csharp")
        frameworks.append("dotnet")

    # Java: pom.xml, build.gradle
    if (
        (p / "pom.xml").exists()
        or (p / "build.gradle").exists()
        or (p / "build.gradle.kts").exists()
    ):
        languages.append("java")
        if (p / "pom.xml").exists():
            frameworks.append("maven")
        if (p / "build.gradle").exists() or (p / "build.gradle.kts").exists():
            frameworks.append("gradle")

    # Determine primary language (first detected wins based on priority above)
    primary = languages[0] if languages else "default"

    return {
        "languages": languages,
        "frameworks": frameworks,
        "primary": primary,
    }


def _get_repo_env() -> dict[str, str]:
    """Build an environment dict with git and gh on the PATH."""
    env = os.environ.copy()
    extra_paths = []
    for candidate in [
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\GitHub CLI",
    ]:
        if os.path.isdir(candidate) and candidate not in env.get("PATH", ""):
            extra_paths.append(candidate)
    if extra_paths:
        env["PATH"] = ";".join(extra_paths) + ";" + env.get("PATH", "")
    return env


def _run_with_env(
    args_list: list[str],
    cwd: str | Path,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Low-level subprocess runner with Windows-PATH-fixed env. Internal use only."""
    return subprocess.run(
        args_list, capture_output=True, text=True,
        cwd=str(cwd), timeout=timeout, env=_get_repo_env(),
    )


def git_run(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a git command with standard env (Windows PATH fix) and timeout.

    The "git" prefix is added automatically — pass only the subcommand and
    its arguments, e.g. ``git_run(["status", "--porcelain"], cwd=repo)``.

    This is the single supported entry point for every git invocation in
    EQUIPA. It guarantees consistent timeout handling and PATH resolution.
    """
    return _run_with_env(["git", *args], cwd, timeout)


async def git_run_async(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Async equivalent of ``git_run`` — does NOT block the event loop.

    Uses ``asyncio.create_subprocess_exec`` so callers running inside an
    async function (dispatch loops, dev-test loop) can issue git commands
    without serialising the loop. Returns a ``subprocess.CompletedProcess``
    with the same ``returncode``, ``stdout``, and ``stderr`` shape as
    ``git_run`` so call sites can be migrated incrementally.

    A ``TimeoutError`` is raised if the command exceeds ``timeout`` seconds;
    the child process is killed before the error propagates.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        env=_get_repo_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise subprocess.TimeoutExpired(["git", *args], timeout) from e
    return subprocess.CompletedProcess(
        args=["git", *args],
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


def _gh_run(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a gh (GitHub CLI) command with the same env as git_run."""
    return _run_with_env(["gh", *args], cwd, timeout)


# Per-process cache of detected default branch, keyed by resolved repo path.
# Populated by get_default_branch() so the orchestrator does not re-shell out
# on every security-gate diff. Entries store (branch_name, expiry_epoch).
# A 5-minute TTL bounds the window where a rename (master->main) goes
# undetected within a long-lived process (S2 of SECURITY-REVIEW-2479).
# Tests that recreate repos at the same path within the TTL must call
# ``_clear_default_branch_cache`` explicitly.
_DEFAULT_BRANCH_CACHE: dict[str, tuple[str, float]] = {}
_DEFAULT_BRANCH_CACHE_TTL_SECONDS: float = 300.0


def _cache_set(key: str, value: str) -> None:
    _DEFAULT_BRANCH_CACHE[key] = (value, time.monotonic() + _DEFAULT_BRANCH_CACHE_TTL_SECONDS)


def _cache_get(key: str) -> str | None:
    entry = _DEFAULT_BRANCH_CACHE.get(key)
    if entry is None:
        return None
    value, expiry = entry
    if time.monotonic() >= expiry:
        _DEFAULT_BRANCH_CACHE.pop(key, None)
        return None
    return value


def get_default_branch(repo_path: str | Path, *, strict: bool = False) -> str:
    """Detect the default branch of ``repo_path`` (``main`` vs ``master``).

    Resolution order:
      1. ``git symbolic-ref --short refs/remotes/origin/HEAD`` — fast and
         definitive when ``origin/HEAD`` is set on the remote.
      2. First existing local branch among ``main`` then ``master``.
      3. ``git rev-parse --abbrev-ref HEAD`` — whatever branch is currently
         checked out.
      4. If ``strict=True``, raise :class:`DefaultBranchDetectionError`.
         Otherwise (default) emit a WARNING and fall back to ``"master"``.

    Results are cached per-process for 5 minutes keyed by the resolved
    absolute path of ``repo_path``. The TTL bounds the window where a
    branch rename remains undetected in a long-lived orchestrator
    process. Call :func:`_clear_default_branch_cache` to invalidate
    immediately (only needed in tests that recreate repos at the same
    path within the TTL).

    Args:
        repo_path: Filesystem path to a git working tree.
        strict: If True, raise :class:`DefaultBranchDetectionError` on
            total detection failure instead of returning ``"master"``.

    Raises:
        DefaultBranchDetectionError: Only when ``strict=True`` and every
            detection strategy fails.
    """
    key = str(Path(repo_path).resolve())
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # 1) origin/HEAD
    try:
        result = git_run(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            repo_path, timeout=5,
        )
        if result.returncode == 0:
            ref = (result.stdout or "").strip()
            if ref.startswith("origin/"):
                ref = ref[len("origin/"):]
            if ref:
                _cache_set(key, ref)
                return ref
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    # 2) Local branch existence: prefer main, fall back to master
    for candidate in ("main", "master"):
        try:
            result = git_run(
                ["rev-parse", "--verify", "--quiet",
                 f"refs/heads/{candidate}"],
                repo_path, timeout=5,
            )
            if result.returncode == 0:
                _cache_set(key, candidate)
                return candidate
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            continue

    # 3) Current branch
    try:
        result = git_run(
            ["rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=5,
        )
        if result.returncode == 0:
            ref = (result.stdout or "").strip()
            if ref and ref != "HEAD":
                _cache_set(key, ref)
                return ref
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    # 4) Total detection failure.
    if strict:
        raise DefaultBranchDetectionError(
            f"could not detect default branch for {repo_path!s}"
        )
    logger.warning(
        "could not detect default branch for %s, using legacy fallback 'master'",
        repo_path,
    )
    _cache_set(key, "master")
    return "master"


def _clear_default_branch_cache() -> None:
    """Drop every cached default-branch entry. Intended for tests.

    The underscore prefix marks this as a test-only helper (S5 of
    SECURITY-REVIEW-2479).
    """
    _DEFAULT_BRANCH_CACHE.clear()


def _git_commit(
    message: str,
    cwd: str | Path,
    timeout: int = 120,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run git commit with EQUIPA identity from dispatch_config.

    Always injects -c user.name and -c user.email if configured,
    so commits are attributed correctly regardless of global git config.
    """
    args = [*_git_identity_args(), "commit", "-m", message]
    if extra_args:
        args.extend(extra_args)
    return git_run(args, cwd, timeout=timeout)


def check_gh_installed() -> bool:
    """Verify that gh CLI is installed and authenticated.

    Returns True if ready, prints error and returns False otherwise.
    """
    if not shutil.which("gh"):
        print("ERROR: GitHub CLI (gh) is not installed.")
        print("Install it from: https://cli.github.com/")
        return False

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print("ERROR: GitHub CLI is not authenticated.")
            print("Run: gh auth login")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("ERROR: Could not check gh auth status.")
        return False

    return True


def setup_single_repo(
    codename: str,
    project_dir: str | Path,
    owner: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Initialize git and create a GitHub private repo for a single project.

    Returns (success: bool, message: str).
    """
    p = Path(project_dir)
    repo_name = codename.lower().replace(" ", "-")

    # Skip if already fully set up (has .git AND a remote)
    has_git = (p / ".git").exists()
    if has_git:
        try:
            r = git_run(["remote", "get-url", "origin"], p, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True, f"Already set up (remote: {r.stdout.strip()})"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if dry_run:
        lang_info = detect_project_language(project_dir)
        return True, (
            f"DRY RUN: Would init git, detect={lang_info['primary']}, "
            f"create {owner}/{repo_name}"
        )

    # .gitignore
    lang_info = detect_project_language(project_dir)
    lang = lang_info["primary"]
    # Map new language keys to existing gitignore template keys
    gitignore_key_map = {
        "typescript": "node",
        "javascript": "node",
        "csharp": "dotnet",
    }
    gitignore_key = gitignore_key_map.get(lang, lang)
    gitignore_path = p / ".gitignore"
    if not gitignore_path.exists():
        template = GITIGNORE_TEMPLATES.get(
            gitignore_key, GITIGNORE_TEMPLATES["default"],
        )
        gitignore_path.write_text(template + "\n", encoding="utf-8")
        print(f"    Created .gitignore ({lang})")

    # git init
    if not has_git:
        r = git_run(["init"], p)
        if r.returncode != 0:
            return False, f"git init failed: {r.stderr.strip()}"
    else:
        print("    .git already exists, resuming setup")

    # git add (filter CRLF warnings)
    r = git_run(["add", "."], p, timeout=300)
    if r.returncode != 0:
        real_errors = [
            line for line in r.stderr.strip().splitlines()
            if not line.startswith("warning:")
        ]
        if real_errors:
            return False, f"git add failed: {chr(10).join(real_errors)}"

    # git commit
    commit_args = [*_git_identity_args(), "commit", "-m", "Initial commit"]
    r = git_run(commit_args, p, timeout=120)
    if r.returncode != 0:
        if "nothing to commit" not in (r.stdout + r.stderr):
            return False, f"git commit failed: {r.stderr.strip()}"
        print("    Nothing to commit (empty or already committed)")

    # gh repo create
    r = _gh_run(
        ["repo", "create", f"{owner}/{repo_name}",
         "--private", "--source=.", "--push"],
        p, timeout=300,
    )
    if r.returncode != 0:
        if "already exists" in r.stderr:
            print("    Repo already exists on GitHub, adding remote...")
            git_run(
                ["remote", "add", "origin",
                 f"https://github.com/{owner}/{repo_name}.git"],
                p, timeout=10,
            )
            # Task #2479: push the repo's actual default branch instead of
            # blindly trying main then master — once Equipa-repo renames
            # master -> main, a stray "master" push would recreate the old
            # name on the remote.
            # Task #2482 S1: if the first push fails and we fall back to
            # "main", we MUST check the second push's returncode too and
            # propagate failure with both stderr blobs. Returning success
            # when both pushes failed was the original HIGH finding.
            default_branch = get_default_branch(p)
            pr = git_run(
                ["push", "-u", "origin", default_branch], p, timeout=120,
            )
            if pr.returncode != 0:
                if default_branch == "main":
                    return False, (
                        f"git push failed for branch '{default_branch}': "
                        f"{pr.stderr.strip()}"
                    )
                pr2 = git_run(
                    ["push", "-u", "origin", "main"], p, timeout=120,
                )
                if pr2.returncode != 0:
                    return False, (
                        f"git push failed for both branches; "
                        f"'{default_branch}' stderr: {pr.stderr.strip()}; "
                        f"'main' stderr: {pr2.stderr.strip()}"
                    )
        else:
            return False, f"gh repo create failed: {r.stderr.strip()}"

    return True, f"Created https://github.com/{owner}/{repo_name}"


def setup_all_repos(args) -> None:
    """Initialize git + GitHub repos for all (or one) project.

    Uses --setup-repos for all, --setup-repos-project for a single project.

    NOTE: This function imports fetch_project_info from the orchestrator at
    call time to avoid circular imports during the Phase 1 split.
    """
    # Lazy import to avoid circular dependency during package split
    from equipa.tasks import fetch_project_info

    # Check prerequisites (skip for dry run)
    if not args.dry_run and not check_gh_installed():
        sys.exit(1)

    # synced storage warning
    print("\n" + "!" * 60)
    print("WARNING: Git repos in synced storage can experience corruption")
    print("from sync conflicts on the .git/index binary file.")
    print("")
    print("Recommendation: Avoid editing the same project on multiple")
    print("PCs simultaneously. The GitHub remote serves as your backup —")
    print("you can always re-clone if needed.")
    print("!" * 60)

    if not args.yes and not args.dry_run:
        response = input("\nContinue? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Determine which projects to set up
    if args.setup_repos_project:
        # Single project by ID
        project_info = fetch_project_info(args.setup_repos_project)
        if not project_info:
            print(
                f"ERROR: Project {args.setup_repos_project} not found in TheForge",
            )
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(
                f"ERROR: No directory mapped for project "
                f"'{project_info.get('name')}'",
            )
            sys.exit(1)

        targets = [(codename or pname, project_dir)]
    else:
        # All projects
        targets = list(PROJECT_DIRS.items())

    print(f"\nSetting up {len(targets)} project(s)...\n")

    results = []
    for codename, project_dir in targets:
        if not Path(project_dir).exists():
            print(
                f"  [{codename}] SKIP — directory does not exist: {project_dir}",
            )
            results.append((codename, False, "Directory does not exist"))
            continue

        print(f"  [{codename}] {project_dir}")
        success, msg = setup_single_repo(
            codename, project_dir, GITHUB_OWNER, args.dry_run,
        )
        status = "OK" if success else "FAIL"
        print(f"    -> {status}: {msg}")
        results.append((codename, success, msg))

    # Summary
    ok = sum(1 for _, s, _ in results if s)
    fail = len(results) - ok
    print(
        f"\nDone: {ok} succeeded, {fail} failed out of {len(results)} projects.",
    )
