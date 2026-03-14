"""Integration tests for SWE-AF HTTP bridge (mocked HTTP)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.execution.sweaf_bridge import execute_tier3_via_sweaf
from forge.schemas import (
    AuditFinding,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    RemediationItem,
    RemediationTier,
)


def _make_finding(fid="F-001"):
    return AuditFinding(
        id=fid,
        title=f"Finding {fid}",
        description="SQL injection vulnerability",
        category=FindingCategory.SECURITY,
        severity=FindingSeverity.HIGH,
        locations=[FindingLocation(file_path="src/db.py", line_start=10)],
    )


def _make_item(fid="F-001"):
    return RemediationItem(
        finding_id=fid,
        title=f"Fix {fid}",
        tier=RemediationTier.TIER_3,
        priority=1,
    )


def _make_state(repo_path="/tmp/test-repo"):
    state = ForgeExecutionState()
    state.repo_path = repo_path
    return state


def _make_cfg():
    return ForgeConfig(
        sweaf_enabled=True,
        sweaf_agentfield_url="http://localhost:8080",
        sweaf_api_key="test-key",
        sweaf_timeout_seconds=30,
    )


def _mock_urlopen(responses):
    """Create a mock urlopen that returns different responses on successive calls."""
    call_idx = 0

    def _urlopen(req, timeout=30):
        nonlocal call_idx
        resp_data = responses[min(call_idx, len(responses) - 1)]
        call_idx += 1
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode()
        mock_resp.status = 200
        return mock_resp

    return _urlopen


class TestSweafBridgeIntegration:
    @pytest.mark.asyncio
    async def test_execute_success(self, tmp_path):
        """Full success flow: POST + poll → completed results."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        responses = [
            # POST response
            {"execution_id": "exec-123"},
            # First poll: still running
            {"status": "running"},
            # Second poll: completed
            {
                "status": "completed",
                "result": {
                    "issues": {
                        "fix-f-001": {
                            "status": "completed",
                            "files_changed": ["src/db.py"],
                            "summary": "Parameterized queries",
                        },
                    },
                },
            },
        ]

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=_mock_urlopen(responses)), \
             patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

            results = await execute_tier3_via_sweaf([item], [finding], state, cfg)

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.COMPLETED
        assert results[0].finding_id == "F-001"

    @pytest.mark.asyncio
    async def test_execute_timeout(self, tmp_path):
        """Timeout should return FAILED_RETRYABLE for all items."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = ForgeConfig(
            sweaf_enabled=True,
            sweaf_agentfield_url="http://localhost:8080",
            sweaf_api_key="test-key",
            sweaf_timeout_seconds=0,  # Immediate timeout
        )

        responses = [
            {"execution_id": "exec-123"},
            {"status": "running"},  # Never completes
        ]

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=_mock_urlopen(responses)), \
             patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

            results = await execute_tier3_via_sweaf([item], [finding], state, cfg)

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.FAILED_RETRYABLE

    @pytest.mark.asyncio
    async def test_execute_connection_error(self, tmp_path):
        """Connection error should return FAILED_RETRYABLE."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=ConnectionError("refused")):

            results = await execute_tier3_via_sweaf([item], [finding], state, cfg)

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.FAILED_RETRYABLE
        assert "bridge error" in results[0].summary.lower()

    @pytest.mark.asyncio
    async def test_execute_partial_success(self, tmp_path):
        """Some issues complete, some fail — mixed results."""
        findings = [_make_finding("F-001"), _make_finding("F-002")]
        items = [_make_item("F-001"), _make_item("F-002")]
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        responses = [
            {"execution_id": "exec-456"},
            {
                "status": "completed",
                "result": {
                    "issues": {
                        "fix-f-001": {"status": "completed", "summary": "Fixed"},
                        "fix-f-002": {"status": "failed", "summary": "Could not fix"},
                    },
                },
            },
        ]

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=_mock_urlopen(responses)), \
             patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

            results = await execute_tier3_via_sweaf(items, findings, state, cfg)

        assert len(results) == 2
        outcomes = {r.finding_id: r.outcome for r in results}
        assert outcomes["F-001"] == FixOutcome.COMPLETED
        assert outcomes["F-002"] == FixOutcome.FAILED_RETRYABLE
