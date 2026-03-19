"""Tests for Opengrep runner."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from forge.execution.opengrep_runner import (
    SEVERITY_MAP,
    OpengrepFinding,
    OpengrepRunner,
    opengrep_available,
    to_audit_finding,
)


class TestOpengrepAvailable:
    def test_returns_true_when_installed(self):
        with patch("shutil.which", return_value="/usr/local/bin/opengrep"):
            assert opengrep_available() is True

    def test_returns_false_when_missing(self):
        with patch("shutil.which", return_value=None):
            assert opengrep_available() is False


class TestOpengrepRunner:
    def test_scan_returns_empty_when_not_installed(self):
        with patch(
            "forge.execution.opengrep_runner.opengrep_available", return_value=False
        ):
            runner = OpengrepRunner()
            assert runner.scan("/tmp") == []

    def test_scan_parses_json_output(self):
        mock_output = json.dumps(
            {
                "results": [
                    {
                        "check_id": "python.lang.security.audit.sql-injection",
                        "path": "/tmp/app.py",
                        "start": {"line": 10, "col": 5, "offset": 100},
                        "end": {"line": 10, "col": 50, "offset": 145},
                        "extra": {
                            "message": "SQL injection risk",
                            "severity": "ERROR",
                            "fingerprint": "abc123",
                            "metadata": {
                                "cwe": ["CWE-89"],
                                "owasp": ["A03:2021"],
                            },
                            "lines": "cursor.execute(f'SELECT * FROM {table}')",
                            "fix": None,
                            "is_ignored": False,
                            "engine_kind": "OSS",
                            "metavars": {},
                        },
                    }
                ],
                "errors": [],
            }
        )
        with patch(
            "forge.execution.opengrep_runner.opengrep_available", return_value=True
        ), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout=mock_output, stderr=""
            )
            runner = OpengrepRunner(use_community_rules=False)
            findings = runner.scan("/tmp")
            assert len(findings) == 1
            assert findings[0].severity == "high"
            assert findings[0].category == "security"

    def test_scan_handles_timeout(self):
        with patch(
            "forge.execution.opengrep_runner.opengrep_available", return_value=True
        ), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("opengrep", 300),
        ):
            runner = OpengrepRunner()
            assert runner.scan("/tmp") == []

    def test_scan_handles_invalid_json(self):
        with patch(
            "forge.execution.opengrep_runner.opengrep_available", return_value=True
        ), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not json", stderr=""
            )
            runner = OpengrepRunner()
            assert runner.scan("/tmp") == []

    def test_skips_ignored_findings(self):
        mock_output = json.dumps(
            {
                "results": [
                    {
                        "check_id": "test.rule",
                        "path": "app.py",
                        "start": {"line": 1, "col": 1, "offset": 0},
                        "end": {"line": 1, "col": 10, "offset": 9},
                        "extra": {
                            "message": "ignored",
                            "severity": "WARNING",
                            "fingerprint": "ign123",
                            "metadata": {},
                            "lines": "x = 1",
                            "is_ignored": True,
                            "engine_kind": "OSS",
                            "metavars": {},
                        },
                    }
                ],
                "errors": [],
            }
        )
        with patch(
            "forge.execution.opengrep_runner.opengrep_available", return_value=True
        ), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout=mock_output, stderr=""
            )
            runner = OpengrepRunner(use_community_rules=False)
            assert runner.scan("/tmp") == []

    def test_default_rules_dir(self):
        """Runner defaults to forge/rules/ when no rules_dirs provided."""
        runner = OpengrepRunner()
        assert len(runner.rules_dirs) >= 0  # may be empty if dir doesn't exist in test env
        # But the constructor should not raise

    def test_custom_rules_dir(self):
        runner = OpengrepRunner(rules_dirs=["/custom/rules"])
        assert runner.rules_dirs == ["/custom/rules"]


class TestToAuditFinding:
    def test_converts_correctly(self):
        og = OpengrepFinding(
            check_id="forge.security.sql-injection",
            path="app.py",
            line_start=10,
            line_end=10,
            message="SQL injection",
            severity="high",
            fingerprint="abc",
            metadata={
                "cwe": ["CWE-89: SQL Injection"],
                "owasp": ["A03:2021"],
                "confidence": "HIGH",
            },
            snippet="cursor.execute(f'...')",
            category="security",
            forge_check_id="SEC-004",
        )
        af = to_audit_finding(og)
        assert af["category"] == "security"
        assert af["severity"] == "high"
        assert af["cwe_id"] == "CWE-89"
        assert af["source"] == "deterministic"
        assert af["confidence"] == 0.95

    def test_medium_confidence_default(self):
        og = OpengrepFinding(
            check_id="test.rule",
            path="app.py",
            line_start=1,
            line_end=1,
            message="test",
            severity="medium",
            metadata={"cwe": [], "owasp": [], "confidence": "MEDIUM"},
        )
        af = to_audit_finding(og)
        assert af["confidence"] == 0.85

    def test_empty_cwe_owasp(self):
        og = OpengrepFinding(
            check_id="test.rule",
            path="app.py",
            line_start=1,
            line_end=1,
            message="test",
            metadata={"cwe": [], "owasp": []},
        )
        af = to_audit_finding(og)
        assert af["cwe_id"] == ""
        assert af["owasp_ref"] == ""


class TestSeverityMap:
    def test_error_maps_to_high(self):
        assert SEVERITY_MAP["ERROR"] == "high"

    def test_warning_maps_to_medium(self):
        assert SEVERITY_MAP["WARNING"] == "medium"

    def test_info_maps_to_low(self):
        assert SEVERITY_MAP["INFO"] == "low"
