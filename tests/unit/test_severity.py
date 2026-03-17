"""Tests for severity calibration."""
import pytest
from forge.execution.severity import calibrate_severity, calibrate_findings


class TestCalibrateSeverity:
    def test_architecture_capped_at_medium(self):
        finding = {"category": "architecture", "severity": "high"}
        assert calibrate_severity(finding) == "medium"

    def test_architecture_low_unchanged(self):
        finding = {"category": "architecture", "severity": "low"}
        assert calibrate_severity(finding) == "low"

    def test_security_not_capped(self):
        finding = {"category": "security", "severity": "high"}
        assert calibrate_severity(finding) == "high"

    def test_owasp_boost(self):
        finding = {"category": "security", "severity": "medium", "cwe_id": "CWE-89"}
        assert calibrate_severity(finding) == "high"

    def test_owasp_already_high(self):
        finding = {"category": "security", "severity": "high", "cwe_id": "CWE-89"}
        assert calibrate_severity(finding) == "high"

    def test_low_confidence_downgrade(self):
        finding = {"category": "security", "severity": "high", "confidence": 0.4}
        assert calibrate_severity(finding) == "medium"

    def test_low_confidence_low_stays_low(self):
        finding = {"category": "security", "severity": "low", "confidence": 0.4}
        assert calibrate_severity(finding) == "low"

    def test_normal_confidence_no_change(self):
        finding = {"category": "security", "severity": "high", "confidence": 0.9}
        assert calibrate_severity(finding) == "high"

    def test_empty_finding(self):
        assert calibrate_severity({}) == "medium"


class TestCalibrateFindings:
    def test_modifies_in_place(self):
        findings = [{"category": "architecture", "severity": "high"}]
        result = calibrate_findings(findings)
        assert result is findings
        assert findings[0]["severity"] == "medium"
        assert findings[0]["original_severity"] == "high"

    def test_no_change_no_original(self):
        findings = [{"category": "security", "severity": "high"}]
        calibrate_findings(findings)
        assert "original_severity" not in findings[0]
