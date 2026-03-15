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
    def test_execute_success(self, tmp_path):
        """Full success flow: POST + poll -> completed results."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        responses = [
            {"execution_id": "exec-123"},
            {"status": "running"},
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

            results = asyncio.run(execute_tier3_via_sweaf([item], [finding], state, cfg))

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.COMPLETED
        assert results[0].finding_id == "F-001"

    def test_execute_timeout(self, tmp_path):
        """Timeout should return FAILED_RETRYABLE for all items."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = ForgeConfig(
            sweaf_agentfield_url="http://localhost:8080",
            sweaf_api_key="test-key",
            sweaf_timeout_seconds=0,
        )

        responses = [
            {"execution_id": "exec-123"},
            {"status": "running"},
        ]

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=_mock_urlopen(responses)), \
             patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

            results = asyncio.run(execute_tier3_via_sweaf([item], [finding], state, cfg))

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.FAILED_RETRYABLE

    def test_execute_connection_error(self, tmp_path):
        """Connection error should return FAILED_RETRYABLE."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=ConnectionError("refused")):

            results = asyncio.run(execute_tier3_via_sweaf([item], [finding], state, cfg))

        assert len(results) == 1
        assert results[0].outcome == FixOutcome.FAILED_RETRYABLE
        assert "bridge error" in results[0].summary.lower()

    def test_execute_partial_success(self, tmp_path):
        """Some issues complete, some fail -- mixed results."""
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

            results = asyncio.run(execute_tier3_via_sweaf(items, findings, state, cfg))

        assert len(results) == 2
        outcomes = {r.finding_id: r.outcome for r in results}
        assert outcomes["F-001"] == FixOutcome.COMPLETED
        assert outcomes["F-002"] == FixOutcome.FAILED_RETRYABLE

    def test_budget_passthrough(self, tmp_path):
        """Remaining budget from RunTelemetry is passed in POST payload."""
        finding = _make_finding()
        item = _make_item()
        state = _make_state(str(tmp_path))
        cfg = _make_cfg()

        # Track what payload was sent
        captured_payloads = []

        responses = [
            {"execution_id": "exec-budget"},
            {
                "status": "completed",
                "result": {
                    "issues": {
                        "fix-f-001": {"status": "completed", "summary": "Fixed"},
                    },
                },
            },
        ]

        original_mock = _mock_urlopen(responses)

        def capturing_urlopen(req, timeout=30):
            if req.method == "POST" and req.data:
                captured_payloads.append(json.loads(req.data.decode()))
            return original_mock(req, timeout=timeout)

        # Set up RunTelemetry with $5 budget, $2 already spent
        from forge.execution.run_telemetry import RunTelemetry, _current_run_telemetry
        rt = RunTelemetry(str(tmp_path / "telemetry"), max_cost_usd=5.0)
        rt.total_cost_usd = 2.0
        token = _current_run_telemetry.set(rt)

        try:
            with patch("forge.execution.sweaf_bridge.urllib.request.urlopen", side_effect=capturing_urlopen), \
                 patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

                asyncio.run(execute_tier3_via_sweaf([item], [finding], state, cfg))

            assert len(captured_payloads) == 1
            sent_max_cost = captured_payloads[0]["max_cost_usd"]
            # Should be capped to remaining $3.00 (not the config default $10.00)
            assert sent_max_cost == pytest.approx(3.0, abs=0.01)
        finally:
            _current_run_telemetry.reset(token)
