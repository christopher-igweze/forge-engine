"""Integration tests for Opengrep pipeline."""

import tempfile
from pathlib import Path

import pytest

from forge.execution.opengrep_runner import (
    OpengrepRunner,
    opengrep_available,
    to_audit_finding,
)

pytestmark = pytest.mark.skipif(
    not opengrep_available(), reason="Opengrep not installed"
)


class TestOpengrepIntegration:
    def test_scan_vulnerable_file(self):
        """Opengrep finds issues in a known-vulnerable file."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").write_text(
                'import os\npassword = "supersecret"\nos.system(f"echo {password}")\n'
            )
            runner = OpengrepRunner(use_community_rules=False)
            findings = runner.scan(d)
            assert len(findings) > 0
            check_ids = [f.check_id for f in findings]
            assert any(
                "secret" in c or "hardcoded" in c or "command" in c for c in check_ids
            )

    def test_scan_clean_file(self):
        """Opengrep returns few/no findings on clean code."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").write_text(
                'import os\n\ndef get_name():\n    return os.getenv("NAME", "world")\n\ndef greet():\n    return f"Hello, {get_name()}"\n'
            )
            runner = OpengrepRunner(use_community_rules=False)
            findings = runner.scan(d)
            assert len(findings) == 0

    def test_determinism(self):
        """Two scans on same code produce identical results."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").write_text('secret = "abc123"\n')
            runner = OpengrepRunner(use_community_rules=False)
            r1 = runner.scan(d)
            r2 = runner.scan(d)
            assert len(r1) == len(r2)
            for f1, f2 in zip(r1, r2):
                assert f1.check_id == f2.check_id
                assert f1.fingerprint == f2.fingerprint
                assert f1.line_start == f2.line_start

    def test_to_audit_finding_integration(self):
        """Converted findings have all required fields for FORGE pipeline."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").write_text('api_key = "sk-1234567890"\n')
            runner = OpengrepRunner(use_community_rules=False)
            findings = runner.scan(d)
            if findings:
                af = to_audit_finding(findings[0])
                assert "title" in af
                assert "severity" in af
                assert "category" in af
                assert af["source"] == "deterministic"

    def test_evaluation_with_opengrep(self):
        """run_evaluation uses opengrep findings when provided."""
        from forge.evaluation import run_evaluation

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "README.md").write_text("# Test\n" * 20)
            (Path(d) / "app.py").write_text('password = "secret"\n')
            runner = OpengrepRunner(use_community_rules=False)
            og_findings = runner.scan(d)
            og_dicts = [to_audit_finding(f) for f in og_findings]

            result = run_evaluation(d, opengrep_findings=og_dicts)
            assert "scores" in result
            assert result["scores"]["composite"] <= 100
