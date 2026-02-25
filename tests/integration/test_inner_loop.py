"""Integration tests for the inner control loop (coder -> review -> retry)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.execution.forge_executor import run_inner_loop
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeCodeReviewResult,
    RemediationItem,
    RemediationTier,
    ReviewDecision,
    TestGeneratorResult,
)


def _finding():
    return AuditFinding(
        id="F-inner001",
        title="Missing input validation",
        description="No validation on user input",
        category=FindingCategory.SECURITY,
        severity=FindingSeverity.HIGH,
        locations=[FindingLocation(file_path="src/handler.ts")],
    )


def _item(tier=RemediationTier.TIER_2):
    return RemediationItem(
        finding_id="F-inner001", title="Add validation",
        tier=tier, priority=1,
    )


def _cfg():
    return ForgeConfig(max_inner_retries=3)


def _resolved_models():
    return {
        "coder_tier2_model": "anthropic/claude-sonnet-4.6",
        "coder_tier3_model": "anthropic/claude-sonnet-4.6",
        "test_generator_model": "anthropic/claude-haiku-4.5",
        "code_reviewer_model": "anthropic/claude-haiku-4.5",
    }


def _coder_result(finding_id="F-inner001", outcome=FixOutcome.COMPLETED):
    return CoderFixResult(
        finding_id=finding_id, outcome=outcome,
        files_changed=["src/handler.ts"], summary="Added validation",
    ).model_dump()


def _test_result(finding_id="F-inner001"):
    return TestGeneratorResult(
        finding_id=finding_id, test_files_created=["tests/test_handler.ts"],
        tests_written=2, tests_passing=2,
    ).model_dump()


def _review_result(finding_id="F-inner001", decision=ReviewDecision.APPROVE):
    return ForgeCodeReviewResult(
        finding_id=finding_id, decision=decision, summary="Looks good",
    ).model_dump()


class TestInnerLoopApproveFirstTry:
    async def test_approve_first_try(self):
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            _coder_result(),
            _test_result(),
            _review_result(),
        ])

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 1
        assert result.coder_result.outcome == FixOutcome.COMPLETED
        assert result.review_result.decision == ReviewDecision.APPROVE
        assert app.call.call_count == 3  # coder + test + review


class TestInnerLoopRetry:
    async def test_retry_on_request_changes(self):
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            # Iteration 1: coder ok, review rejects
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.REQUEST_CHANGES),
            # Iteration 2: coder ok, review approves
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.APPROVE),
        ])

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 2
        assert result.coder_result.outcome == FixOutcome.COMPLETED
        assert result.review_result.decision == ReviewDecision.APPROVE
        assert app.call.call_count == 6


class TestInnerLoopExhausted:
    async def test_exhausted_retries(self):
        app = MagicMock()
        # All 3 iterations get REQUEST_CHANGES
        app.call = AsyncMock(side_effect=[
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.REQUEST_CHANGES),
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.REQUEST_CHANGES),
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.REQUEST_CHANGES),
        ])

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 3
        assert result.coder_result.outcome == FixOutcome.FAILED_RETRYABLE
        assert app.call.call_count == 9


class TestInnerLoopBlocked:
    async def test_blocked_exits_immediately(self):
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            _coder_result(),
            _test_result(),
            _review_result(decision=ReviewDecision.BLOCK),
        ])

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 1
        assert result.coder_result.outcome == FixOutcome.FAILED_ESCALATED
        assert result.review_result.decision == ReviewDecision.BLOCK


class TestInnerLoopCoderFailed:
    async def test_coder_failure_retries(self):
        """When coder returns FAILED_RETRYABLE, inner loop continues without calling reviewer."""
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            # Iteration 1: coder fails -> continue (no test/review)
            _coder_result(outcome=FixOutcome.FAILED_RETRYABLE),
            # Iteration 2: coder succeeds, test + review approve
            _coder_result(),
            _test_result(),
            _review_result(),
        ])

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 2
        assert result.coder_result.outcome == FixOutcome.COMPLETED
        # 1 coder (failed) + 1 coder + 1 test + 1 review = 4 calls
        assert app.call.call_count == 4


class TestInnerLoopTier3SelectsCoder:
    async def test_tier3_uses_tier3_coder(self):
        """Tier 3 items should invoke the tier3 coder reasoner."""
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            _coder_result(),
            _test_result(),
            _review_result(),
        ])

        await run_inner_loop(
            app, "forge-engine", _item(tier=RemediationTier.TIER_3), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        # First call should target tier3 coder
        first_call_args = app.call.call_args_list[0]
        assert first_call_args[0][0] == "forge-engine.run_coder_tier3"


class TestInnerLoopTier2SelectsCoder:
    async def test_tier2_uses_tier2_coder(self):
        """Tier 2 items should invoke the tier2 coder reasoner."""
        app = MagicMock()
        app.call = AsyncMock(side_effect=[
            _coder_result(),
            _test_result(),
            _review_result(),
        ])

        await run_inner_loop(
            app, "forge-engine", _item(tier=RemediationTier.TIER_2), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        first_call_args = app.call.call_args_list[0]
        assert first_call_args[0][0] == "forge-engine.run_coder_tier2"


class TestInnerLoopTestGeneratorFailure:
    async def test_test_generator_exception_continues(self):
        """When test generator raises, inner loop should still process the review."""
        app = MagicMock()
        # Coder succeeds, test generator will raise, review approves.
        # asyncio.gather with return_exceptions=True means the exception
        # is returned as a value, not raised. The inner loop handles this.
        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _coder_result()
            elif call_count == 2:
                raise RuntimeError("Test generator crashed")
            elif call_count == 3:
                return _review_result()
            return {}

        app.call = AsyncMock(side_effect=_side_effect)

        result = await run_inner_loop(
            app, "forge-engine", _item(), _finding(),
            "/tmp/worktree", None, _cfg(), _resolved_models(),
        )

        assert result.iteration == 1
        assert result.coder_result.outcome == FixOutcome.COMPLETED
        assert result.review_result.decision == ReviewDecision.APPROVE
        # test_result should be a default (failed gracefully)
        assert result.test_result.finding_id == "F-inner001"
