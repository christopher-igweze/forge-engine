"""Unit tests for RunTelemetry with cost + time circuit breakers."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.execution.run_telemetry import (
    AgentStatus,
    CostLimitExceeded,
    RunTelemetry,
    TimeLimitExceeded,
)


@pytest.fixture
def telemetry(tmp_path):
    """Create a RunTelemetry instance with a temp artifacts dir."""
    return RunTelemetry(
        artifacts_dir=str(tmp_path),
        max_cost_usd=5.0,
        max_duration_seconds=1800.0,
    )


class TestInitialState:
    """test_initial_state — snapshot has all fields."""

    def test_initial_state(self, telemetry):
        snap = telemetry.snapshot()
        assert snap["status"] == "running"
        assert "elapsed_seconds" in snap
        assert "elapsed_human" in snap
        assert snap["budget"]["cost_spent"] == 0.0
        assert snap["budget"]["cost_limit"] == 5.0
        assert snap["budget"]["time_limit"] == 1800.0
        assert snap["totals"]["invocations"] == 0
        assert snap["totals"]["tokens"] == 0
        assert snap["phase"] == "initializing"
        assert snap["phases_completed"] == []
        assert snap["findings"]["total"] == 0
        assert snap["active_agents"] == []


class TestRecordInvocation:
    """test_record_invocation — cost and tokens accumulate."""

    @pytest.mark.asyncio
    async def test_record_invocation(self, telemetry):
        await telemetry.record_invocation(
            agent_name="security_auditor",
            model="minimax/minimax-m2.5",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.05,
        )
        await telemetry.record_invocation(
            agent_name="quality_auditor",
            model="minimax/minimax-m2.5",
            input_tokens=2000,
            output_tokens=800,
            cost_usd=0.10,
        )

        snap = telemetry.snapshot()
        assert snap["totals"]["invocations"] == 2
        assert snap["totals"]["tokens"] == 4300
        assert snap["totals"]["input_tokens"] == 3000
        assert snap["totals"]["output_tokens"] == 1300
        assert snap["budget"]["cost_spent"] == 0.15
        assert "security_auditor" in snap["cost_by_agent"]
        assert "quality_auditor" in snap["cost_by_agent"]


class TestCostLimitExceeded:
    """test_cost_limit_exceeded — raises when budget hit."""

    @pytest.mark.asyncio
    async def test_cost_limit_exceeded(self, tmp_path):
        rt = RunTelemetry(
            artifacts_dir=str(tmp_path),
            max_cost_usd=0.10,
            max_duration_seconds=1800.0,
        )
        with pytest.raises(CostLimitExceeded, match="BUDGET EXCEEDED"):
            await rt.record_invocation(
                agent_name="coder",
                model="anthropic/claude-sonnet-4.6",
                input_tokens=50000,
                output_tokens=10000,
                cost_usd=0.15,  # Exceeds $0.10 limit
            )


class TestTimeLimitExceeded:
    """test_time_limit_exceeded — raises when time hit."""

    @pytest.mark.asyncio
    async def test_time_limit_exceeded(self, tmp_path):
        rt = RunTelemetry(
            artifacts_dir=str(tmp_path),
            max_cost_usd=100.0,
            max_duration_seconds=0.0,  # Already expired
        )
        # Manually set start time to the past
        rt._start_time = time.time() - 10

        with pytest.raises(TimeLimitExceeded, match="TIME EXCEEDED"):
            await rt.record_invocation(
                agent_name="coder",
                model="minimax/minimax-m2.5",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.001,
            )


class TestSnapshotBudgetPercentages:
    """test_snapshot_budget_percentages — math is correct."""

    @pytest.mark.asyncio
    async def test_snapshot_budget_percentages(self, tmp_path):
        rt = RunTelemetry(
            artifacts_dir=str(tmp_path),
            max_cost_usd=10.0,
            max_duration_seconds=1800.0,
        )
        await rt.record_invocation(
            agent_name="test",
            model="minimax/minimax-m2.5",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=2.5,  # 25% of $10
        )

        snap = rt.snapshot()
        assert snap["budget"]["cost_percent"] == 25.0
        assert snap["budget"]["cost_remaining"] == 7.5
        assert snap["budget"]["cost_spent"] == 2.5


class TestFlushWritesLiveStatus:
    """test_flush_writes_live_status — file exists and is valid JSON."""

    def test_flush_writes_live_status(self, telemetry, tmp_path):
        status_file = tmp_path / "telemetry" / "live_status.json"
        assert status_file.exists()

        data = json.loads(status_file.read_text())
        assert data["status"] == "running"
        assert data["phase"] == "initializing"


class TestInvocationsLogAppends:
    """test_invocations_log_appends — JSONL file grows."""

    @pytest.mark.asyncio
    async def test_invocations_log_appends(self, telemetry, tmp_path):
        for i in range(3):
            await telemetry.record_invocation(
                agent_name=f"agent_{i}",
                model="minimax/minimax-m2.5",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.001,
            )

        log_file = tmp_path / "telemetry" / "invocations.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3

        # Each line is valid JSON
        for line in lines:
            record = json.loads(line)
            assert "agent" in record
            assert "cost_usd" in record
            assert "cumulative_cost" in record


class TestAgentLifecycle:
    """test_agent_lifecycle — started/completed/failed tracking."""

    def test_agent_lifecycle(self, telemetry):
        telemetry.agent_started("agent-1", "security_auditor", "minimax/minimax-m2.5")
        snap = telemetry.snapshot()
        assert len(snap["active_agents"]) == 1
        assert snap["active_agents"][0]["name"] == "security_auditor"
        assert snap["active_agents"][0]["status"] == "running"

        telemetry.agent_completed("agent-1", cost_usd=0.05)
        snap = telemetry.snapshot()
        # Completed agents are no longer in active_agents (filtered to "running")
        assert len(snap["active_agents"]) == 0
        assert telemetry.active_agents["agent-1"].status == "completed"

    def test_agent_failed(self, telemetry):
        telemetry.agent_started("agent-2", "coder", "anthropic/claude-sonnet-4.6")
        telemetry.agent_failed("agent-2", error="context window exceeded")

        assert telemetry.active_agents["agent-2"].status == "failed"
        assert telemetry.active_agents["agent-2"].error == "context window exceeded"


class TestPhaseTracking:
    """test_phase_tracking — set_phase records transitions."""

    def test_phase_tracking(self, telemetry):
        assert telemetry.current_phase == "initializing"
        assert telemetry.phases_completed == []

        telemetry.set_phase("discovery")
        assert telemetry.current_phase == "discovery"
        assert telemetry.phases_completed == []  # "initializing" not tracked

        telemetry.set_phase("triage")
        assert telemetry.current_phase == "triage"
        assert telemetry.phases_completed == ["discovery"]

        telemetry.set_phase("remediation")
        assert telemetry.current_phase == "remediation"
        assert telemetry.phases_completed == ["discovery", "triage"]

        telemetry.set_phase("validation")
        assert telemetry.current_phase == "validation"
        assert telemetry.phases_completed == ["discovery", "triage", "remediation"]


class TestConcurrentRecording:
    """test_concurrent_recording — async safety with multiple writers."""

    @pytest.mark.asyncio
    async def test_concurrent_recording(self, tmp_path):
        rt = RunTelemetry(
            artifacts_dir=str(tmp_path),
            max_cost_usd=100.0,
            max_duration_seconds=1800.0,
        )

        async def record_batch(agent_name: str, count: int):
            for _ in range(count):
                await rt.record_invocation(
                    agent_name=agent_name,
                    model="minimax/minimax-m2.5",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.001,
                )

        # Run 5 agents concurrently, each recording 10 invocations
        await asyncio.gather(
            record_batch("agent_a", 10),
            record_batch("agent_b", 10),
            record_batch("agent_c", 10),
            record_batch("agent_d", 10),
            record_batch("agent_e", 10),
        )

        snap = rt.snapshot()
        assert snap["totals"]["invocations"] == 50
        assert snap["totals"]["tokens"] == 50 * 150  # 100+50 per invocation
        assert abs(snap["budget"]["cost_spent"] - 0.05) < 0.001  # 50 * 0.001

        # JSONL log should also have 50 entries
        log_file = tmp_path / "telemetry" / "invocations.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 50
