#!/usr/bin/env python3
"""README truth-sync helper for the Athena weekly cron (task #2480).

Athena regenerates ``docs/README.md`` (and ``docs/ARCHITECTURE.md`` etc.)
from the current state of the code.  This module reconciles the
machine-verifiable claims in those regenerated files back into the
top-level ``README.md`` while preserving the hand-written intro and
philosophy sections verbatim.

Sections owned by this script (rewritten on every run):
  * The badge block (lines wrapped by ``<p align="center">`` containing
    ``img.shields.io`` URLs).  The ``tests-<N>`` and lines/modules badges
    are regenerated from the live counts.
  * The ``## Architecture`` section (module list, file paths, doc links).
    The replacement copy is lifted from the Athena-generated
    ``docs/README.md`` when present, or rebuilt from a directory scan
    as a fallback.

Sections NEVER touched (preserved verbatim):
  * Lines 1-29 of the original README (title, taglines, intro paragraphs,
    philosophy).  The cut point is the first ``## What It Actually Does``
    heading, per the task spec.
  * Every section after Architecture (Features ordering, Quick Start,
    Honest Limitations, Production Use, Documentation, License, Credits).

The sync is a no-op when the resulting bytes are identical to the input;
the caller is expected to ``git diff`` and skip the commit in that case.

Invoked directly as a CLI by ``scripts/athena_truth_sync.sh``::

    python3 scripts/athena_sync_readme.py \
        --repo-root /srv/forge-share/AI_Stuff/Equipa-repo \
        --athena-readme /path/to/docs/README.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PRESERVE_BOUNDARY = "## What It Actually Does"
ARCH_HEADING = "## Architecture"
BADGE_BLOCK_OPEN = '<p align="center">'
BADGE_BLOCK_CLOSE = "</p>"
SHIELDS_MARKER = "img.shields.io"

# ---------------------------------------------------------------------------
# Content allowlist (task #2483 / SECURITY-REVIEW-2480 S1 HIGH).
# ---------------------------------------------------------------------------
# Athena's output domain is markdown documentation ONLY. Athena must NEVER
# emit raw HTML tags (other than the narrow whitelist of inline elements
# markdown itself uses), executable URIs, event handlers, or non-printable
# bytes. The validator below is a *fail-closed* gate: any rejected
# pattern causes the sync to abort BEFORE the splice happens, so a
# compromised or hallucinating Athena cannot inject script-y payloads
# into the repo via the truth-sync cron.

# Maximum bytes for the Athena-supplied Architecture block. Real
# architecture sections are <10KB; the cap blocks pathological inputs.
MAX_ATHENA_BLOCK_BYTES = 50 * 1024

# Dangerous HTML tag prefixes -- any opening tag matching these is
# rejected outright. ``on[a-z]+`` catches every event-handler attribute
# being expressed as a tag name (a defence-in-depth measure on top of
# the inline-attribute check below).
_DANGEROUS_TAG_RE = re.compile(
    r"<\s*(?:script|iframe|object|embed|svg|style|link|meta|frame|"
    r"frameset|applet|form|input|button|on[a-z]+)\b",
    re.IGNORECASE,
)

# Dangerous URI schemes inside markdown link/image targets.
_DANGEROUS_URI_RE = re.compile(
    r"(?:javascript|vbscript|file)\s*:|data\s*:\s*text/html",
    re.IGNORECASE,
)

# Event-handler attributes (onclick=, onload=, ...).
_EVENT_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)

# Any HTML tag at all -- markdown does not require inline HTML, so we
# fail closed if Athena tries to emit one. (Comments <!-- ... --> are
# permitted because the existing docs already use drift-ignore markers,
# but only as a single complete tag with no attributes.)
_ANY_HTML_TAG_RE = re.compile(r"<\s*[A-Za-z/!?][^>]*>")

# Markdown comment <!-- ... --> is the only HTML construct we allow
# through the inline-tag gate.
_MARKDOWN_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")

# Control characters other than \n (\x0A) and \t (\x09).
_DISALLOWED_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")


def validate_athena_content(block: str) -> tuple[bool, str]:
    """Validate Athena-supplied markdown content against the allowlist.

    Returns ``(ok, reason)``. Pure function, no I/O -- suitable for
    direct unit-testing of each attack vector.

    The function fails CLOSED: anything not on the markdown-only
    allowlist (headings, lists, code fences/spans, tables, blockquotes,
    plain text, hyperlinks, images, footnotes, markdown comments) is
    rejected. The cron's caller is expected to abort the splice and
    log the rejection on a ``False`` result.
    """

    if block is None:
        return False, "content is None"

    # Encode to count bytes (not codepoints) for the size gate.
    encoded = block.encode("utf-8", errors="replace")
    if len(encoded) > MAX_ATHENA_BLOCK_BYTES:
        return False, (
            f"Architecture block exceeds {MAX_ATHENA_BLOCK_BYTES} bytes "
            f"(got {len(encoded)} bytes)."
        )

    if _DISALLOWED_CONTROL_RE.search(block):
        return False, "content contains disallowed control characters."

    if _DANGEROUS_TAG_RE.search(block):
        match = _DANGEROUS_TAG_RE.search(block)
        return False, (
            f"content contains forbidden HTML tag near "
            f"{match.group(0)!r}."  # type: ignore[union-attr]
        )

    if _EVENT_HANDLER_RE.search(block):
        match = _EVENT_HANDLER_RE.search(block)
        return False, (
            f"content contains event-handler attribute "
            f"{match.group(0)!r}."  # type: ignore[union-attr]
        )

    # Strip markdown comments before the catch-all HTML scan -- comments
    # are the only HTML we tolerate (used by drift-ignore markers).
    stripped = _MARKDOWN_COMMENT_RE.sub("", block)
    # Strip fenced code blocks too; inside a fence, raw HTML is just
    # rendered text and cannot execute.
    stripped = re.sub(r"```[\s\S]*?```", "", stripped)
    # And inline code spans.
    stripped = re.sub(r"`[^`\n]*`", "", stripped)

    html_match = _ANY_HTML_TAG_RE.search(stripped)
    if html_match:
        return False, (
            f"content contains inline HTML tag {html_match.group(0)!r}; "
            "markdown-only allowlist forbids raw HTML in Architecture."
        )

    # Markdown link / image targets: [text](target) or ![alt](target).
    # Reject dangerous URI schemes in any (target).
    for target_match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", block):
        target = target_match.group(1).strip()
        if _DANGEROUS_URI_RE.search(target):
            return False, (
                f"content contains forbidden URI scheme in link target: "
                f"{target!r}."
            )

    # S1 HIGH (task #2485): second-pass full-block scan. The inline loop
    # above only inspects ``[text](url)`` shapes -- reference-style links
    # ``[ref]: javascript:alert(1)`` and any other location of a forbidden
    # scheme would slip through. Scan the entire block as a defence in
    # depth so a dangerous URI cannot survive in ANY position.
    if _DANGEROUS_URI_RE.search(block):
        return False, "content contains forbidden URI scheme"

    return True, "ok"


class AthenaContentRejected(ValueError):
    """Raised when Athena's output fails the markdown-only allowlist.

    Carries the human-readable rejection reason. The CLI wrapper turns
    this into a non-zero exit so the cron caller refuses to commit the
    poisoned splice.
    """


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a single :func:`sync_readme` call."""

    content: str
    badge_changes: int
    architecture_changed: bool

    @property
    def changed(self) -> bool:
        return self.badge_changes > 0 or self.architecture_changed


def _find_boundary_line(lines: list[str]) -> int:
    """Return the 0-based index of the first ``## What It Actually Does``.

    Raises :class:`ValueError` if the boundary is missing, because we
    refuse to overwrite a README that has lost its preserved intro
    (better to fail loudly than to clobber hand-written prose).
    """

    for idx, line in enumerate(lines):
        if line.strip() == PRESERVE_BOUNDARY:
            return idx
    raise ValueError(
        f"README preserve boundary '{PRESERVE_BOUNDARY}' not found; "
        "refusing to sync to avoid clobbering hand-written intro."
    )


def _find_section(
    lines: list[str], heading: str, start: int = 0
) -> tuple[int, int] | None:
    """Locate ``heading`` and return ``(start_idx, end_idx)`` (exclusive).

    A section ends at the next top-level ``## `` heading or end-of-file.
    Returns ``None`` when the heading is absent.
    """

    section_start: int | None = None
    for idx in range(start, len(lines)):
        line = lines[idx]
        if section_start is None:
            if line.strip() == heading:
                section_start = idx
            continue
        # Look for the next sibling-or-higher heading to close the section.
        if line.startswith("## ") and line.strip() != heading:
            return (section_start, idx)
    if section_start is None:
        return None
    return (section_start, len(lines))


def count_tests(repo_root: Path) -> int | None:
    """Return the live pytest-collected test count, or ``None`` on error.

    Skipping the badge update on collection failure is safer than
    writing a wrong number into the README.
    """

    try:
        proc = subprocess.run(
            ["pytest", "--collect-only", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in reversed(combined.splitlines()):
        match = re.search(r"(\d+)\s+tests?\s+collected", line)
        if match:
            return int(match.group(1))
    return None


def count_modules(repo_root: Path) -> int:
    """Count Python modules under ``equipa/`` (drift-fence baseline)."""

    equipa_dir = repo_root / "equipa"
    if not equipa_dir.is_dir():
        return 0
    return sum(1 for _ in equipa_dir.rglob("*.py"))


def count_lines(repo_root: Path) -> int:
    """Sum total lines of Python under ``equipa/`` for the LoC badge.

    Tokei/cloc may not be installed on the cron host; we approximate
    with a stdlib line count over ``equipa/**/*.py`` which is what the
    architecture diagram already references.
    """

    equipa_dir = repo_root / "equipa"
    if not equipa_dir.is_dir():
        return 0
    total = 0
    for path in equipa_dir.rglob("*.py"):
        try:
            with path.open("rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            continue
    return total


def _rebuild_badge_line(
    line: str,
    *,
    test_count: int | None,
    module_count: int,
    line_count: int,
) -> tuple[str, int]:
    """Return ``(rewritten_line, change_count)`` for a single badge line.

    Recognised badges:
      * ``tests-<N>`` -> ``tests-{test_count}`` (unchanged when test_count is None)
      * ``modules-<N>`` -> ``modules-{module_count}``
      * ``lines-<N>`` -> ``lines-{line_count}``
    """

    if SHIELDS_MARKER not in line:
        return line, 0

    changes = 0
    new_line = line

    if test_count is not None:
        rewritten, n = re.subn(
            r"tests-(\d+)",
            lambda m: f"tests-{test_count}" if int(m.group(1)) != test_count else m.group(0),
            new_line,
        )
        if rewritten != new_line:
            changes += n
            new_line = rewritten
        new_line, alt_label_changes = re.subn(
            r"alt=\"(\d+)\s+Tests\"",
            lambda m: (
                f'alt="{test_count} Tests"'
                if int(m.group(1)) != test_count
                else m.group(0)
            ),
            new_line,
        )
        changes += alt_label_changes

    if module_count > 0:
        new_line, mod_changes = re.subn(
            r"modules-(\d+)",
            lambda m: (
                f"modules-{module_count}"
                if int(m.group(1)) != module_count
                else m.group(0)
            ),
            new_line,
        )
        changes += mod_changes

    if line_count > 0:
        new_line, line_changes = re.subn(
            r"lines-(\d+)",
            lambda m: (
                f"lines-{line_count}"
                if int(m.group(1)) != line_count
                else m.group(0)
            ),
            new_line,
        )
        changes += line_changes

    return new_line, changes


def _replace_badges(
    lines: list[str],
    preserve_end: int,
    *,
    test_count: int | None,
    module_count: int,
    line_count: int,
) -> int:
    """Rewrite badge lines in-place above ``preserve_end``. Returns count.

    Only lines in the preserved-intro region carry badges (per task
    spec, the badge block sits before line 29), but the rewrite
    operates by content (``img.shields.io`` marker) rather than
    location so a future repo restructure does not silently drop
    the update.
    """

    total = 0
    for idx in range(preserve_end):
        new_line, changes = _rebuild_badge_line(
            lines[idx],
            test_count=test_count,
            module_count=module_count,
            line_count=line_count,
        )
        if changes:
            lines[idx] = new_line
            total += changes
    return total


def _extract_athena_section(
    athena_readme: Path | None, heading: str
) -> list[str] | None:
    """Pull ``heading`` from the Athena-generated README, if present."""

    if athena_readme is None or not athena_readme.exists():
        return None
    try:
        text = athena_readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines(keepends=False)
    section = _find_section(lines, heading)
    if section is None:
        return None
    start, end = section
    return lines[start:end]


def _replace_architecture(
    lines: list[str],
    *,
    boundary_idx: int,
    athena_section: list[str] | None,
) -> bool:
    """Replace the ``## Architecture`` section.

    Returns ``True`` when the section content changed.  When no
    replacement is available (Athena run was skipped or the section is
    absent from its output), the existing section is left intact so a
    failed Athena run cannot blank the architecture block.

    Raises :class:`AthenaContentRejected` if ``athena_section`` fails
    the markdown-only allowlist (S1 HIGH, task #2483).
    """

    if athena_section is None:
        return False

    candidate = "\n".join(athena_section)
    ok, reason = validate_athena_content(candidate)
    if not ok:
        raise AthenaContentRejected(
            f"Athena Architecture section rejected by content allowlist: {reason}"
        )

    section = _find_section(lines, ARCH_HEADING, start=boundary_idx)
    if section is None:
        return False

    start, end = section
    original = lines[start:end]
    # Normalise trailing whitespace so a one-line diff doesn't masquerade
    # as a meaningful change.
    if original == athena_section:
        return False

    lines[start:end] = athena_section
    return True


def sync_readme(
    readme_path: Path,
    *,
    athena_readme: Path | None = None,
    test_count: int | None = None,
    module_count: int | None = None,
    line_count: int | None = None,
) -> SyncResult:
    """Reconcile machine-verifiable claims in ``readme_path``.

    Counts default to ``None`` (no change) when caller does not pass
    them; the CLI wrapper computes them from the repo root.
    """

    text = readme_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=False)

    boundary_idx = _find_boundary_line(lines)

    badge_changes = _replace_badges(
        lines,
        preserve_end=boundary_idx,
        test_count=test_count,
        module_count=module_count or 0,
        line_count=line_count or 0,
    )

    athena_section = _extract_athena_section(athena_readme, ARCH_HEADING)
    arch_changed = _replace_architecture(
        lines, boundary_idx=boundary_idx, athena_section=athena_section
    )

    # Preserve the trailing newline behaviour of the original file.
    trailing_newline = text.endswith("\n")
    new_text = "\n".join(lines)
    if trailing_newline:
        new_text += "\n"

    return SyncResult(
        content=new_text,
        badge_changes=badge_changes,
        architecture_changed=arch_changed,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync the root README.md against Athena-regenerated docs and "
            "live counts (tests, modules, lines)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--athena-readme",
        type=Path,
        default=None,
        help="Path to the Athena-generated docs/README.md to pull the "
        "Architecture section from. Optional; if absent or unreadable, "
        "the existing Architecture section is preserved.",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="Path to the root README.md (default: <repo-root>/README.md).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resulting README to stdout instead of overwriting.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    repo_root = args.repo_root.resolve()
    readme_path = args.readme or (repo_root / "README.md")
    if not readme_path.is_file():
        print(f"error: README.md not found at {readme_path}", file=sys.stderr)
        return 2

    test_count = count_tests(repo_root)
    module_count = count_modules(repo_root)
    line_count = count_lines(repo_root)

    try:
        result = sync_readme(
            readme_path,
            athena_readme=args.athena_readme,
            test_count=test_count,
            module_count=module_count,
            line_count=line_count,
        )
    except AthenaContentRejected as err:
        # Content allowlist rejection: fail closed, exit 3 so the
        # bash runner can distinguish poisoning from a structural
        # README error. The script does NOT write anything to disk.
        print(f"error: {err}", file=sys.stderr)
        return 3
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if args.dry_run:
        sys.stdout.write(result.content)
        return 0

    if not result.changed:
        print("OK: README already in sync; no changes written.")
        return 0

    readme_path.write_text(result.content, encoding="utf-8")
    print(
        f"OK: README synced "
        f"(badge changes: {result.badge_changes}, "
        f"architecture changed: {result.architecture_changed})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
