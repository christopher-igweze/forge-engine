"""Tests for FORGE telemetry cost tracking and training data."""

import json
import os

import pytest

from unittest.mock import patch

from forge.execution.telemetry import (
    ForgeTelemetry,
    MODEL_PRICING,
    DEFAULT_PRICING,
    AgentInvocationLog,
    _write_json,
)


class TestLogInvocation:
    def test_cost_calculation(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="coder_tier2",
            model="anthropic/claude-sonnet-4.6",
            input_tokens=1000,
            output_tokens=500,
        )
        # Sonnet: input $3/M, output $15/M
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert entry.cost_usd == pytest.approx(expected, abs=1e-6)

    def test_unknown_model_uses_default(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="test",
            model="unknown/model",
            input_tokens=1000,
            output_tokens=500,
        )
        expected = (1000 * DEFAULT_PRICING[0] + 500 * DEFAULT_PRICING[1]) / 1_000_000
        assert entry.cost_usd == pytest.approx(expected, abs=1e-6)

    def test_invocation_appended(self):
        t = ForgeTelemetry()
        t.log_invocation(agent_name="a1", model="minimax/minimax-m2.5")
        t.log_invocation(agent_name="a2", model="minimax/minimax-m2.5")
        assert len(t.invocations) == 2

    def test_zero_tokens_zero_cost(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="test",
            model="anthropic/claude-sonnet-4.6",
            input_tokens=0,
            output_tokens=0,
        )
        assert entry.cost_usd == 0.0

    def test_haiku_pricing(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="security_auditor",
            model="anthropic/claude-haiku-4.5",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        # Haiku: $1.00/M input
        assert entry.cost_usd == pytest.approx(1.0, abs=1e-6)

    def test_minimax_pricing(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="analyst",
            model="minimax/minimax-m2.5",
            input_tokens=0,
            output_tokens=1_000_000,
        )
        # MiniMax: $1.20/M output
        assert entry.cost_usd == pytest.approx(1.2, abs=1e-6)

    def test_entry_has_timestamp(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(agent_name="test", model="minimax/minimax-m2.5")
        assert entry.timestamp  # non-empty ISO string

    def test_entry_records_success(self):
        t = ForgeTelemetry()
        entry = t.log_invocation(
            agent_name="test", model="minimax/minimax-m2.5", success=False, error="timeout"
        )
        assert entry.success is False
        assert entry.error == "timeout"


class TestTrainingPair:
    def test_log_training_pair(self):
        t = ForgeTelemetry()
        t.log_training_pair(
            finding_id="F-001",
            category="security",
            severity="high",
            title="Test",
            description="Desc",
            tier=2,
            outcome="completed",
            summary="Fixed it",
            files_changed=["app.ts"],
        )
        assert len(t.training_data) == 1
        assert t.training_data[0].finding_id == "F-001"

    def test_log_multiple_training_pairs(self):
        t = ForgeTelemetry()
        for i in range(3):
            t.log_training_pair(
                finding_id=f"F-{i:03d}",
                category="security",
                severity="high",
                title=f"Finding {i}",
                description="Desc",
                tier=2,
                outcome="completed",
            )
        assert len(t.training_data) == 3

    def test_training_pair_defaults(self):
        t = ForgeTelemetry()
        t.log_training_pair(
            finding_id="F-001",
            category="quality",
            severity="low",
            title="Test",
            description="Desc",
            tier=1,
            outcome="completed",
        )
        entry = t.training_data[0]
        assert entry.files_changed == []
        assert entry.retry_count == 0
        assert entry.escalated is False
        assert entry.model_used == ""


class TestSummary:
    def test_total_cost(self):
        t = ForgeTelemetry()
        t.log_invocation(
            agent_name="a1", model="minimax/minimax-m2.5",
            input_tokens=1_000_000, output_tokens=0,
        )
        t.log_invocation(
            agent_name="a2", model="minimax/minimax-m2.5",
            input_tokens=0, output_tokens=1_000_000,
        )
        # MiniMax: $0.30/M input, $1.20/M output
        assert t.total_cost == pytest.approx(0.30 + 1.20, abs=0.01)

    def test_total_tokens(self):
        t = ForgeTelemetry()
        t.log_invocation(
            agent_name="a1", model="minimax/minimax-m2.5",
            input_tokens=100, output_tokens=200,
        )
        assert t.total_tokens == 300

    def test_summary_structure(self):
        t = ForgeTelemetry(run_id="test-run")
        t.log_invocation(
            agent_name="coder", model="anthropic/claude-sonnet-4.6", input_tokens=100,
        )
        s = t.summary()
        assert s["run_id"] == "test-run"
        assert "total_cost_usd" in s
        assert "cost_by_agent" in s
        assert "cost_by_model" in s
        assert s["total_invocations"] == 1
        assert s["successful_invocations"] == 1
        assert s["failed_invocations"] == 0

    def test_summary_cost_by_agent(self):
        t = ForgeTelemetry()
        t.log_invocation(
            agent_name="coder", model="minimax/minimax-m2.5",
            input_tokens=1_000_000,
        )
        t.log_invocation(
            agent_name="reviewer", model="minimax/minimax-m2.5",
            input_tokens=1_000_000,
        )
        s = t.summary()
        assert "coder" in s["cost_by_agent"]
        assert "reviewer" in s["cost_by_agent"]

    def test_summary_failed_invocations(self):
        t = ForgeTelemetry()
        t.log_invocation(agent_name="a1", model="minimax/minimax-m2.5", success=False)
        t.log_invocation(agent_name="a2", model="minimax/minimax-m2.5", success=True)
        s = t.summary()
        assert s["failed_invocations"] == 1
        assert s["successful_invocations"] == 1

    def test_empty_summary(self):
        t = ForgeTelemetry(run_id="empty")
        s = t.summary()
        assert s["total_invocations"] == 0
        assert s["total_cost_usd"] == 0.0
        assert s["total_tokens"] == 0


class TestFlush:
    def test_flush_creates_files(self, tmp_path):
        t = ForgeTelemetry(artifacts_dir=str(tmp_path), run_id="test")
        t.log_invocation(agent_name="a1", model="minimax/minimax-m2.5", input_tokens=100)
        t.log_training_pair(
            finding_id="F-001", category="sec", severity="high",
            title="T", description="D", tier=2, outcome="completed",
        )
        t.flush()

        telemetry_dir = tmp_path / "telemetry"
        assert (telemetry_dir / "cost_summary.json").exists()
        assert (telemetry_dir / "invocations.jsonl").exists()
        assert (telemetry_dir / "training_data.jsonl").exists()

        # Verify JSON is valid
        summary = json.loads((telemetry_dir / "cost_summary.json").read_text())
        assert summary["run_id"] == "test"

    def test_flush_no_artifacts_dir(self):
        t = ForgeTelemetry()
        # Should not raise
        t.flush()

    def test_flush_no_training_data(self, tmp_path):
        t = ForgeTelemetry(artifacts_dir=str(tmp_path))
        t.log_invocation(agent_name="a1", model="minimax/minimax-m2.5")
        t.flush()
        telemetry_dir = tmp_path / "telemetry"
        assert not (telemetry_dir / "training_data.jsonl").exists()

    def test_flush_invocations_jsonl_valid(self, tmp_path):
        t = ForgeTelemetry(artifacts_dir=str(tmp_path))
        t.log_invocation(agent_name="a1", model="minimax/minimax-m2.5", input_tokens=50)
        t.log_invocation(agent_name="a2", model="minimax/minimax-m2.5", output_tokens=75)
        t.flush()

        invocations_path = tmp_path / "telemetry" / "invocations.jsonl"
        lines = invocations_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "agent_name" in data
            assert "cost_usd" in data

    def test_flush_training_data_jsonl_valid(self, tmp_path):
        t = ForgeTelemetry(artifacts_dir=str(tmp_path))
        t.log_training_pair(
            finding_id="F-001", category="security", severity="high",
            title="T", description="D", tier=2, outcome="completed",
            files_changed=["a.py"],
        )
        t.flush()

        training_path = tmp_path / "telemetry" / "training_data.jsonl"
        data = json.loads(training_path.read_text().strip())
        assert data["finding_id"] == "F-001"
        assert data["files_changed"] == ["a.py"]


class TestFlushResilience:
    """Tests for telemetry I/O error resilience."""

    def test_flush_oserror_does_not_crash(self, tmp_path):
        t = ForgeTelemetry(artifacts_dir=str(tmp_path))
        t.log_invocation(
            agent_name="test", model="test/model",
            input_tokens=100, output_tokens=50,
        )
        with patch("os.makedirs", side_effect=OSError("permission denied")):
            # Should not raise — flush is non-fatal
            t.flush()

    def test_write_json_oserror_does_not_crash(self):
        with patch("builtins.open", side_effect=OSError("disk full")):
            # Should not raise — _write_json catches OSError
            _write_json("/nonexistent/path.json", {"key": "value"})
