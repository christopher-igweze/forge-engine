"""Tests for per-agent feedback tracking."""
import json
import logging
from pathlib import Path

import pytest

from forge.execution.feedback import (
    FEEDBACK_FILENAME,
    FP_RATE_WARNING_THRESHOLD,
    AgentFeedback,
    FeedbackTracker,
)


class TestAgentFeedback:
    def test_fp_rate_zero_findings(self):
        fb = AgentFeedback(total_findings=0, total_suppressed=0)
        assert fb.fp_rate == 0.0

    def test_fp_rate_calculation(self):
        fb = AgentFeedback(total_findings=10, total_suppressed=3)
        assert fb.fp_rate == pytest.approx(0.3)


class TestFeedbackTracker:
    def test_load_missing_returns_empty(self, tmp_path):
        tracker = FeedbackTracker.load(str(tmp_path))
        assert tracker.agents == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        tracker = FeedbackTracker()
        tracker.agents["security_auditor"] = AgentFeedback(
            total_findings=20, total_suppressed=5, last_updated="2026-01-01T00:00:00+00:00"
        )
        tracker.save(str(tmp_path))

        loaded = FeedbackTracker.load(str(tmp_path))
        assert "security_auditor" in loaded.agents
        fb = loaded.agents["security_auditor"]
        assert fb.total_findings == 20
        assert fb.total_suppressed == 5
        assert fb.fp_rate == pytest.approx(0.25)

    def test_save_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        tracker = FeedbackTracker()
        tracker.agents["agent_a"] = AgentFeedback(total_findings=1)
        tracker.save(str(nested))
        assert (nested / FEEDBACK_FILENAME).exists()

    def test_update_from_scan_counts_correctly(self):
        tracker = FeedbackTracker()
        all_findings = [
            {"agent": "security_auditor", "title": "SQL injection"},
            {"agent": "security_auditor", "title": "XSS"},
            {"agent": "quality_auditor", "title": "Dead code"},
        ]
        suppressed = [
            {"agent": "security_auditor", "title": "XSS"},
        ]

        fp_rates = tracker.update_from_scan(all_findings, suppressed)

        # security_auditor: 3 total (2 from all + 1 from suppressed), 1 suppressed
        assert tracker.agents["security_auditor"].total_findings == 3
        assert tracker.agents["security_auditor"].total_suppressed == 1
        # quality_auditor: 1 total, 0 suppressed
        assert tracker.agents["quality_auditor"].total_findings == 1
        assert tracker.agents["quality_auditor"].total_suppressed == 0
        assert "security_auditor" in fp_rates
        assert "quality_auditor" in fp_rates

    def test_update_accumulates_across_scans(self):
        tracker = FeedbackTracker()
        findings1 = [{"agent": "agent_a", "title": "f1"}]
        suppressed1 = [{"agent": "agent_a", "title": "f1"}]
        tracker.update_from_scan(findings1, suppressed1)

        findings2 = [{"agent": "agent_a", "title": "f2"}]
        suppressed2: list[dict] = []
        tracker.update_from_scan(findings2, suppressed2)

        fb = tracker.agents["agent_a"]
        # Scan 1: 2 total (1 all + 1 suppressed), 1 suppressed
        # Scan 2: 1 total, 0 suppressed
        assert fb.total_findings == 3
        assert fb.total_suppressed == 1

    def test_warning_logged_when_threshold_exceeded(self, caplog):
        tracker = FeedbackTracker()
        # Pre-seed with high FP rate agent that has enough findings
        tracker.agents["noisy_agent"] = AgentFeedback(
            total_findings=8, total_suppressed=6
        )
        # Add more findings to push over the 10-finding minimum
        all_findings = [
            {"agent": "noisy_agent", "title": "fp1"},
            {"agent": "noisy_agent", "title": "fp2"},
            {"agent": "noisy_agent", "title": "fp3"},
        ]
        suppressed = [
            {"agent": "noisy_agent", "title": "fp1"},
            {"agent": "noisy_agent", "title": "fp2"},
            {"agent": "noisy_agent", "title": "fp3"},
        ]

        with caplog.at_level(logging.WARNING):
            tracker.update_from_scan(all_findings, suppressed)

        assert any("noisy_agent" in r.message and "false positive rate" in r.message for r in caplog.records)

    def test_no_warning_below_threshold(self, caplog):
        tracker = FeedbackTracker()
        all_findings = [{"agent": "good_agent", "title": f"f{i}"} for i in range(15)]
        suppressed = [{"agent": "good_agent", "title": "f0"}]

        with caplog.at_level(logging.WARNING):
            tracker.update_from_scan(all_findings, suppressed)

        assert not any("false positive rate" in r.message for r in caplog.records)

    def test_load_corrupted_file_returns_empty(self, tmp_path):
        (tmp_path / FEEDBACK_FILENAME).write_text("not json {{{")
        tracker = FeedbackTracker.load(str(tmp_path))
        assert tracker.agents == {}
