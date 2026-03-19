"""Tests for FORGE v3 feedback tracking."""

import tempfile
from pathlib import Path

import pytest

from forge.evaluation.checks import CheckResult
from forge.evaluation.feedback import Feedback


def _make_result(check_id: str, passed: bool) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        name=f"Check {check_id}",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -5,
    )


class TestFeedbackLoad:
    def test_load_empty(self):
        d = tempfile.mkdtemp()
        fb = Feedback.load(d)
        assert fb.checks == {}
        assert fb.agents == {}


class TestFeedbackRoundtrip:
    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        fb = Feedback()
        fb.checks["SEC-001"] = {"total_triggers": 5, "confirmed_fp": 1, "fp_rate": 0.2}
        fb.save(d)
        loaded = Feedback.load(d)
        assert loaded.checks["SEC-001"]["total_triggers"] == 5


class TestRecordCheckResults:
    def test_records_trigger_counts(self):
        fb = Feedback()
        results = [
            _make_result("SEC-001", False),
            _make_result("SEC-001", False),
            _make_result("SEC-002", True),
        ]
        fb.record_check_results(results)
        # SEC-001 failed twice → 2 triggers
        assert fb.checks["SEC-001"]["total_triggers"] == 2
        # SEC-002 passed → 0 triggers
        assert fb.checks.get("SEC-002", {}).get("total_triggers", 0) == 0


class TestAgentFPRate:
    def test_agent_fp_rate(self):
        fb = Feedback()
        fb.record_suppressed_findings("security_auditor", total=10, suppressed=3)
        rate = fb.agent_fp_rate("security_auditor")
        assert rate == pytest.approx(0.3)

    def test_unknown_agent(self):
        fb = Feedback()
        assert fb.agent_fp_rate("nonexistent") == 0.0


class TestHighFPAgents:
    def test_high_fp_agents(self):
        fb = Feedback()
        fb.record_suppressed_findings("bad_agent", total=10, suppressed=5)
        fb.record_suppressed_findings("good_agent", total=10, suppressed=1)
        high = fb.high_fp_agents(threshold=0.4)
        assert "bad_agent" in high
        assert "good_agent" not in high
