"""Regression tests for task #2360.

Three behaviors:
  1. Doc-only diffs (only .md/.txt/.rst files changed) skip security gate.
  2. Diffs containing code files still gate normally.
  3. A gating verdict without a saved SECURITY-REVIEW-NNNN.md artifact is
     treated as a reviewer failure (NEVER a silent block).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa import security_gate


CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
    ".sh", ".sql", ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift",
    ".kt", ".scala", ".lua", ".pl", ".r", ".m", ".mm",
}


class TestIsDocOnlyDiff:
    def test_md_only_is_doc_only(self) -> None:
        assert security_gate.is_doc_only_diff(["README.md", "docs/spec.md"]) is True

    def test_mixed_md_and_txt_is_doc_only(self) -> None:
        assert security_gate.is_doc_only_diff(["README.md", "NOTES.txt", "x.rst"]) is True

    def test_single_py_file_is_not_doc_only(self) -> None:
        assert security_gate.is_doc_only_diff(["src/foo.py"]) is False

    def test_md_plus_py_is_not_doc_only(self) -> None:
        assert security_gate.is_doc_only_diff(["README.md", "src/foo.py"]) is False

    def test_empty_diff_is_not_doc_only(self) -> None:
        # No files changed at all is a degenerate case; treat as not-doc-only
        # so callers do not skip a gate they actually want to run.
        assert security_gate.is_doc_only_diff([]) is False

    @pytest.mark.parametrize("ext", sorted(CODE_EXTENSIONS))
    def test_each_code_extension_blocks_skip(self, ext: str) -> None:
        assert security_gate.is_doc_only_diff([f"file{ext}"]) is False

    def test_unknown_extension_is_treated_as_code(self) -> None:
        # Conservative: unknown extension => not doc-only, run the gate.
        assert security_gate.is_doc_only_diff(["Makefile", "src/x.weird"]) is False


class TestEvaluateGate:
    def test_docs_only_diff_skips_security_gate(self, tmp_path: Path) -> None:
        verdict = security_gate.evaluate_gate(
            task_id=2358,
            changed_files=["CRYPTOTRADER-V3-ARCHITECTURE.md"],
            reviewer_verdict="BLOCK",
            reviewer_findings_high=1,
            reviewer_findings_critical=0,
            artifact_dir=tmp_path,
        )
        assert verdict.gating is False
        assert verdict.reason == "doc-only change — security gate skipped"
        assert verdict.escalate is False

    def test_code_diff_still_gated(self, tmp_path: Path) -> None:
        # Write the artifact so artifact-missing rule does not fire.
        (tmp_path / "SECURITY-REVIEW-2400.md").write_text("# review\n1 HIGH finding\n")
        verdict = security_gate.evaluate_gate(
            task_id=2400,
            changed_files=["src/foo.py"],
            reviewer_verdict="BLOCK",
            reviewer_findings_high=1,
            reviewer_findings_critical=0,
            artifact_dir=tmp_path,
        )
        assert verdict.gating is True
        assert verdict.escalate is False

    def test_gating_verdict_without_artifact_is_reviewer_failure(
        self, tmp_path: Path
    ) -> None:
        # Reviewer says BLOCK but did NOT save an artifact for the operator.
        verdict = security_gate.evaluate_gate(
            task_id=2401,
            changed_files=["src/foo.py"],
            reviewer_verdict="BLOCK",
            reviewer_findings_high=1,
            reviewer_findings_critical=0,
            artifact_dir=tmp_path,
        )
        assert verdict.escalate is True
        assert "artifact" in verdict.reason.lower()

    def test_passing_verdict_does_not_require_artifact(self, tmp_path: Path) -> None:
        verdict = security_gate.evaluate_gate(
            task_id=2402,
            changed_files=["src/foo.py"],
            reviewer_verdict="PASS",
            reviewer_findings_high=0,
            reviewer_findings_critical=0,
            artifact_dir=tmp_path,
        )
        assert verdict.gating is False
        assert verdict.escalate is False

    def test_doc_only_skip_takes_precedence_over_missing_artifact(
        self, tmp_path: Path
    ) -> None:
        # Even if reviewer claims BLOCK with no artifact, doc-only short-circuits.
        verdict = security_gate.evaluate_gate(
            task_id=2358,
            changed_files=["docs/spec.md"],
            reviewer_verdict="BLOCK",
            reviewer_findings_high=1,
            reviewer_findings_critical=0,
            artifact_dir=tmp_path,
        )
        assert verdict.gating is False
        assert verdict.escalate is False
