"""Integration tests for SWE-AF HTTP bridge (mocked HTTP)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
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


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    """Create an httpx.Response with a request set so raise_for_status works."""
    resp = httpx.Response(status_code, json=json_data)
    resp._request = httpx.Request("GET", "http://test")
    return resp


def _make_mock_client(responses: list[dict], *, post_side_effect=None):
    """Create a mock httpx.AsyncClient that returns successive responses.

    The first response is returned by POST, the rest by GET (poll calls).
    """
    call_idx = 0

    def _next_response(*args, **kwargs):
        nonlocal call_idx
        data = responses[min(call_idx, len(responses) - 1)]
        call_idx += 1
        return _make_response(200, data)

    mock_client = AsyncMock()
    if post_side_effect:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(side_effect=lambda *a, **kw: _next_response())
    mock_client.get = AsyncMock(side_effect=lambda *a, **kw: _next_response())
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


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

        mock_client = _make_mock_client(responses)

        with patch("forge.execution.sweaf_bridge.httpx.AsyncClient", return_value=mock_client), \
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

        mock_client = _make_mock_client(responses)

        with patch("forge.execution.sweaf_bridge.httpx.AsyncClient", return_value=mock_client), \
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

        mock_client = _make_mock_client(
            [],
            post_side_effect=httpx.ConnectError("refused"),
        )

        with patch("forge.execution.sweaf_bridge.httpx.AsyncClient", return_value=mock_client):

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

        mock_client = _make_mock_client(responses)

        with patch("forge.execution.sweaf_bridge.httpx.AsyncClient", return_value=mock_client), \
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

        def capturing_post(*args, **kwargs):
            json_data = kwargs.get("json")
            if json_data:
                captured_payloads.append(json_data)
            return _make_response(200, responses[0])

        mock_client = _make_mock_client(responses, post_side_effect=capturing_post)

        # Set up RunTelemetry with $5 budget, $2 already spent
        from forge.execution.run_telemetry import RunTelemetry, _current_run_telemetry
        rt = RunTelemetry(str(tmp_path / "telemetry"), max_cost_usd=5.0)
        rt.total_cost_usd = 2.0
        token = _current_run_telemetry.set(rt)

        try:
            with patch("forge.execution.sweaf_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("forge.execution.sweaf_bridge._POLL_INTERVAL", 0.01):

                asyncio.run(execute_tier3_via_sweaf([item], [finding], state, cfg))

            assert len(captured_payloads) == 1
            # Verify payload has input.config but no max_cost_usd (removed — SWE-AF rejects it)
            assert "config" in captured_payloads[0]["input"]
            assert "max_cost_usd" not in captured_payloads[0]["input"]["config"]
        finally:
            _current_run_telemetry.reset(token)
