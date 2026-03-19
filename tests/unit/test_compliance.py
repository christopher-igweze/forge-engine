"""Tests for FORGE v3 compliance mapping."""

import pytest

from forge.evaluation.checks import CheckResult
from forge.evaluation.compliance import (
    estimate_asvs_level,
    get_nist_coverage,
    get_stride_mapping,
)


def _make_result(check_id: str, passed: bool, stride: str = "") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        name=f"Check {check_id}",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -5,
        stride=stride,
    )


class TestASVS:
    def test_level_0_when_failing(self):
        results = [
            _make_result("SEC-001", False),
            _make_result("SEC-002", False),
            _make_result("SEC-004", True),
        ]
        asvs = estimate_asvs_level(results)
        assert asvs["estimated_level"] == 0

    def test_level_1_when_all_pass(self):
        # All ASVS-mapped check IDs passing
        asvs_check_ids = {
            "SEC-002", "SEC-009", "SEC-004", "SEC-005", "SEC-006",
            "SEC-001", "SEC-012", "SEC-010",
        }
        results = [_make_result(cid, True) for cid in asvs_check_ids]
        asvs = estimate_asvs_level(results)
        assert asvs["estimated_level"] == 1


class TestSTRIDE:
    def test_stride_mapping_covers_sec_checks(self):
        results = [
            _make_result("SEC-001", True, stride="Information Disclosure"),
            _make_result("SEC-004", False, stride="Spoofing"),
        ]
        stride = get_stride_mapping(results)
        assert "categories" in stride
        assert stride["covered_categories"] >= 1


class TestNIST:
    def test_nist_coverage_structure(self):
        results = [
            _make_result("SEC-001", True),
            _make_result("TST-001", False),
            _make_result("OPS-001", True),
        ]
        nist = get_nist_coverage(results)
        assert "practices" in nist
        assert "covered" in nist
        assert "total" in nist

    def test_compliance_with_empty_results(self):
        asvs = estimate_asvs_level([])
        stride = get_stride_mapping([])
        nist = get_nist_coverage([])
        # Should not crash
        assert asvs["estimated_level"] == 0
        assert stride["covered_categories"] == 0
        assert nist["total"] == 0
