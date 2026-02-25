"""Integration tests for the outer control loop (replan)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.config import ForgeConfig
from forge.execution.forge_executor import run_outer_loop
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    CoderFixResult,
    EscalationAction,
    EscalationDecision,
    FindingCategory,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    OuterLoopState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
)


def _state_with_escalation():
    state = ForgeExecutionState(repo_path="/tmp/test")
    state.codebase_map = CodebaseMap(loc_total=100, file_count=1)
    state.all_findings = [
        AuditFinding(
            id="F-esc001", title="Architecture issue",
            description="Needs refactoring",
            category=FindingCategory.ARCHITECTURE,
            severity=FindingSeverity.HIGH,
        ),
    ]
    state.outer_loop.escalations.append(
        EscalationDecision(
            finding_id="F-esc001",
            action=EscalationAction.ESCALATE,
            rationale="Fundamental architecture issue",
        )
    )
    return state


def _resolved_models():
    return {"fix_strategist_model": "anthropic/claude-haiku-4.5"}


def _cfg():
    return ForgeConfig(max_outer_replans=1)


class TestOuterLoopReplan:
    async def test_triggers_replan(self):
        state = _state_with_escalation()
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "items": [
                {"finding_id": "F-esc001", "title": "Refactor", "tier": 3, "priority": 1},
            ],
            "execution_levels": [["F-esc001"]],
            "total_items": 1,
        })

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        assert plan is not None
        assert len(plan.items) == 1
        assert plan.items[0].finding_id == "F-esc001"
        assert plan.items[0].tier == RemediationTier.TIER_3
        assert state.outer_loop.iteration == 1

    async def test_replan_calls_fix_strategist(self):
        state = _state_with_escalation()
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "items": [
                {"finding_id": "F-esc001", "title": "Refactor", "tier": 3, "priority": 1},
            ],
            "execution_levels": [["F-esc001"]],
            "total_items": 1,
        })

        await run_outer_loop(app, "forge-engine", state, _cfg(), _resolved_models())

        app.call.assert_called_once()
        call_args = app.call.call_args
        assert call_args[0][0] == "forge-engine.run_fix_strategist"


class TestOuterLoopNoEscalations:
    async def test_no_replan_without_escalations(self):
        state = ForgeExecutionState(repo_path="/tmp/test")
        state.codebase_map = CodebaseMap()
        app = MagicMock()

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        assert plan is None
        app.call.assert_not_called()

    async def test_no_replan_with_non_escalate_actions(self):
        """Only ESCALATE actions trigger replan, not DEFER/RECLASSIFY/SPLIT."""
        state = ForgeExecutionState(repo_path="/tmp/test")
        state.codebase_map = CodebaseMap()
        state.all_findings = [
            AuditFinding(
                id="F-def001", title="Deferred issue",
                description="Was deferred",
                category=FindingCategory.QUALITY,
                severity=FindingSeverity.MEDIUM,
            ),
        ]
        state.outer_loop.escalations.append(
            EscalationDecision(
                finding_id="F-def001",
                action=EscalationAction.DEFER,
                rationale="Deferred as tech debt",
            )
        )
        app = MagicMock()

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        assert plan is None
        app.call.assert_not_called()


class TestOuterLoopMaxReplans:
    async def test_max_replans_reached(self):
        state = _state_with_escalation()
        state.outer_loop.iteration = 1  # Already replanned once
        app = MagicMock()

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        assert plan is None
        app.call.assert_not_called()


class TestOuterLoopNoRemainingFindings:
    async def test_all_completed_no_replan(self):
        state = _state_with_escalation()
        state.completed_fixes.append(
            CoderFixResult(
                finding_id="F-esc001",
                outcome=FixOutcome.COMPLETED,
                files_changed=["src/app.ts"],
                summary="Fixed",
            )
        )
        app = MagicMock()

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        # Iteration is incremented but no remaining findings -> None
        assert plan is None

    async def test_all_deferred_no_replan(self):
        state = _state_with_escalation()
        state.outer_loop.deferred_findings.append("F-esc001")
        app = MagicMock()

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        assert plan is None


class TestOuterLoopParseFailure:
    async def test_bad_replan_response_returns_none(self):
        state = _state_with_escalation()
        app = MagicMock()
        app.call = AsyncMock(return_value={"invalid": "data"})

        plan = await run_outer_loop(
            app, "forge-engine", state, _cfg(), _resolved_models(),
        )

        # RemediationPlan(**{"invalid": "data"}) should still parse
        # (all fields have defaults) but items will be empty.
        # The function returns the plan object regardless --
        # it's the caller (execute_remediation) that checks `plan.items`.
        # So this returns a plan with empty items.
        assert plan is not None
        assert len(plan.items) == 0
