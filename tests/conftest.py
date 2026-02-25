"""Shared test fixtures for FORGE engine tests."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.schemas import (
    AuditFinding,
    AuditPassType,
    CategoryScore,
    CodebaseMap,
    CoderFixResult,
    DebtEntry,
    EscalationAction,
    EscalationDecision,
    FileEntry,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeCodeReviewResult,
    ForgeExecutionState,
    IntegrationValidationResult,
    InnerLoopState,
    ProductionReadinessReport,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
    ReviewDecision,
    TestGeneratorResult,
    TriageDecision,
    TriageResult,
)


# ── Schema Factories ─────────────────────────────────────────────────


def make_finding(
    id: str = "F-test0001",
    title: str = "Test Finding",
    description: str = "Test description",
    category: str = "security",
    severity: str = "high",
    **kwargs,
) -> AuditFinding:
    return AuditFinding(
        id=id,
        title=title,
        description=description,
        category=FindingCategory(category),
        severity=FindingSeverity(severity),
        locations=kwargs.pop("locations", [FindingLocation(file_path="src/app.ts")]),
        **kwargs,
    )


def make_codebase_map(**overrides) -> CodebaseMap:
    defaults = {
        "files": [FileEntry(path="src/app.ts", language="typescript", loc=100)],
        "loc_total": 100,
        "file_count": 1,
        "primary_language": "typescript",
        "languages": ["typescript"],
    }
    defaults.update(overrides)
    return CodebaseMap(**defaults)


def make_coder_fix_result(
    finding_id: str = "F-test0001",
    outcome: FixOutcome = FixOutcome.COMPLETED,
    **overrides,
) -> CoderFixResult:
    defaults = {
        "finding_id": finding_id,
        "outcome": outcome,
        "files_changed": ["src/app.ts"],
        "summary": "Fixed the issue",
    }
    defaults.update(overrides)
    return CoderFixResult(**defaults)


def make_test_generator_result(
    finding_id: str = "F-test0001",
    **overrides,
) -> TestGeneratorResult:
    defaults = {
        "finding_id": finding_id,
        "test_files_created": ["tests/test_app.ts"],
        "tests_written": 3,
        "tests_passing": 3,
    }
    defaults.update(overrides)
    return TestGeneratorResult(**defaults)


def make_review_result(
    finding_id: str = "F-test0001",
    decision: str = "APPROVE",
    **overrides,
) -> ForgeCodeReviewResult:
    defaults = {
        "finding_id": finding_id,
        "decision": ReviewDecision(decision),
        "summary": "Looks good",
    }
    defaults.update(overrides)
    return ForgeCodeReviewResult(**defaults)


def make_remediation_item(
    finding_id: str = "F-test0001",
    tier: RemediationTier = RemediationTier.TIER_2,
    priority: int = 1,
    **overrides,
) -> RemediationItem:
    defaults = {
        "finding_id": finding_id,
        "title": "Fix test issue",
        "tier": tier,
        "priority": priority,
        "estimated_files": 1,
    }
    defaults.update(overrides)
    return RemediationItem(**defaults)


def make_remediation_plan(
    items: list[RemediationItem] | None = None,
    **overrides,
) -> RemediationPlan:
    if items is None:
        items = [make_remediation_item()]
    defaults = {
        "items": items,
        "execution_levels": [[i.finding_id for i in items]],
        "total_items": len(items),
    }
    defaults.update(overrides)
    return RemediationPlan(**defaults)


def make_inner_loop_state(
    finding_id: str = "F-test0001",
    iteration: int = 1,
    outcome: FixOutcome = FixOutcome.COMPLETED,
    decision: str = "APPROVE",
) -> InnerLoopState:
    return InnerLoopState(
        finding_id=finding_id,
        iteration=iteration,
        coder_result=make_coder_fix_result(finding_id, outcome),
        review_result=make_review_result(finding_id, decision),
        test_result=make_test_generator_result(finding_id),
    )


def make_execution_state(
    repo_path: str = "/tmp/test-repo",
    **overrides,
) -> ForgeExecutionState:
    defaults = {
        "repo_path": repo_path,
        "artifacts_dir": "/tmp/test-artifacts",
    }
    defaults.update(overrides)
    return ForgeExecutionState(**defaults)


def make_readiness_report(**overrides) -> ProductionReadinessReport:
    defaults = {
        "overall_score": 75,
        "findings_total": 10,
        "findings_fixed": 7,
        "findings_deferred": 3,
        "summary": "Good progress",
        "category_scores": [
            CategoryScore(name="Security", score=80, weight=0.3),
            CategoryScore(name="Quality", score=70, weight=0.3),
            CategoryScore(name="Architecture", score=75, weight=0.4),
        ],
        "recommendations": ["Add more tests", "Fix remaining debt"],
        "debt_items": [
            DebtEntry(
                title="Unresolved issue",
                description="Needs manual fix",
                severity=FindingSeverity.MEDIUM,
                reason_deferred="Too complex for automation",
            ),
        ],
    }
    defaults.update(overrides)
    return ProductionReadinessReport(**defaults)


def make_triage_result(**overrides) -> TriageResult:
    defaults = {
        "decisions": [
            TriageDecision(
                finding_id="F-test0001",
                tier=RemediationTier.TIER_2,
                confidence=0.9,
                rationale="Scoped fix",
            ),
        ],
        "tier_0_count": 0,
        "tier_1_count": 0,
        "tier_2_count": 1,
        "tier_3_count": 0,
    }
    defaults.update(overrides)
    return TriageResult(**defaults)


# ── Mock Helpers ──────────────────────────────────────────────────────


def make_mock_app_call(*responses):
    """Create a mock AgentField app with sequenced call responses.

    Usage:
        mock_app = make_mock_app_call(
            coder_result.model_dump(),
            test_result.model_dump(),
            review_result.model_dump(),
        )
    """
    app = MagicMock()
    app.call = AsyncMock(side_effect=list(responses))
    return app


@pytest.fixture
def forge_config():
    """Default ForgeConfig for tests."""
    return ForgeConfig(max_inner_retries=3, max_middle_escalations=2, max_outer_replans=1)


@pytest.fixture
def mock_app():
    """Mock AgentField app."""
    app = MagicMock()
    app.call = AsyncMock()
    return app


# ── Live Test Infrastructure ─────────────────────────────────────────


def pytest_addoption(parser):
    """Add --run-live flag for live integration tests."""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run live E2E tests against real AgentField + LLM APIs",
    )


def pytest_configure(config):
    """Register the 'live' marker."""
    config.addinivalue_line(
        "markers",
        "live: mark test as a live E2E test (requires --run-live or FORGE_LIVE_TESTS=1)",
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests marked @pytest.mark.live unless opted in."""
    run_live = config.getoption("--run-live", default=False)
    env_live = os.environ.get("FORGE_LIVE_TESTS", "0") == "1"

    if run_live or env_live:
        return  # do not skip

    skip_live = pytest.mark.skip(reason="Live tests require --run-live or FORGE_LIVE_TESTS=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def skip_unless_live(request):
    """Fixture that skips the test unless live mode is enabled.

    Use as a parameter in any test that requires a live AgentField instance.
    Also validates that required environment variables are set.
    """
    run_live = request.config.getoption("--run-live", default=False)
    env_live = os.environ.get("FORGE_LIVE_TESTS", "0") == "1"

    if not (run_live or env_live):
        pytest.skip("Live tests require --run-live or FORGE_LIVE_TESTS=1")

    # Validate required env vars
    missing = []
    if not os.environ.get("AGENTFIELD_SERVER"):
        missing.append("AGENTFIELD_SERVER")
    if not os.environ.get("OPENROUTER_API_KEY"):
        missing.append("OPENROUTER_API_KEY")

    if missing:
        pytest.skip(f"Missing required env vars: {', '.join(missing)}")
