"""Integration tests for the full 3-loop remediation orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.execution.forge_executor import execute_remediation
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    CoderFixResult,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeCodeReviewResult,
    ForgeExecutionState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
    ReviewDecision,
    TestGeneratorResult,
)


def _state_with_plan():
    state = ForgeExecutionState(repo_path="/tmp/test-repo")
    state.codebase_map = CodebaseMap(loc_total=100, file_count=1)

    f1 = AuditFinding(
        id="F-rem001", title="SQL Injection",
        description="User input concatenated into SQL",
        category=FindingCategory.SECURITY,
        severity=FindingSeverity.CRITICAL,
        locations=[FindingLocation(file_path="src/db.ts")],
    )
    f2 = AuditFinding(
        id="F-rem002", title="Missing error handling",
        description="No try-catch in API handler",
        category=FindingCategory.QUALITY,
        severity=FindingSeverity.MEDIUM,
        locations=[FindingLocation(file_path="src/api.ts")],
    )
    state.all_findings = [f1, f2]

    i1 = RemediationItem(finding_id="F-rem001", title="Fix SQL injection", tier=RemediationTier.TIER_2, priority=1)
    i2 = RemediationItem(finding_id="F-rem002", title="Add error handling", tier=RemediationTier.TIER_2, priority=2)
    state.remediation_plan = RemediationPlan(
        items=[i1, i2],
        execution_levels=[["F-rem001", "F-rem002"]],
        total_items=2,
    )

    return state


def _coder_ok(fid):
    return CoderFixResult(
        finding_id=fid, outcome=FixOutcome.COMPLETED,
        files_changed=["src/file.ts"], summary="Fixed",
    ).model_dump()


def _test_ok(fid):
    return TestGeneratorResult(
        finding_id=fid, test_files_created=["tests/t.ts"],
        tests_written=1, tests_passing=1,
    ).model_dump()


def _review_ok(fid):
    return ForgeCodeReviewResult(
        finding_id=fid, decision=ReviewDecision.APPROVE, summary="OK",
    ).model_dump()


def _review_reject(fid):
    return ForgeCodeReviewResult(
        finding_id=fid, decision=ReviewDecision.REQUEST_CHANGES, summary="Needs work",
    ).model_dump()


# Patch worktree functions at the source module since _execute_single_fix
# uses a local import from forge.execution.worktree.
@patch("forge.execution.worktree.create_worktree", return_value="/tmp/test-worktree")
@patch("forge.execution.worktree.merge_worktree", return_value="clean")
@patch("forge.execution.worktree.remove_worktree")
@patch("forge.execution.worktree.get_current_branch", return_value="main")
class TestExecuteRemediation:
    async def test_both_findings_approved(self, mock_branch, mock_remove, mock_merge, mock_create):
        state = _state_with_plan()
        cfg = ForgeConfig(max_inner_retries=3)
        models = {
            "coder_tier2_model": "anthropic/claude-sonnet-4.6",
            "test_generator_model": "anthropic/claude-haiku-4.5",
            "code_reviewer_model": "anthropic/claude-haiku-4.5",
            "fix_strategist_model": "anthropic/claude-haiku-4.5",
        }

        # Both fixes succeed on first try.
        # execute_remediation runs fixes in parallel via asyncio.gather.
        # Each fix: coder + test + review = 3 calls.
        # With 2 parallel fixes, call ordering depends on gather scheduling,
        # so we use a finding-id-aware side_effect.
        call_log = []

        async def _smart_side_effect(*args, **kwargs):
            call_log.append(args[0] if args else kwargs.get("finding", {}).get("id", "unknown"))
            fid = "F-rem001"
            # Determine finding_id from kwargs
            if "finding" in kwargs and isinstance(kwargs["finding"], dict):
                fid = kwargs["finding"].get("id", fid)
            elif "code_change" in kwargs and isinstance(kwargs["code_change"], dict):
                fid = kwargs["code_change"].get("finding_id", fid)

            reasoner = args[0] if args else ""
            if "run_coder" in reasoner:
                return _coder_ok(fid)
            elif "run_test_generator" in reasoner:
                return _test_ok(fid)
            elif "run_code_reviewer" in reasoner:
                return _review_ok(fid)
            return {}

        app = MagicMock()
        app.call = AsyncMock(side_effect=_smart_side_effect)

        await execute_remediation(app, "forge-engine", state, cfg, models)

        assert len(state.completed_fixes) == 2
        assert all(f.outcome == FixOutcome.COMPLETED for f in state.completed_fixes)

    async def test_tier0_and_tier1_skipped(self, mock_branch, mock_remove, mock_merge, mock_create):
        """Tier 0 and Tier 1 items are skipped by execute_remediation (handled by tier_router)."""
        state = ForgeExecutionState(repo_path="/tmp/test-repo")
        state.codebase_map = CodebaseMap(loc_total=50, file_count=1)
        state.all_findings = [
            AuditFinding(
                id="F-t0", title="False positive",
                description="Not an issue",
                category=FindingCategory.QUALITY,
                severity=FindingSeverity.INFO,
            ),
            AuditFinding(
                id="F-t1", title="Missing semicolon",
                description="Trivial fix",
                category=FindingCategory.QUALITY,
                severity=FindingSeverity.LOW,
            ),
        ]
        state.remediation_plan = RemediationPlan(
            items=[
                RemediationItem(finding_id="F-t0", title="Skip", tier=RemediationTier.TIER_0, priority=1),
                RemediationItem(finding_id="F-t1", title="Auto-fix", tier=RemediationTier.TIER_1, priority=2),
            ],
            execution_levels=[["F-t0", "F-t1"]],
            total_items=2,
        )

        app = MagicMock()
        app.call = AsyncMock()

        cfg = ForgeConfig(max_inner_retries=3)
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        await execute_remediation(app, "forge-engine", state, cfg, models)

        # No agent calls should be made for Tier 0/1
        app.call.assert_not_called()
        assert len(state.completed_fixes) == 0

    async def test_empty_plan_returns_early(self, mock_branch, mock_remove, mock_merge, mock_create):
        """execute_remediation returns immediately if plan is empty."""
        state = ForgeExecutionState(repo_path="/tmp/test-repo")
        state.remediation_plan = None

        app = MagicMock()
        app.call = AsyncMock()

        cfg = ForgeConfig(max_inner_retries=3)
        models = {}

        await execute_remediation(app, "forge-engine", state, cfg, models)

        app.call.assert_not_called()

    async def test_deferred_findings_skipped_in_level(self, mock_branch, mock_remove, mock_merge, mock_create):
        """Findings already in deferred_findings list are skipped."""
        state = _state_with_plan()
        state.outer_loop.deferred_findings.append("F-rem001")
        cfg = ForgeConfig(max_inner_retries=3)
        models = {
            "coder_tier2_model": "anthropic/claude-sonnet-4.6",
            "test_generator_model": "anthropic/claude-haiku-4.5",
            "code_reviewer_model": "anthropic/claude-haiku-4.5",
            "fix_strategist_model": "anthropic/claude-haiku-4.5",
        }

        async def _smart_side_effect(*args, **kwargs):
            fid = "F-rem002"
            if "finding" in kwargs and isinstance(kwargs["finding"], dict):
                fid = kwargs["finding"].get("id", fid)
            elif "code_change" in kwargs and isinstance(kwargs["code_change"], dict):
                fid = kwargs["code_change"].get("finding_id", fid)

            reasoner = args[0] if args else ""
            if "run_coder" in reasoner:
                return _coder_ok(fid)
            elif "run_test_generator" in reasoner:
                return _test_ok(fid)
            elif "run_code_reviewer" in reasoner:
                return _review_ok(fid)
            return {}

        app = MagicMock()
        app.call = AsyncMock(side_effect=_smart_side_effect)

        await execute_remediation(app, "forge-engine", state, cfg, models)

        # Only F-rem002 should be completed (F-rem001 was deferred)
        assert len(state.completed_fixes) == 1
        assert state.completed_fixes[0].finding_id == "F-rem002"
