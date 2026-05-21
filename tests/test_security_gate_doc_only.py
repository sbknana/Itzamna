"""Regression tests for task #2360 (doc-only-skip predicate).

Task #2451 Phase J: the ``TestEvaluateGate`` class was removed when
``evaluate_gate`` and ``GateVerdict`` were deleted (F-03 duplicate-gate
fix). The gate policy now lives only in ``dispatch._gated_merge_task``
and the defensive invariant in ``dispatch._merge_task_branch``; the
unit tests for that policy live under
``tests/orchestrator/test_security_gate_blocks_merge.py``. What remains
here is the pure ``is_doc_only_diff`` predicate, which is still
imported by both single-task and parallel dispatch.
"""

from __future__ import annotations

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


def test_evaluate_gate_is_deleted() -> None:
    """Task #2451 Phase J (F-03): evaluate_gate must not return.

    A single-source-of-truth gate is the architectural contract. If a
    re-introduction of ``evaluate_gate`` lands, this guard test fails
    and the reviewer must explicitly choose to delete it again (or wire
    the dispatch path through it as the only call site).
    """
    assert not hasattr(security_gate, "evaluate_gate"), (
        "evaluate_gate was deleted in Phase J to avoid a duplicate "
        "open-coded gate policy. Any re-introduction must funnel "
        "dispatch._gated_merge_task through it as the SINGLE call site."
    )
    assert not hasattr(security_gate, "GateVerdict"), (
        "GateVerdict is dead code once evaluate_gate is gone."
    )
