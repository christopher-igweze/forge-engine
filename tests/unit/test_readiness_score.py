"""Tests for production readiness score estimation."""
import pytest

from forge.execution.readiness_score import (
    MAX_CATEGORY_DEDUCTION,
    SEVERITY_DEDUCTIONS,
    estimate_readiness_score,
    readiness_breakdown,
)


class TestEstimateReadinessScore:
    def test_perfect_score_no_findings(self):
        assert estimate_readiness_score([]) == 100

    def test_single_critical_deduction(self):
        findings = [{"severity": "critical", "category": "security"}]
        assert estimate_readiness_score(findings) == 100 + SEVERITY_DEDUCTIONS["critical"]

    def test_single_high_deduction(self):
        findings = [{"severity": "high", "category": "security"}]
        assert estimate_readiness_score(findings) == 100 + SEVERITY_DEDUCTIONS["high"]

    def test_single_medium_deduction(self):
        findings = [{"severity": "medium", "category": "quality"}]
        assert estimate_readiness_score(findings) == 100 + SEVERITY_DEDUCTIONS["medium"]

    def test_single_low_deduction(self):
        findings = [{"severity": "low", "category": "quality"}]
        assert estimate_readiness_score(findings) == 100 + SEVERITY_DEDUCTIONS["low"]

    def test_category_cap_prevents_tanking(self):
        # 10 critical findings in the same category should be capped
        findings = [{"severity": "critical", "category": "security"} for _ in range(10)]
        score = estimate_readiness_score(findings)
        # With cap at -25, single category can only deduct 25
        assert score == 100 + MAX_CATEGORY_DEDUCTION

    def test_multiple_categories_deduct_independently(self):
        findings = [
            {"severity": "critical", "category": "security"},
            {"severity": "critical", "category": "quality"},
        ]
        score = estimate_readiness_score(findings)
        # Each category deducts 15 independently
        assert score == 100 + 2 * SEVERITY_DEDUCTIONS["critical"]

    def test_score_never_below_zero(self):
        # Many critical findings across many categories
        findings = [
            {"severity": "critical", "category": f"cat_{i}"}
            for i in range(20)
        ]
        score = estimate_readiness_score(findings)
        assert score == 0

    def test_score_never_above_100(self):
        assert estimate_readiness_score([]) == 100

    def test_unknown_severity_defaults_to_minus_one(self):
        findings = [{"severity": "unknown_sev", "category": "misc"}]
        assert estimate_readiness_score(findings) == 99

    def test_missing_severity_defaults_to_medium(self):
        findings = [{"category": "quality"}]
        assert estimate_readiness_score(findings) == 100 + SEVERITY_DEDUCTIONS["medium"]

    def test_missing_category_uses_uncategorized(self):
        findings = [{"severity": "high"}, {"severity": "high"}]
        # Both go to "uncategorized" category, deduction is 2*(-8) = -16
        assert estimate_readiness_score(findings) == 84


class TestReadinessBreakdown:
    def test_empty_findings(self):
        result = readiness_breakdown([])
        assert result["overall_score"] == 100
        assert result["categories"] == {}
        assert result["total_findings"] == 0

    def test_correct_structure(self):
        findings = [
            {"severity": "critical", "category": "security"},
            {"severity": "medium", "category": "security"},
            {"severity": "low", "category": "quality"},
        ]
        result = readiness_breakdown(findings)
        assert "overall_score" in result
        assert "categories" in result
        assert "total_findings" in result
        assert result["total_findings"] == 3

    def test_category_counts(self):
        findings = [
            {"severity": "critical", "category": "security"},
            {"severity": "high", "category": "security"},
            {"severity": "medium", "category": "quality"},
        ]
        result = readiness_breakdown(findings)
        sec = result["categories"]["security"]
        assert sec["findings"] == 2
        assert sec["critical"] == 1
        assert sec["high"] == 1
        qual = result["categories"]["quality"]
        assert qual["findings"] == 1
        assert qual["medium"] == 1
