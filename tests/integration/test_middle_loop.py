"""Integration tests for the middle control loop (escalation decisions)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.config import ForgeConfig
from forge.execution.forge_executor import run_middle_loop, _heuristic_escalation
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    EscalationAction,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeCodeReviewResult,
    InnerLoopState,
    RemediationItem,
    RemediationTier,
    ReviewDecision,
)


def _finding(id="F-mid001"):
    return AuditFinding(
        id=id, title="Complex issue",
        description="Multi-file concern",
        category=FindingCategory.ARCHITECTURE,
        severity=FindingSeverity.HIGH,
        locations=[FindingLocation(file_path="src/app.ts")],
    )


def _item(tier=RemediationTier.TIER_2):
    return RemediationItem(
        finding_id="F-mid001", title="Fix architecture",
        tier=tier, priority=1,
    )


def _inner_state(decision=ReviewDecision.REQUEST_CHANGES):
    return InnerLoopState(
        finding_id="F-mid001",
        iteration=3,
        coder_result=CoderFixResult(
            finding_id="F-mid001", outcome=FixOutcome.FAILED_RETRYABLE,
            summary="Could not fix", files_changed=["src/app.ts"],
        ),
        review_result=ForgeCodeReviewResult(
            finding_id="F-mid001", decision=decision,
            summary="Needs broader context",
        ),
    )


class TestMiddleLoopBlockedFastPath:
    async def test_blocked_defers_immediately(self):
        app = MagicMock()
        cfg = ForgeConfig()

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(decision=ReviewDecision.BLOCK), cfg,
        )

        assert result.action == EscalationAction.DEFER
        assert "Blocked" in result.rationale
        # No LLM calls made
        app.call.assert_not_called()


class TestMiddleLoopLLMEscalation:
    async def test_reclassify_from_llm(self):
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "action": "RECLASSIFY",
            "rationale": "Needs architectural context",
            "new_tier": 3,
        })
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(), cfg, models,
        )

        assert result.action == EscalationAction.RECLASSIFY
        assert result.new_tier == RemediationTier.TIER_3

    async def test_defer_from_llm(self):
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "action": "DEFER",
            "rationale": "Too risky for automated fix",
        })
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(), cfg, models,
        )

        assert result.action == EscalationAction.DEFER

    async def test_split_from_llm(self):
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "action": "SPLIT",
            "rationale": "Two distinct issues bundled together",
            "split_items": [
                {"title": "Fix auth", "estimated_files": 1},
                {"title": "Fix validation", "estimated_files": 2},
            ],
        })
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(), cfg, models,
        )

        assert result.action == EscalationAction.SPLIT
        assert len(result.split_items) == 2

    async def test_escalate_from_llm(self):
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "action": "ESCALATE",
            "rationale": "Requires human review due to security implications",
        })
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(), cfg, models,
        )

        assert result.action == EscalationAction.ESCALATE

    async def test_unknown_action_defaults_to_defer(self):
        app = MagicMock()
        app.call = AsyncMock(return_value={
            "action": "UNKNOWN_ACTION",
            "rationale": "Something unexpected",
        })
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(), _finding(),
            _inner_state(), cfg, models,
        )

        assert result.action == EscalationAction.DEFER


class TestMiddleLoopHeuristicFallback:
    async def test_llm_failure_falls_back(self):
        app = MagicMock()
        app.call = AsyncMock(side_effect=Exception("LLM error"))
        cfg = ForgeConfig()
        models = {"fix_strategist_model": "anthropic/claude-haiku-4.5"}

        result = await run_middle_loop(
            app, "forge-engine", _item(RemediationTier.TIER_2), _finding(),
            _inner_state(), cfg, models,
        )

        # Heuristic: Tier 2 -> RECLASSIFY to Tier 3
        assert result.action == EscalationAction.RECLASSIFY
        assert result.new_tier == RemediationTier.TIER_3

    async def test_no_resolved_models_uses_heuristic(self):
        app = MagicMock()
        cfg = ForgeConfig()

        result = await run_middle_loop(
            app, "forge-engine", _item(RemediationTier.TIER_3), _finding(),
            _inner_state(), cfg,
            # resolved_models defaults to None -> heuristic path
        )

        # Heuristic: Tier 3 -> DEFER
        assert result.action == EscalationAction.DEFER
        app.call.assert_not_called()


class TestHeuristicEscalation:
    def test_tier2_reclassifies(self):
        result = _heuristic_escalation(_item(RemediationTier.TIER_2), _finding())
        assert result.action == EscalationAction.RECLASSIFY
        assert result.new_tier == RemediationTier.TIER_3

    def test_tier3_defers(self):
        result = _heuristic_escalation(_item(RemediationTier.TIER_3), _finding())
        assert result.action == EscalationAction.DEFER

    def test_tier1_defers(self):
        item = RemediationItem(
            finding_id="F-mid001", title="Fix",
            tier=RemediationTier.TIER_1, priority=1,
        )
        result = _heuristic_escalation(item, _finding())
        assert result.action == EscalationAction.DEFER

    def test_tier0_defers(self):
        item = RemediationItem(
            finding_id="F-mid001", title="Fix",
            tier=RemediationTier.TIER_0, priority=1,
        )
        result = _heuristic_escalation(item, _finding())
        assert result.action == EscalationAction.DEFER
