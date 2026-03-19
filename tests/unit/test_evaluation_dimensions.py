"""Tests for FORGE v3 dimension scoring."""

import pytest

from forge.evaluation.checks import CheckResult
from forge.evaluation.dimensions import (
    DEFAULT_WEIGHTS,
    DimensionScore,
    DimensionScores,
    compute_dimension_score,
    run_all_checks,
)


def _make_score(name: str, score: int) -> DimensionScore:
    return DimensionScore(
        name=name, score=score, checks_passed=0, checks_failed=0, deductions=0,
    )


def _make_scores(**kwargs) -> DimensionScores:
    defaults = {
        "security": 50,
        "reliability": 50,
        "maintainability": 50,
        "test_quality": 50,
        "performance": 50,
        "documentation": 50,
        "operations": 50,
    }
    defaults.update(kwargs)
    return DimensionScores(**{k: _make_score(k, v) for k, v in defaults.items()})


class TestCompositeScore:
    def test_default_weights(self):
        scores = _make_scores()
        assert scores.composite() == 50

    def test_custom_weights(self):
        scores = _make_scores(security=100)
        custom = {"security": 1.0, "reliability": 0.0, "maintainability": 0.0,
                  "test_quality": 0.0, "performance": 0.0, "documentation": 0.0,
                  "operations": 0.0}
        assert scores.composite(custom) == 100


class TestBand:
    def test_band_a(self):
        scores = _make_scores(
            security=85, reliability=85, maintainability=85, test_quality=85,
            performance=85, documentation=85, operations=85,
        )
        letter, label = scores.band()
        assert letter == "A"
        assert label == "Production Ready"

    def test_band_c(self):
        scores = _make_scores(
            security=45, reliability=45, maintainability=45, test_quality=45,
            performance=45, documentation=45, operations=45,
        )
        letter, label = scores.band()
        assert letter == "C"
        assert label == "Needs Work"


class TestDimensionScoreCalculation:
    def test_score_from_checks(self):
        checks = [
            CheckResult(check_id="X-1", name="a", passed=True, severity="high", deduction=0),
            CheckResult(check_id="X-2", name="b", passed=False, severity="high", deduction=-10),
            CheckResult(check_id="X-3", name="c", passed=False, severity="medium", deduction=-5),
        ]
        dim = compute_dimension_score(checks, "test")
        assert dim.score == 85  # 100 + (-10) + (-5) = 85
        assert dim.checks_passed == 1
        assert dim.checks_failed == 2

    def test_score_floor_at_zero(self):
        checks = [
            CheckResult(check_id="X-1", name="a", passed=False, severity="critical", deduction=-60),
            CheckResult(check_id="X-2", name="b", passed=False, severity="critical", deduction=-60),
        ]
        dim = compute_dimension_score(checks, "test")
        assert dim.score == 0


class TestToDict:
    def test_structure(self):
        scores = _make_scores()
        d = scores.to_dict()
        assert set(d.keys()) == set(DEFAULT_WEIGHTS.keys())
        for dim_data in d.values():
            assert "score" in dim_data
            assert "checks_passed" in dim_data
            assert "checks_failed" in dim_data
            assert "deductions" in dim_data


class TestRunAllChecks:
    def test_returns_48_checks(self):
        import tempfile
        d = tempfile.mkdtemp()
        scores, results = run_all_checks(d)
        # 12 + 7 + 5 + 7 + 5 + 6 + 6 = 48
        assert len(results) == 48
        assert isinstance(scores, DimensionScores)
