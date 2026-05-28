#!/usr/bin/env python3
"""Documentation drift checker for the EQUIPA repository.

Scans README.md and docs/*.md for path references and count claims that
have drifted from reality. Designed as a CI fence (run on every push/PR)
and as a non-blocking pre-commit warning.

Checks performed:
  1. Markdown link targets `[text](path/to/file.ext)` resolve to existing
     files in the repository.
  2. Inline code-span paths (backtick-wrapped strings ending in a known
     source extension) resolve to existing files.
  3. Path-like entries inside ASCII tree diagrams (lines starting with
     ``|--`` or ``+--``) resolve to existing files.
  4. README claim "<N> modules" is within +/-3 of the actual count of
     .py files under equipa/.
  5. README badge ``tests-<N>`` is within +/-25 of the count reported by
     ``pytest --collect-only -q``.

Escape hatches (paths inside these are ignored):
  * Any block between an HTML comment ``<!-- drift-ignore -->`` and the
    matching ``<!-- /drift-ignore -->`` marker (or to end of file if no
    closing marker is present).
  * Any fenced code block opened with ``` ```sh-example ``` (case-
    insensitive on the language tag).

Exit codes:
  0 -- no drift detected (CI passes)
  1 -- one or more drifts detected (CI fails)
  2 -- internal/usage error

Run locally from the repo root:

    python scripts/check_docs_drift.py

Or against a different repo root:

    python scripts/check_docs_drift.py --repo-root /path/to/repo

The pre-commit hook may call this with ``--warn-only`` to keep the
result advisory (always exits 0 but still prints drifts).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".md",
    ".json",
    ".sh",
    ".yml",
    ".yaml",
    ".ts",
    ".js",
)

MODULE_COUNT_TOLERANCE = 3
TEST_COUNT_TOLERANCE = 25

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
TREE_LINE_RE = re.compile(r"^[\s|`]*(?:\|--|\+--|`--)\s*([^\s#]+)")
MODULES_CLAIM_RE = re.compile(r"(\d+)\s+modules", re.IGNORECASE)
TEST_BADGE_RE = re.compile(r"tests-(\d+)")

DRIFT_IGNORE_OPEN = "<!-- drift-ignore -->"
DRIFT_IGNORE_CLOSE = "<!-- /drift-ignore -->"
FENCE_RE = re.compile(r"^\s*```([\w-]*)\s*$")


@dataclass(frozen=True)
class Drift:
    """A single documented drift entry."""

    file: Path
    line: int
    message: str

    def format(self, repo_root: Path) -> str:
        try:
            display = self.file.relative_to(repo_root)
        except ValueError:
            display = self.file
        return f"{display}:{self.line}: {self.message}"


def _strip_ignored_regions(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, line)`` tuples with ignored regions blanked.

    Lines inside ``<!-- drift-ignore -->`` / ``</...>`` HTML comment
    pairs are replaced with empty strings, as are lines inside fenced
    code blocks opened with ``sh-example`` language tag. Line numbers
    remain 1-based and stable so they can be reported back to the user.
    """

    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    in_ignore_block = False
    in_sh_example_fence = False

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        if in_sh_example_fence:
            if FENCE_RE.match(line):
                in_sh_example_fence = False
            out.append((idx, ""))
            continue

        fence_match = FENCE_RE.match(line)
        if fence_match and fence_match.group(1).lower() == "sh-example":
            in_sh_example_fence = True
            out.append((idx, ""))
            continue

        if DRIFT_IGNORE_OPEN in stripped:
            in_ignore_block = True
            out.append((idx, ""))
            continue
        if DRIFT_IGNORE_CLOSE in stripped:
            in_ignore_block = False
            out.append((idx, ""))
            continue
        if in_ignore_block:
            out.append((idx, ""))
            continue

        out.append((idx, line))

    return out


def _looks_like_repo_path(candidate: str) -> bool:
    """Best-effort filter: does this string look like an in-repo path?"""

    if not candidate:
        return False
    if candidate.startswith(("http://", "https://", "mailto:", "ftp://")):
        return False
    if candidate.startswith("#"):
        return False
    # Exclude things that obviously aren't paths.
    if " " in candidate:
        return False
    return candidate.lower().endswith(SOURCE_EXTENSIONS)


def _resolve(repo_root: Path, doc_file: Path, raw: str) -> Path:
    """Resolve ``raw`` to an absolute path under ``repo_root``."""

    candidate = raw.strip()
    # Strip URL fragments and query strings.
    candidate = candidate.split("#", 1)[0].split("?", 1)[0]
    # Strip a single leading "./" but never the leading "../" that
    # could legitimately appear in relative links from docs/*.md.
    if candidate.startswith("./"):
        candidate = candidate[2:]

    if candidate.startswith("/"):
        # Absolute-looking paths resolve against the repo root.
        return (repo_root / candidate.lstrip("/")).resolve()
    return (doc_file.parent / candidate).resolve()


def _extract_path_references(
    doc_file: Path, lines: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """Yield ``(line_no, candidate)`` for every path-like reference."""

    refs: list[tuple[int, str]] = []
    for line_no, line in lines:
        if not line:
            continue

        for match in MARKDOWN_LINK_RE.finditer(line):
            target = match.group(1)
            if _looks_like_repo_path(target):
                refs.append((line_no, target))

        for match in INLINE_CODE_RE.finditer(line):
            target = match.group(1)
            if _looks_like_repo_path(target):
                refs.append((line_no, target))

        tree_match = TREE_LINE_RE.match(line)
        if tree_match:
            target = tree_match.group(1)
            if _looks_like_repo_path(target):
                refs.append((line_no, target))

    return refs


def check_paths(repo_root: Path, doc_files: list[Path]) -> list[Drift]:
    """Verify that every extracted path reference exists on disk."""

    drifts: list[Drift] = []
    for doc_file in doc_files:
        try:
            text = doc_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            drifts.append(Drift(doc_file, 0, f"could not read doc file: {err}"))
            continue

        lines = _strip_ignored_regions(text)
        for line_no, raw in _extract_path_references(doc_file, lines):
            resolved = _resolve(repo_root, doc_file, raw)
            if not resolved.exists():
                drifts.append(
                    Drift(
                        doc_file,
                        line_no,
                        f"referenced path does not exist: {raw}",
                    )
                )
    return drifts


def check_module_count(repo_root: Path) -> list[Drift]:
    """Compare the README's claimed module count against reality."""

    readme = repo_root / "README.md"
    if not readme.exists():
        return []

    try:
        text = readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return [Drift(readme, 0, f"could not read README: {err}")]

    lines = _strip_ignored_regions(text)
    equipa_dir = repo_root / "equipa"
    if not equipa_dir.is_dir():
        return []

    actual = sum(1 for _ in equipa_dir.rglob("*.py"))

    drifts: list[Drift] = []
    for line_no, line in lines:
        if not line:
            continue
        match = MODULES_CLAIM_RE.search(line)
        if not match:
            continue
        claimed = int(match.group(1))
        if abs(claimed - actual) > MODULE_COUNT_TOLERANCE:
            drifts.append(
                Drift(
                    readme,
                    line_no,
                    (
                        f"README claims {claimed} modules but equipa/ "
                        f"contains {actual} .py files "
                        f"(tolerance +/-{MODULE_COUNT_TOLERANCE})"
                    ),
                )
            )
    return drifts


def _collect_pytest_count(repo_root: Path) -> int | None:
    """Run ``pytest --collect-only -q`` and return collected test count.

    Returns ``None`` if pytest is unavailable or the count cannot be
    parsed -- in that case the badge check is silently skipped, which
    is safer than failing CI on transient pytest issues.
    """

    try:
        proc = subprocess.run(
            ["pytest", "--collect-only", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # pytest's summary line looks like: "1709 tests collected in 1.23s"
    # or simply "1709 tests collected" depending on version.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    last_lines = [ln for ln in combined.splitlines() if ln.strip()]
    for line in reversed(last_lines):
        match = re.search(r"(\d+)\s+tests?\s+collected", line)
        if match:
            return int(match.group(1))
    return None


def check_test_badge(
    repo_root: Path, actual_count: int | None = None
) -> list[Drift]:
    """Compare README ``tests-<N>`` badge against pytest collection."""

    readme = repo_root / "README.md"
    if not readme.exists():
        return []

    try:
        text = readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return [Drift(readme, 0, f"could not read README: {err}")]

    lines = _strip_ignored_regions(text)

    if actual_count is None:
        actual_count = _collect_pytest_count(repo_root)
    if actual_count is None:
        return []  # Skip the check rather than fail CI on pytest issues.

    drifts: list[Drift] = []
    for line_no, line in lines:
        if not line:
            continue
        match = TEST_BADGE_RE.search(line)
        if not match:
            continue
        claimed = int(match.group(1))
        if abs(claimed - actual_count) > TEST_COUNT_TOLERANCE:
            drifts.append(
                Drift(
                    readme,
                    line_no,
                    (
                        f"README badge claims tests-{claimed} but pytest "
                        f"collected {actual_count} "
                        f"(tolerance +/-{TEST_COUNT_TOLERANCE})"
                    ),
                )
            )
    return drifts


def _discover_doc_files(repo_root: Path) -> list[Path]:
    """Return README.md plus every docs/*.md found at the repo root."""

    files: list[Path] = []
    readme = repo_root / "README.md"
    if readme.exists():
        files.append(readme)
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.glob("*.md")))
    return files


def run_all_checks(
    repo_root: Path, skip_pytest: bool = False
) -> list[Drift]:
    """Execute every drift check and return the combined drift list."""

    doc_files = _discover_doc_files(repo_root)
    drifts = check_paths(repo_root, doc_files)
    drifts.extend(check_module_count(repo_root))
    if not skip_pytest:
        drifts.extend(check_test_badge(repo_root))
    return drifts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail CI when README/docs reference files that don't exist.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan (default: current working directory).",
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Print drifts but always exit 0 (for pre-commit hook use).",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip the pytest test-count badge check.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Verify the module is importable and runnable, then exit 0. "
            "Replaces the inline `python3 -c \"import ...\"` smoke test "
            "that the Athena truth-sync runner used to perform (avoids "
            "shell-quoted code interpolation into Python source)."
        ),
    )
    args = parser.parse_args(argv)

    if args.self_test:
        print("OK: check_docs_drift importable and runnable.")
        return 0

    repo_root = args.repo_root.resolve()
    if not repo_root.is_dir():
        print(f"error: repo root does not exist: {repo_root}", file=sys.stderr)
        return 2

    drifts = run_all_checks(repo_root, skip_pytest=args.skip_pytest)

    if not drifts:
        print("OK: no documentation drift detected.")
        return 0

    print(f"FAIL: detected {len(drifts)} documentation drift(s):")
    for drift in drifts:
        print(f"  - {drift.format(repo_root)}")

    if args.warn_only:
        print("(warn-only mode -- exiting 0)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
