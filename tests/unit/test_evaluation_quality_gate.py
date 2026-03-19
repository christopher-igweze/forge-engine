"""Tests for FORGE v3 evaluation quality gate."""

import pytest

from forge.evaluation.checks import CheckResult
from forge.evaluation.dimensions import DimensionScore, DimensionScores
from forge.evaluation.quality_gate import (
    GATE_PROFILES,
    QualityGate,
    QualityGateResult,
    evaluate_quality_gate,
)


def _make_score(name: str, score: int) -> DimensionScore:
    return DimensionScore(
        name=name, score=score, checks_passed=0, checks_failed=0, deductions=0,
    )


def _make_scores(**kwargs) -> DimensionScores:
    defaults = {
        "security": 80,
        "reliability": 80,
        "maintainability": 80,
        "test_quality": 80,
        "performance": 80,
        "documentation": 80,
        "operations": 80,
    }
    defaults.update(kwargs)
    return DimensionScores(**{k: _make_score(k, v) for k, v in defaults.items()})


class TestGatePassFail:
    def test_passes_all_good(self):
        scores = _make_scores()
        result = evaluate_quality_gate(scores, gate="forge-way")
        assert result.passed is True
        assert result.failures == []

    def test_fails_low_security(self):
        scores = _make_scores(security=30)
        result = evaluate_quality_gate(scores, gate="forge-way")
        assert result.passed is False
        assert any("Security" in f for f in result.failures)

    def test_fails_new_critical(self):
        scores = _make_scores()
        baseline = {"new_critical": 1, "new_high": 0, "new_medium": 0}
        result = evaluate_quality_gate(scores, gate="forge-way", baseline_comparison=baseline)
        assert result.passed is False
        assert any("critical" in f for f in result.failures)

    def test_no_baseline_doesnt_crash(self):
        scores = _make_scores()
        result = evaluate_quality_gate(scores, gate="forge-way", baseline_comparison=None)
        assert result.passed is True


class TestGateProfiles:
    def test_profiles_exist(self):
        assert "forge-way" in GATE_PROFILES
        assert "strict" in GATE_PROFILES
        assert "startup" in GATE_PROFILES

    def test_strict_higher_thresholds(self):
        strict = GATE_PROFILES["strict"]
        default = GATE_PROFILES["forge-way"]
        assert strict.min_security_score > default.min_security_score

    def test_startup_relaxed(self):
        startup = GATE_PROFILES["startup"]
        assert startup.min_test_score == 0


class TestCustomGate:
    def test_custom_gate_instance(self):
        custom = QualityGate(min_security_score=90)
        scores = _make_scores(security=85)
        result = evaluate_quality_gate(scores, gate=custom)
        assert result.passed is False
        assert result.profile == "custom"
