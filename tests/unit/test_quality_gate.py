"""Tests for quality gate evaluation."""
import pytest

from forge.execution.quality_gate import (
    QualityGateResult,
    QualityGateThreshold,
    evaluate_gate,
)


class TestQualityGate:
    def _make_findings(self, critical=0, high=0, medium=0, low=0):
        findings = []
        for _ in range(critical):
            findings.append({"severity": "critical", "category": "security"})
        for _ in range(high):
            findings.append({"severity": "high", "category": "security"})
        for _ in range(medium):
            findings.append({"severity": "medium", "category": "quality"})
        for _ in range(low):
            findings.append({"severity": "low", "category": "quality"})
        return findings

    def test_gate_passes_no_findings(self):
        result = evaluate_gate([])
        assert result.passed is True

    def test_gate_fails_with_critical(self):
        findings = self._make_findings(critical=1)
        result = evaluate_gate(findings)
        assert result.passed is False
        assert "critical" in result.reason.lower()

    def test_gate_fails_with_high(self):
        findings = self._make_findings(high=1)
        result = evaluate_gate(findings)
        assert result.passed is False
        assert "high" in result.reason.lower()

    def test_gate_passes_below_threshold(self):
        # Default thresholds: critical=0, high=0, medium=None (unlimited)
        findings = self._make_findings(medium=50, low=100)
        result = evaluate_gate(findings)
        assert result.passed is True

    def test_custom_thresholds(self):
        # Allow up to 2 critical and 5 high
        findings = self._make_findings(critical=2, high=5)
        threshold = QualityGateThreshold(max_new_critical=2, max_new_high=5)
        result = evaluate_gate(findings, threshold=threshold)
        assert result.passed is True

        # One more critical should fail
        findings = self._make_findings(critical=3, high=5)
        result = evaluate_gate(findings, threshold=threshold)
        assert result.passed is False

    def test_medium_threshold_none_means_unlimited(self):
        findings = self._make_findings(medium=1000)
        threshold = QualityGateThreshold(
            max_new_critical=0, max_new_high=0, max_new_medium=None
        )
        result = evaluate_gate(findings, threshold=threshold)
        assert result.passed is True

    def test_result_counts(self):
        findings = self._make_findings(critical=2, high=3, medium=4, low=5)
        result = evaluate_gate(findings)
        assert result.new_critical == 2
        assert result.new_high == 3
        assert result.new_medium == 4
        assert result.total_new == 14
