"""Tests for FORGE middle/outer loop control paths.

Covers SPLIT escalation execution, regression check override,
tier 3 routing, and heuristic escalation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.execution.forge_executor import (
    _execute_single_fix,
    _heuristic_escalation,
    run_inner_loop,
)
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    EscalationAction,
    EscalationDecision,
    FindingCategory,
    FindingSeverity,
    FixOutcome,
    ForgeCodeReviewResult,
    ForgeExecutionState,
    InnerLoopState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
    ReviewDecision,
)


def _make_finding(fid="F-001", severity=FindingSeverity.HIGH):
    return AuditFinding(
        id=fid,
        title=f"Test finding {fid}",
        description="Test description",
        category=FindingCategory.SECURITY,
        severity=severity,
    )


def _make_item(fid="F-001", tier=RemediationTier.TIER_2):
    return RemediationItem(
        finding_id=fid,
        title=f"Fix {fid}",
        tier=tier,
        priority=1,
    )


def _make_inner_state(fid="F-001", outcome=FixOutcome.FAILED_RETRYABLE):
    return InnerLoopState(
        finding_id=fid,
        iteration=3,
        coder_result=CoderFixResult(
            finding_id=fid,
            outcome=outcome,
            summary="Coder attempt",
        ),
        review_result=ForgeCodeReviewResult(
            finding_id=fid,
            decision=ReviewDecision.REQUEST_CHANGES,
            summary="Needs more work",
        ),
    )


# Patch targets for lazy imports inside forge_executor
_WORKTREE = "forge.execution.worktree"
_TEST_RUNNER = "forge.execution.test_runner"


class TestSplitEscalation:
    """Bug 1: SPLIT escalation should execute sub-items instead of being dropped."""

    def test_split_escalation_executes(self):
        """SPLIT handler creates synthetic findings and runs inner loop for each."""
        finding = _make_finding()
        item = _make_item()
        state = ForgeExecutionState()
        state.all_findings = [finding]
        state.repo_path = "/tmp/test-repo"
        cfg = ForgeConfig()

        split_decision = EscalationDecision(
            finding_id="F-001",
            action=EscalationAction.SPLIT,
            rationale="Splitting into sub-items",
            split_items=[
                RemediationItem(
                    finding_id="F-001-split-1",
                    title="Sub-fix 1",
                    tier=RemediationTier.TIER_2,
                    priority=1,
                ),
                RemediationItem(
                    finding_id="F-001-split-2",
                    title="Sub-fix 2",
                    tier=RemediationTier.TIER_2,
                    priority=2,
                ),
            ],
        )

        completed_inner = InnerLoopState(
            finding_id="F-001-split-1",
            coder_result=CoderFixResult(
                finding_id="F-001-split-1",
                outcome=FixOutcome.COMPLETED,
                summary="Fixed",
            ),
        )

        app = AsyncMock()

        # Patch at the module where _execute_single_fix imports from
        with patch(f"{_WORKTREE}.create_worktree", return_value="/tmp/test-repo"), \
             patch(f"{_WORKTREE}.remove_worktree"), \
             patch(f"{_WORKTREE}.merge_worktree", return_value="clean"), \
             patch(f"{_WORKTREE}.get_current_branch", return_value="main"), \
             patch("forge.execution.forge_executor.run_inner_loop") as mock_inner, \
             patch("forge.execution.forge_executor.run_middle_loop", return_value=split_decision):

            failed_inner = _make_inner_state()
            mock_inner.side_effect = [failed_inner, completed_inner, completed_inner]

            asyncio.run(_execute_single_fix(
                app, "node", item, finding, state, cfg, cfg.resolved_models(),
            ))

        assert "F-001" in state.outer_loop.deferred_findings
        assert len(state.completed_fixes) >= 1

    def test_split_defers_failed_sub_items(self):
        """Failed split sub-items should be added to deferred findings."""
        finding = _make_finding()
        item = _make_item()
        state = ForgeExecutionState()
        state.all_findings = [finding]
        state.repo_path = "/tmp/test-repo"
        cfg = ForgeConfig()

        split_decision = EscalationDecision(
            finding_id="F-001",
            action=EscalationAction.SPLIT,
            split_items=[
                RemediationItem(
                    finding_id="F-001-split-1",
                    title="Sub-fix 1",
                    tier=RemediationTier.TIER_2,
                    priority=1,
                ),
            ],
        )

        failed_inner = _make_inner_state("F-001-split-1", FixOutcome.FAILED_RETRYABLE)

        app = AsyncMock()
        with patch(f"{_WORKTREE}.create_worktree", return_value="/tmp/test-repo"), \
             patch(f"{_WORKTREE}.remove_worktree"), \
             patch(f"{_WORKTREE}.get_current_branch", return_value="main"), \
             patch("forge.execution.forge_executor.run_inner_loop") as mock_inner, \
             patch("forge.execution.forge_executor.run_middle_loop", return_value=split_decision):

            first_inner = _make_inner_state()
            mock_inner.side_effect = [first_inner, failed_inner]

            asyncio.run(_execute_single_fix(
                app, "node", item, finding, state, cfg, cfg.resolved_models(),
            ))

        assert "F-001-split-1" in state.outer_loop.deferred_findings


class TestRegressionCheck:
    """Bug 2: Regression check should override APPROVE when existing tests fail."""

    def test_regression_overrides_approve(self):
        """When existing tests fail as code_bug, APPROVE should become REQUEST_CHANGES."""
        finding = _make_finding()
        item = _make_item()
        cfg = ForgeConfig(enable_regression_check=True, max_inner_retries=1)

        mock_test_exec = MagicMock()
        mock_test_exec.success = False
        mock_test_exec.tests_run = 10
        mock_test_exec.tests_passed = 7
        mock_test_exec.tests_failed = 3
        mock_test_exec.error_output = "AssertionError: expected 200 got 500"

        coder_result = CoderFixResult(
            finding_id="F-001",
            outcome=FixOutcome.COMPLETED,
            files_changed=["src/app.py"],
            summary="Fixed auth issue",
        )

        app = AsyncMock()
        app.call.side_effect = [
            coder_result.model_dump(),
            {"finding_id": "F-001", "test_file_contents": []},
            {"finding_id": "F-001", "decision": "APPROVE", "summary": "LGTM"},
        ]

        with patch(f"{_TEST_RUNNER}.detect_test_framework", return_value="pytest"), \
             patch(f"{_TEST_RUNNER}.run_tests_in_worktree", return_value=mock_test_exec), \
             patch("forge.execution.forge_executor._classify_test_failure", return_value="code_bug"):

            result = asyncio.run(run_inner_loop(
                app, "node", item, finding, "/tmp/repo", None, cfg, cfg.resolved_models(),
            ))

        assert result.review_result.decision == ReviewDecision.REQUEST_CHANGES
        assert "regress" in result.regression_summary.lower()

    def test_regression_ignores_environment(self):
        """Environment noise in regression check should not affect decision."""
        finding = _make_finding()
        item = _make_item()
        cfg = ForgeConfig(enable_regression_check=True, max_inner_retries=1)

        mock_test_exec = MagicMock()
        mock_test_exec.success = False
        mock_test_exec.tests_run = 10
        mock_test_exec.tests_passed = 9
        mock_test_exec.tests_failed = 1
        mock_test_exec.error_output = "urllib3 warning deprecation"

        coder_result = CoderFixResult(
            finding_id="F-001",
            outcome=FixOutcome.COMPLETED,
            files_changed=["src/app.py"],
            summary="Fixed",
        )

        app = AsyncMock()
        app.call.side_effect = [
            coder_result.model_dump(),
            {"finding_id": "F-001", "test_file_contents": []},
            {"finding_id": "F-001", "decision": "APPROVE", "summary": "LGTM"},
        ]

        with patch(f"{_TEST_RUNNER}.detect_test_framework", return_value="pytest"), \
             patch(f"{_TEST_RUNNER}.run_tests_in_worktree", return_value=mock_test_exec), \
             patch("forge.execution.forge_executor._classify_test_failure", return_value="environment"):

            result = asyncio.run(run_inner_loop(
                app, "node", item, finding, "/tmp/repo", None, cfg, cfg.resolved_models(),
            ))

        assert result.review_result.decision == ReviewDecision.APPROVE


class TestSweafRouting:
    """All AI items should route to SWE-AF."""

    def test_ai_items_routed_to_sweaf(self):
        """All AI items (Tier 2 + Tier 3) dispatched to SWE-AF."""
        from forge.phases import _run_remediation

        state = ForgeExecutionState()
        state.all_findings = [_make_finding()]
        state.repo_path = "/tmp/test-repo"
        state.remediation_plan = RemediationPlan(
            items=[_make_item(tier=RemediationTier.TIER_3)],
            execution_levels=[["F-001"]],
            total_items=1,
        )
        cfg = ForgeConfig(sweaf_agentfield_url="http://localhost:8080")
        resolved = cfg.resolved_models()

        sweaf_results = [CoderFixResult(
            finding_id="F-001", outcome=FixOutcome.COMPLETED, summary="SWE-AF fixed",
        )]

        with patch("forge.execution.tier_router.route_plan_items", return_value=([], [_make_item(tier=RemediationTier.TIER_3)])), \
             patch("forge.execution.sweaf_bridge.execute_tier3_via_sweaf", return_value=sweaf_results) as mock_sweaf:

            asyncio.run(_run_remediation(AsyncMock(), state, cfg, resolved))

        mock_sweaf.assert_called_once()
        assert len(state.completed_fixes) == 1

    def test_sweaf_falls_back_to_forge(self):
        """SWE-AF failure triggers FORGE fallback when sweaf_fallback_to_forge=True."""
        from forge.phases import _run_remediation

        state = ForgeExecutionState()
        state.all_findings = [_make_finding()]
        state.repo_path = "/tmp/test-repo"
        state.remediation_plan = RemediationPlan(
            items=[_make_item(tier=RemediationTier.TIER_3)],
            execution_levels=[["F-001"]],
            total_items=1,
        )
        cfg = ForgeConfig(
            sweaf_agentfield_url="http://localhost:8080",
            sweaf_fallback_to_forge=True,
        )

        with patch("forge.execution.tier_router.route_plan_items", return_value=([], [_make_item(tier=RemediationTier.TIER_3)])), \
             patch("forge.execution.sweaf_bridge.execute_tier3_via_sweaf", side_effect=RuntimeError("SWE-AF down")), \
             patch("forge.phases._run_sweaf_fallback_via_forge") as mock_fallback:

            asyncio.run(_run_remediation(AsyncMock(), state, cfg, cfg.resolved_models()))

        mock_fallback.assert_called_once()


class TestHeuristicEscalation:
    """Heuristic fallback escalation logic."""

    def test_heuristic_escalation_tier2(self):
        """Tier 2 failure should RECLASSIFY to Tier 3."""
        item = _make_item(tier=RemediationTier.TIER_2)
        finding = _make_finding()

        decision = _heuristic_escalation(item, finding)
        assert decision.action == EscalationAction.RECLASSIFY
        assert decision.new_tier == RemediationTier.TIER_3

    def test_heuristic_escalation_tier3(self):
        """Tier 3 failure should DEFER as technical debt."""
        item = _make_item(tier=RemediationTier.TIER_3)
        finding = _make_finding()

        decision = _heuristic_escalation(item, finding)
        assert decision.action == EscalationAction.DEFER
