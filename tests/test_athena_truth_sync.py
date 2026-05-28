"""Tests for the Athena weekly truth-sync workflow (task #2480).

These tests cover the Python sync helper that the cron's bash runner
calls (scripts/athena_sync_readme.py).  The bash runner itself is
intentionally not unit-tested -- it is thin orchestration of well-tested
components (git, the sync helper, check_docs_drift.py) and behaviour
verification belongs in the cron-host smoke test.

What the tests assert:
  * The hand-written intro (lines 1-29 of the original README, up to the
    first "## What It Actually Does" header) is preserved verbatim.
  * Machine-verifiable sections (badges, Architecture) are rewritten
    when stale counts or an Athena-generated replacement is supplied.
  * The no-change path returns ``changed == False`` and does not touch
    the file when invoked via the CLI wrapper.
  * Missing the preserve boundary raises -- we refuse to clobber the
    intro silently.
  * The open_questions / drift-still-present path is hit when
    check_docs_drift.py still reports drift after the sync.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / "scripts" / "athena_sync_readme.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "athena_sync_readme", SYNC_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can find the module for its
    # KW_ONLY lookup (Python 3.12 importlib quirk).
    sys.modules["athena_sync_readme"] = module
    spec.loader.exec_module(module)
    return module


sync_mod = _load_module()


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------
FIXTURE_README = textwrap.dedent(
    """\
    <h1 align="center">EQUIPA</h1>

    <p align="center">
      <strong>Your AI development team.</strong>
    </p>

    <p align="center">
      <em>Hand-written tagline that must survive every sync.</em>
    </p>

    <p align="center">
      <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
      <img src="https://img.shields.io/badge/tests-100-success" alt="100 Tests">
      <img src="https://img.shields.io/badge/modules-21-blue" alt="21 Modules">
      <img src="https://img.shields.io/badge/lines-9000-blue" alt="9000 Lines">
    </p>

    ---

    A hand-written paragraph that describes EQUIPA in voice we never want a
    machine to rewrite.  This sentence is the canary -- if it ever changes
    the sync helper has overstepped its mandate.

    A second paragraph; equally precious.

    ---

    ## What It Actually Does

    Stuff happens here.  This section can be machine-touched in theory,
    but the current implementation does not.

    ## Architecture

    ```
    equipa/                    # OLD 21 modules
    |-- cli.py
    |-- dispatch.py
    ```

    ## Features

    A trailing section that the sync never touches.
    """
)


ATHENA_REPLACEMENT_README = textwrap.dedent(
    """\
    # Athena-generated README

    Intro stuff Athena writes that we ignore.

    ## Architecture

    ```
    equipa/                    # 48 modules, ~22,000 lines
    |-- cli.py
    |-- dispatch.py
    |-- new_module.py
    ```

    Updated architecture description from Athena.

    ## Other Athena Section

    We also ignore this.
    """
)


@pytest.fixture()
def readme_file(tmp_path: Path) -> Path:
    path = tmp_path / "README.md"
    path.write_text(FIXTURE_README, encoding="utf-8")
    return path


@pytest.fixture()
def athena_readme_file(tmp_path: Path) -> Path:
    path = tmp_path / "athena_README.md"
    path.write_text(ATHENA_REPLACEMENT_README, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests: hand-written sections preserved verbatim.
# ---------------------------------------------------------------------------
def test_hand_written_intro_preserved(readme_file: Path, athena_readme_file: Path):
    """Lines 1 through the boundary are byte-identical after sync."""

    original_lines = FIXTURE_README.splitlines()
    boundary = sync_mod._find_boundary_line(original_lines)

    result = sync_mod.sync_readme(
        readme_file,
        athena_readme=athena_readme_file,
        test_count=500,
        module_count=48,
        line_count=22000,
    )

    new_lines = result.content.splitlines()
    # Compare everything strictly before the "## What It Actually Does"
    # header except the badge block, which we ALLOW to mutate; the
    # hand-written prose (taglines, intro paragraphs) must be untouched.
    for idx in range(boundary):
        original = original_lines[idx]
        rewritten = new_lines[idx]
        if "img.shields.io" in original:
            continue  # badges are machine-owned
        assert original == rewritten, (
            f"line {idx + 1} mutated:\n  before: {original!r}\n  after:  {rewritten!r}"
        )

    # The canary paragraph must survive verbatim.
    assert "canary -- if it ever changes" in result.content
    assert "A second paragraph; equally precious." in result.content


def test_trailing_features_section_preserved(
    readme_file: Path, athena_readme_file: Path
):
    """Sections after Architecture are not touched."""

    result = sync_mod.sync_readme(
        readme_file,
        athena_readme=athena_readme_file,
        test_count=500,
        module_count=48,
        line_count=22000,
    )

    assert "## Features" in result.content
    assert "A trailing section that the sync never touches." in result.content


# ---------------------------------------------------------------------------
# Tests: machine-verifiable sections replaced.
# ---------------------------------------------------------------------------
def test_badges_rewritten_with_live_counts(
    readme_file: Path, athena_readme_file: Path
):
    """tests/modules/lines badges pick up the supplied live counts."""

    result = sync_mod.sync_readme(
        readme_file,
        athena_readme=athena_readme_file,
        test_count=518,
        module_count=48,
        line_count=22000,
    )

    assert "tests-518" in result.content
    assert "modules-48" in result.content
    assert "lines-22000" in result.content
    # Stale values must be gone.
    assert "tests-100" not in result.content
    assert "modules-21" not in result.content
    assert "lines-9000" not in result.content
    assert result.badge_changes >= 3


def test_alt_label_rewritten_with_test_count(
    readme_file: Path, athena_readme_file: Path
):
    """The alt text on the tests badge stays in sync with the badge."""

    result = sync_mod.sync_readme(
        readme_file,
        athena_readme=athena_readme_file,
        test_count=518,
        module_count=48,
        line_count=22000,
    )

    assert 'alt="518 Tests"' in result.content
    assert 'alt="100 Tests"' not in result.content


def test_architecture_section_replaced(
    readme_file: Path, athena_readme_file: Path
):
    """The Architecture section content is lifted from Athena's output."""

    result = sync_mod.sync_readme(
        readme_file,
        athena_readme=athena_readme_file,
        test_count=518,
        module_count=48,
        line_count=22000,
    )

    assert result.architecture_changed is True
    assert "48 modules, ~22,000 lines" in result.content
    assert "new_module.py" in result.content
    # The stale architecture description must be gone.
    assert "OLD 21 modules" not in result.content


# ---------------------------------------------------------------------------
# Tests: no-changes quiet exit.
# ---------------------------------------------------------------------------
def test_no_changes_quiet_exit_when_in_sync(tmp_path: Path):
    """When counts already match and no Athena output is supplied, the
    sync reports ``changed == False`` and the CLI returns 0 without
    rewriting the file."""

    readme = tmp_path / "README.md"
    readme.write_text(FIXTURE_README, encoding="utf-8")
    mtime_before = readme.stat().st_mtime

    proc = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--repo-root",
            str(tmp_path),
            "--readme",
            str(readme),
        ],
        capture_output=True,
        text=True,
    )
    # The repo has no equipa/ directory so module_count and line_count
    # are 0 (no-op); pytest collection is absent so test_count is None.
    # The result should be "already in sync".
    assert proc.returncode == 0, proc.stderr
    assert "already in sync" in proc.stdout
    assert readme.stat().st_mtime == mtime_before
    assert readme.read_text(encoding="utf-8") == FIXTURE_README


# ---------------------------------------------------------------------------
# Tests: missing preserve boundary aborts loudly.
# ---------------------------------------------------------------------------
def test_missing_preserve_boundary_raises(tmp_path: Path):
    """If the README has no '## What It Actually Does' header, the sync
    refuses to operate -- this prevents accidental clobbering of a
    restructured README."""

    readme = tmp_path / "README.md"
    readme.write_text("# Title only\n\nNo boundary marker here.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="preserve boundary"):
        sync_mod.sync_readme(readme, test_count=1, module_count=1, line_count=1)


# ---------------------------------------------------------------------------
# Tests: markdown-only content allowlist (task #2483 S1 HIGH).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expected_substr"),
    [
        # 1. <script> injection
        (
            "## Architecture\n\n<script>alert(1)</script>\n",
            "forbidden HTML tag",
        ),
        # 2. javascript: URI in markdown link target
        (
            "## Architecture\n\n[click](javascript:alert(1))\n",
            "forbidden URI scheme",
        ),
        # 3. onclick event handler attribute
        (
            "## Architecture\n\n<a onclick=\"x()\">x</a>\n",
            "event-handler attribute",
        ),
        # 4. Oversize block
        (
            "## Architecture\n\n" + ("A" * (50 * 1024 + 1)) + "\n",
            "exceeds",
        ),
        # 5. Control character (NUL byte) in body
        (
            "## Architecture\n\nhello\x00world\n",
            "control characters",
        ),
        # 6. Raw HTML <form> outside fence
        (
            "## Architecture\n\n<form action=\"/x\"><input name=\"y\"></form>\n",
            "forbidden HTML tag",
        ),
        # 7. data:text/html base64 image
        (
            "## Architecture\n\n![pic](data:text/html;base64,PHNjcmlwdD4=)\n",
            "forbidden URI scheme",
        ),
        # 8. Mismatched code fence breakout -- raw HTML <iframe> after a
        #    naively-paired fence closure.
        (
            "## Architecture\n\n```\nfine\n```\n<iframe src=\"x\"></iframe>\n",
            "forbidden HTML tag",
        ),
        # 9. Reference-style markdown link with javascript: target
        #    (task #2485 S1 HIGH -- escapes the inline (url) regex).
        (
            "## Architecture\n\n[r]: javascript:alert(1)\n\n[click][r]\n",
            "forbidden URI scheme",
        ),
        # 10. Reference-style link target using data:text/html
        #    (uses HTML-entity-encoded svg to avoid the inline <svg> tag
        #    check firing first -- the spec's literal "<svg onload=>" is
        #    correctly caught earlier by _DANGEROUS_TAG_RE, but the URI
        #    scheme itself must also be rejected by the second-pass scan).
        (
            "## Architecture\n\n[r]: data:text/html,foo\n",
            "forbidden URI scheme",
        ),
        # 11. Reference-style link target using vbscript:
        (
            "## Architecture\n\n[r]: vbscript:msgbox(1)\n",
            "forbidden URI scheme",
        ),
        # 12. Reference-style link target using file:// URI
        (
            "## Architecture\n\n[r]: file:///etc/passwd\n",
            "forbidden URI scheme",
        ),
    ],
)
def test_validate_athena_content_rejects_attack_vectors(payload, expected_substr):
    ok, reason = sync_mod.validate_athena_content(payload)
    assert ok is False, f"payload should have been rejected: {payload!r}"
    assert expected_substr.lower() in reason.lower(), (
        f"reason {reason!r} should mention {expected_substr!r}"
    )


def test_validate_athena_content_accepts_clean_markdown():
    """Plain markdown -- headings, lists, code fences, tables, links --
    passes the allowlist."""

    clean = textwrap.dedent(
        """\
        ## Architecture

        ```
        equipa/
        |-- cli.py
        |-- dispatch.py
        ```

        - `equipa/cli.py` -- command-line entry point
        - `equipa/dispatch.py` -- task dispatch

        | File | Purpose |
        |------|---------|
        | cli.py | entry |

        See [the README](README.md) for more.

        <!-- drift-ignore -->
        Inline `code` is fine. *Italic* and **bold** too.
        """
    )
    ok, reason = sync_mod.validate_athena_content(clean)
    assert ok is True, f"clean markdown was rejected: {reason}"


def test_architecture_replace_raises_on_poisoned_athena_section(
    readme_file: Path, tmp_path: Path
):
    """A poisoned Athena replacement aborts the sync via
    AthenaContentRejected before the splice writes anything."""

    poisoned = tmp_path / "athena_bad.md"
    poisoned.write_text(
        "## Architecture\n\n<script>alert(1)</script>\n", encoding="utf-8"
    )
    with pytest.raises(sync_mod.AthenaContentRejected):
        sync_mod.sync_readme(
            readme_file,
            athena_readme=poisoned,
            test_count=10,
            module_count=1,
            line_count=1,
        )


def test_cli_returns_rc3_on_poisoned_content(tmp_path: Path):
    """CLI wrapper exits with rc=3 (sentinel for content-allowlist
    rejection) and leaves README.md untouched."""

    readme = tmp_path / "README.md"
    readme.write_text(FIXTURE_README, encoding="utf-8")
    poisoned = tmp_path / "athena_bad.md"
    poisoned.write_text(
        "## Architecture\n\n<iframe src='x'></iframe>\n", encoding="utf-8"
    )
    mtime_before = readme.stat().st_mtime

    proc = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--repo-root",
            str(tmp_path),
            "--readme",
            str(readme),
            "--athena-readme",
            str(poisoned),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 3, (
        f"expected rc=3, got {proc.returncode}; stderr:\n{proc.stderr}"
    )
    assert "allowlist" in proc.stderr.lower() or "rejected" in proc.stderr.lower()
    # README must not have been written.
    assert readme.stat().st_mtime == mtime_before
    assert readme.read_text(encoding="utf-8") == FIXTURE_README


# ---------------------------------------------------------------------------
# Tests: drift-still-present path triggers the open_questions insert.
# ---------------------------------------------------------------------------
def test_drift_still_present_triggers_open_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end exercise of the bash runner's drift-still-present
    branch: when check_docs_drift.py exits non-zero after the sync, the
    runner must file an open_question and skip the push.

    We simulate the path by composing the relevant shell environment
    against fake binaries on PATH, so the assertion is on the
    sqlite3 INSERT issued for open_questions rather than a real git
    push.  Heavy isolation -- no network, no actual Athena, no git.
    """

    # ---- Lay out a fake repo --------------------------------------------------
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".git").mkdir()  # the runner only requires the .git directory exist
    (repo / "README.md").write_text(FIXTURE_README, encoding="utf-8")
    # Real sync script + real open_question helper. The drift checker is
    # a stub that supports --self-test (passes) and a normal run (fails),
    # so the runner's preflight succeeds but the post-sync check fails.
    real_sync = REPO_ROOT / "scripts" / "athena_sync_readme.py"
    real_open_q = REPO_ROOT / "scripts" / "athena_open_question.py"
    (repo / "scripts" / "athena_sync_readme.py").write_bytes(real_sync.read_bytes())
    (repo / "scripts" / "athena_open_question.py").write_bytes(real_open_q.read_bytes())
    (repo / "scripts" / "athena_open_question.py").chmod(0o755)
    # Drift checker stub: --self-test exits 0; normal run exits 1.
    (repo / "scripts" / "check_docs_drift.py").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys


            def main():
                if "--self-test" in sys.argv:
                    print("OK: stub self-test")
                    return 0
                print("FAIL: synthetic drift", file=sys.stdout)
                return 1


            if __name__ == "__main__":
                sys.exit(main())
            """
        ),
        encoding="utf-8",
    )
    (repo / "scripts" / "check_docs_drift.py").chmod(0o755)

    # ---- Fake git / node so the runner does not touch real infra --------------
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_log = tmp_path / "git.log"
    (fake_bin / "git").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo "$@" >> "{git_log}"
            case "$1" in
              -C) shift 2; ;;  # discard `-C <path>` prefix
            esac
            case "$1" in
              fetch|pull|checkout|push|add|reset) exit 0 ;;
              commit)
                  # Refuse unsigned commits in the shim too; the real
                  # script always passes -S. Treat success silently.
                  exit 0 ;;
              diff|status) exit 0 ;;
              symbolic-ref) echo "origin/master"; exit 0 ;;
              remote) echo "  HEAD branch: master"; exit 0 ;;
              rev-parse) exit 0 ;;
              show) exit 0 ;;
              *)
                  # Tightened shim (task #2483 S10): unrecognized
                  # subcommands FAIL LOUDLY so the test cannot silently
                  # accept new git invocations the script grew without
                  # the test author noticing.
                  echo "fake-git: unrecognized subcommand: $*" >&2
                  exit 2 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    (fake_bin / "git").chmod(0o755)
    (fake_bin / "node").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fake_bin / "node").chmod(0o755)
    # flock is needed in PATH so the runner's re-exec finds it. Use
    # the host flock if present; otherwise stub it as a passthrough.
    import shutil

    host_flock = shutil.which("flock")
    if host_flock:
        (fake_bin / "flock").symlink_to(host_flock)
    else:
        (fake_bin / "flock").write_text(
            '#!/usr/bin/env bash\n# stub: drop -n <lockfile> and exec rest\n'
            'while [[ "$1" == -* || "$1" == /* ]]; do shift; done\nexec "$@"\n',
            encoding="utf-8",
        )
        (fake_bin / "flock").chmod(0o755)

    # ---- Stand up a TheForge DB with the open_questions table -----------------
    forge_db = tmp_path / "theforge.db"
    subprocess.run(
        [
            "sqlite3",
            str(forge_db),
            (
                "CREATE TABLE open_questions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "project_id INTEGER NOT NULL, "
                "question TEXT NOT NULL, "
                "context TEXT, "
                "resolved INTEGER DEFAULT 0);"
            ),
        ],
        check=True,
    )

    # ---- Run the script -------------------------------------------------------
    runner = REPO_ROOT / "scripts" / "athena_truth_sync.sh"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "EQUIPA_REPO": str(repo),
        "ATHENA_DIR": str(tmp_path / "athena_missing"),
        "ATHENA_CLI": str(tmp_path / "athena_missing" / "cli.js"),
        "PYTHON_BIN": sys.executable,
        "THEFORGE_DB": str(forge_db),
        "EQUIPA_PROJECT_ID": "23",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, env=env
    )

    assert proc.returncode == 1, (
        f"expected runner to exit 1 on persistent drift, "
        f"got {proc.returncode}\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert "drift remains after sync" in proc.stderr

    # The open_question must have been inserted.
    inserted = subprocess.run(
        ["sqlite3", str(forge_db), "SELECT COUNT(*) FROM open_questions;"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert inserted.stdout.strip() == "1", (
        f"expected 1 open_question, got {inserted.stdout!r}; runner stderr:\n{proc.stderr}"
    )

    # ---- Explicit subcommand-sequence assertion (task #2483 S10) -------------
    # Pin the order of git invocations the runner makes BEFORE the drift
    # check fails. Any silent change to the script that reorders these
    # (e.g. moving the clean-checkout guard, the branch detection, the
    # fetch/pull) should break this assertion so the test author sees it.
    git_history = git_log.read_text(encoding="utf-8").splitlines()
    # First command after the flock re-exec should be the clean-checkout
    # `diff --quiet HEAD` guard. Then the branch detection helpers, then
    # fetch + checkout + pull. We assert prefixes so harmless flag
    # tweaks (--quiet, --ff-only) do not break the test.
    expected_prefixes = [
        "diff --quiet HEAD",
        "symbolic-ref",
        "fetch origin master",
        "checkout --quiet master",
        "pull --ff-only origin master",
    ]
    history_text = "\n".join(git_history)
    cursor = 0
    for prefix in expected_prefixes:
        idx = history_text.find(prefix, cursor)
        assert idx != -1, (
            f"expected git subcommand prefix {prefix!r} not found in order; "
            f"history was:\n{history_text}"
        )
        cursor = idx + len(prefix)
