#!/usr/bin/env python3
"""Tests for system prompt static/dynamic cache split.

Validates the PromptResult class and the cache split strategy ported from
Claude Code (prompts.ts). Ensures backward compatibility (str coercion),
boundary marker placement, and correct static/dynamic partitioning.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from equipa.constants import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from equipa.prompts import PromptResult


# ---------------------------------------------------------------------------
# PromptResult unit tests
# ---------------------------------------------------------------------------


class TestPromptResult:
    """Test the PromptResult value class."""

    def test_full_includes_boundary(self):
        """Full prompt contains the dynamic boundary marker."""
        pr = PromptResult(static_prefix="STATIC", dynamic_suffix="DYNAMIC")
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in pr.full

    def test_full_order(self):
        """Static comes before boundary, boundary comes before dynamic."""
        pr = PromptResult(static_prefix="STATIC_PART", dynamic_suffix="DYNAMIC_PART")
        full = pr.full
        static_pos = full.index("STATIC_PART")
        boundary_pos = full.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        dynamic_pos = full.index("DYNAMIC_PART")
        assert static_pos < boundary_pos < dynamic_pos

    def test_str_equals_full(self):
        """str() returns the same as .full property."""
        pr = PromptResult(static_prefix="A", dynamic_suffix="B")
        assert str(pr) == pr.full

    def test_len_equals_full_length(self):
        """len() returns length of the full prompt."""
        pr = PromptResult(static_prefix="Hello", dynamic_suffix="World")
        assert len(pr) == len(pr.full)

    def test_contains_static(self):
        """'in' operator checks against full prompt — finds static content."""
        pr = PromptResult(static_prefix="unique_static_marker", dynamic_suffix="dyn")
        assert "unique_static_marker" in pr

    def test_contains_dynamic(self):
        """'in' operator checks against full prompt — finds dynamic content."""
        pr = PromptResult(static_prefix="stat", dynamic_suffix="unique_dynamic_marker")
        assert "unique_dynamic_marker" in pr

    def test_contains_missing(self):
        """'in' operator returns False for absent content."""
        pr = PromptResult(static_prefix="A", dynamic_suffix="B")
        assert "ZZZZZ_NOT_PRESENT" not in pr

    def test_eq_same_promptresult(self):
        """Two PromptResults with identical parts are equal."""
        a = PromptResult(static_prefix="S", dynamic_suffix="D")
        b = PromptResult(static_prefix="S", dynamic_suffix="D")
        assert a == b

    def test_eq_different_static(self):
        """PromptResults with different static_prefix are not equal."""
        a = PromptResult(static_prefix="S1", dynamic_suffix="D")
        b = PromptResult(static_prefix="S2", dynamic_suffix="D")
        assert a != b

    def test_eq_different_dynamic(self):
        """PromptResults with different dynamic_suffix are not equal."""
        a = PromptResult(static_prefix="S", dynamic_suffix="D1")
        b = PromptResult(static_prefix="S", dynamic_suffix="D2")
        assert a != b

    def test_eq_string_comparison(self):
        """PromptResult == str compares against .full."""
        pr = PromptResult(static_prefix="S", dynamic_suffix="D")
        assert pr == pr.full

    def test_eq_string_mismatch(self):
        """PromptResult != unrelated string."""
        pr = PromptResult(static_prefix="S", dynamic_suffix="D")
        assert pr != "totally different"

    def test_eq_non_string_non_prompt(self):
        """PromptResult != unrelated type returns NotImplemented."""
        pr = PromptResult(static_prefix="S", dynamic_suffix="D")
        assert pr != 42
        assert pr != None  # noqa: E711

    def test_repr_includes_char_counts(self):
        """repr() shows character counts for static, dynamic, total."""
        pr = PromptResult(static_prefix="ABCDE", dynamic_suffix="FGH")
        r = repr(pr)
        assert "static=5" in r
        assert "dynamic=3" in r
        assert "total=8" in r

    def test_slots_prevent_arbitrary_attrs(self):
        """PromptResult uses __slots__ — no arbitrary attributes allowed."""
        pr = PromptResult(static_prefix="S", dynamic_suffix="D")
        with pytest.raises(AttributeError):
            pr.nonexistent_attr = "boom"

    def test_empty_parts(self):
        """PromptResult works with empty strings."""
        pr = PromptResult(static_prefix="", dynamic_suffix="")
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in pr.full
        assert str(pr) == "\n\n" + SYSTEM_PROMPT_DYNAMIC_BOUNDARY + "\n\n"

    def test_boundary_not_in_static_or_dynamic_alone(self):
        """The boundary marker is injected by .full, not stored in parts."""
        pr = PromptResult(static_prefix="stat", dynamic_suffix="dyn")
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in pr.static_prefix
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in pr.dynamic_suffix
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in pr.full


# ---------------------------------------------------------------------------
# Boundary marker constant tests
# ---------------------------------------------------------------------------


class TestBoundaryConstant:
    """Verify the boundary marker is stable and distinctive."""

    def test_boundary_is_string(self):
        assert isinstance(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, str)

    def test_boundary_not_empty(self):
        assert len(SYSTEM_PROMPT_DYNAMIC_BOUNDARY) > 0

    def test_boundary_is_distinctive(self):
        """Boundary marker uses dunder-style to avoid collisions with content."""
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY.startswith("__")
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY.endswith("__")


# ---------------------------------------------------------------------------
# build_system_prompt returns PromptResult (integration-level)
# ---------------------------------------------------------------------------


class TestBuildSystemPromptCacheSplit:
    """Test that build_system_prompt returns a PromptResult with correct split."""

    @pytest.fixture
    def minimal_task(self):
        return {
            "id": 999,
            "title": "Test task",
            "description": "A test task description",
            "project_id": 23,
            "project_name": "TestProject",
            "task_type": "feature",
            "priority": "medium",
        }

    @pytest.fixture
    def empty_context(self):
        return {}

    @pytest.fixture
    def mock_prompts_dir(self, tmp_path):
        """Create minimal prompt files for testing."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        common = prompts_dir / "_common.md"
        common.write_text("# EQUIPA Common Rules\nThese are common rules.\n")

        dev = prompts_dir / "developer.md"
        dev.write_text("# Developer Agent\nYou are a developer. Task {task_id}.\n")

        planner = prompts_dir / "planner.md"
        planner.write_text("# Planner Agent\nYou plan tasks. Project {project_id}.\n")

        evaluator = prompts_dir / "evaluator.md"
        evaluator.write_text("# Evaluator Agent\nYou evaluate tasks.\n")

        return prompts_dir

    def test_returns_prompt_result(self, minimal_task, empty_context, mock_prompts_dir):
        """build_system_prompt returns a PromptResult, not a bare string."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        assert isinstance(result, PromptResult)

    def test_static_contains_common_and_role(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """Static prefix contains _common.md and role prompt content."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        assert "EQUIPA Common Rules" in result.static_prefix
        assert "Developer Agent" in result.static_prefix

    def test_dynamic_contains_task_description(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """Dynamic suffix contains the task description."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        assert "A test task description" in result.dynamic_suffix

    def test_task_id_NOT_replaced_in_static(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """The {task_id} placeholder is kept as-is in static prefix.

        Per-task values like {task_id} and {project_id} must NOT be
        substituted in the static prefix — doing so would make every
        dispatch unique and defeat prompt caching.  The agent resolves
        these from the '## Assigned Task' block in the dynamic suffix.
        """
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        # Static still has the literal placeholder (template, not interpolated)
        assert "{task_id}" in result.static_prefix
        # The actual task ID appears in the dynamic suffix (## Assigned Task)
        assert "999" in result.dynamic_suffix

    def test_full_prompt_splits_on_boundary(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """The full prompt can be split back on the boundary marker."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        parts = result.full.split(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        assert len(parts) == 2, "Boundary should appear exactly once"

    def test_str_coercion_backward_compatible(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """str(result) works as a backward-compatible string."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(minimal_task, empty_context, "/tmp/proj")

        prompt_str = str(result)
        assert isinstance(prompt_str, str)
        assert "EQUIPA Common Rules" in prompt_str
        assert "A test task description" in prompt_str

    def test_budget_visibility_in_dynamic(
        self, minimal_task, empty_context, mock_prompts_dir
    ):
        """max_turns budget info appears in dynamic suffix, not static."""
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result = _call_build_system_prompt(
                minimal_task, empty_context, "/tmp/proj", max_turns=45
            )

        assert "45 turns" in result.dynamic_suffix
        assert "45 turns" not in result.static_prefix

    def test_same_role_same_static_across_tasks(self, empty_context, mock_prompts_dir):
        """Two DIFFERENT tasks with same role produce identical static prefixes.

        This is the critical cache-split invariant: the static prefix must be
        byte-identical across dispatches so the API can cache it globally.
        """
        task_a = {
            "id": 100, "title": "Task A", "description": "Desc A",
            "project_id": 23, "project_name": "Proj", "task_type": "feature",
        }
        task_b = {
            "id": 200, "title": "Task B", "description": "Desc B",
            "project_id": 42, "project_name": "OtherProj", "task_type": "bug",
        }
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result_a = _call_build_system_prompt(task_a, empty_context, "/tmp/proj")
            result_b = _call_build_system_prompt(task_b, empty_context, "/tmp/proj")

        # Static prefix MUST be byte-identical (enables global cache hit)
        assert result_a.static_prefix == result_b.static_prefix
        # Dynamic suffix MUST differ (different task metadata)
        assert result_a.dynamic_suffix != result_b.dynamic_suffix

    def test_different_tasks_different_dynamic(self, empty_context, mock_prompts_dir):
        """Two different tasks produce different dynamic suffixes."""
        task_a = {
            "id": 100, "title": "Task A", "description": "First task description",
            "project_id": 23, "project_name": "Proj", "task_type": "feature",
        }
        task_b = {
            "id": 100, "title": "Task B", "description": "Second task description",
            "project_id": 23, "project_name": "Proj", "task_type": "bug",
        }
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {"developer": mock_prompts_dir / "developer.md"}):
            result_a = _call_build_system_prompt(task_a, empty_context, "/tmp/proj")
            result_b = _call_build_system_prompt(task_b, empty_context, "/tmp/proj")

        assert result_a.dynamic_suffix != result_b.dynamic_suffix


# ---------------------------------------------------------------------------
# Planner and Evaluator return PromptResult
# ---------------------------------------------------------------------------


class TestPlannerEvaluatorCacheSplit:
    """Test that planner/evaluator prompts also return PromptResult."""

    @pytest.fixture
    def mock_prompts_dir(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "_common.md").write_text("# Common\n")
        (prompts_dir / "planner.md").write_text("# Planner\nProject {project_id}\n")
        (prompts_dir / "evaluator.md").write_text("# Evaluator\n")
        return prompts_dir

    def test_planner_returns_prompt_result(self, mock_prompts_dir):
        from equipa.prompts import build_planner_prompt
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {
                 "planner": mock_prompts_dir / "planner.md",
             }):
            result = build_planner_prompt(
                goal="Build feature X", project_id=23,
                project_dir="/tmp/proj", project_context={},
            )
        assert isinstance(result, PromptResult)
        assert "Common" in result.static_prefix
        assert "Planner" in result.static_prefix
        assert "Build feature X" in result.dynamic_suffix
        assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in result.full

    def test_planner_static_not_substituted(self, mock_prompts_dir):
        """Planner static prefix keeps {project_id} as literal placeholder."""
        from equipa.prompts import build_planner_prompt
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {
                 "planner": mock_prompts_dir / "planner.md",
             }):
            result = build_planner_prompt(
                goal="Plan X", project_id=99,
                project_dir="/tmp/proj", project_context={},
            )
        # {project_id} should remain in static (template placeholder)
        assert "{project_id}" in result.static_prefix
        # Actual project_id appears in dynamic (## Project Info)
        assert "99" in result.dynamic_suffix

    def test_planner_static_identical_across_projects(self, mock_prompts_dir):
        """Two planner dispatches with different project_ids have identical static."""
        from equipa.prompts import build_planner_prompt
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {
                 "planner": mock_prompts_dir / "planner.md",
             }):
            result_a = build_planner_prompt(
                goal="Plan A", project_id=10,
                project_dir="/tmp/projA", project_context={},
            )
            result_b = build_planner_prompt(
                goal="Plan B", project_id=20,
                project_dir="/tmp/projB", project_context={},
            )
        assert result_a.static_prefix == result_b.static_prefix

    def test_evaluator_returns_prompt_result(self, mock_prompts_dir):
        from equipa.prompts import build_evaluator_prompt
        with patch("equipa.prompts.PROMPTS_DIR", mock_prompts_dir), \
             patch("equipa.constants.ROLE_PROMPTS", {
                 "evaluator": mock_prompts_dir / "evaluator.md",
             }):
            result = build_evaluator_prompt(
                goal="Verify feature X", project_id=23,
                project_dir="/tmp/proj", project_context={},
                completed_tasks=[{"id": 1, "title": "Did thing"}],
                blocked_tasks=[],
            )
        assert isinstance(result, PromptResult)
        assert "Evaluator" in result.static_prefix
        assert "Verify feature X" in result.dynamic_suffix
        assert "Did thing" in result.dynamic_suffix


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _call_build_system_prompt(task, context, project_dir, **kwargs):
    """Import and call build_system_prompt with standard mocks."""
    from equipa.prompts import build_system_prompt

    # Mock late imports: detect_project_language is imported inside the function
    # from equipa.git_ops, and forgesmith may not be available.
    mock_git_ops = MagicMock()
    mock_git_ops.detect_project_language = MagicMock(return_value={"languages": []})

    mock_dispatch = MagicMock()
    mock_dispatch.is_feature_enabled = MagicMock(return_value=False)

    mock_forgesmith = MagicMock()
    mock_forgesmith.get_relevant_lessons = MagicMock(return_value=[])

    with patch.dict("sys.modules", {
        "forgesmith": mock_forgesmith,
    }), \
        patch("equipa.git_ops.detect_project_language", return_value={"languages": []}), \
        patch("equipa.dispatch.is_feature_enabled", return_value=False):
        return build_system_prompt(
            task=task,
            project_context=context,
            project_dir=project_dir,
            role="developer",
            **kwargs,
        )
