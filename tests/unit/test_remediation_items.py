"""Tests for deterministic check remediation item generation."""
import pytest
from forge.evaluation.checks import CheckResult
from forge.evaluation.remediation_items import generate_check_remediation_items


class TestGenerateCheckRemediationItems:

    def _make_check(self, check_id="SEC-001", name="Hardcoded secrets", passed=False, severity="critical", deduction=-20):
        return CheckResult(
            check_id=check_id,
            name=name,
            passed=passed,
            severity=severity,
            deduction=deduction,
            locations=[{"file": "config.py", "line": 42}],
            details="Found API key in config.py",
        )

    def test_generates_items_from_failed_checks(self):
        checks = [self._make_check(), self._make_check("REL-002", "No health check", severity="high")]
        items = generate_check_remediation_items(checks)
        assert len(items) == 2
        assert items[0]["finding_id"] == "SEC-001"
        assert items[1]["finding_id"] == "REL-002"

    def test_skips_passing_checks(self):
        checks = [self._make_check(passed=True)]
        items = generate_check_remediation_items(checks)
        assert len(items) == 0

    def test_items_have_required_fields(self):
        items = generate_check_remediation_items([self._make_check()])
        item = items[0]
        assert "finding_id" in item
        assert "title" in item
        assert "tier" in item
        assert "priority" in item
        assert "approach" in item
        assert "acceptance_criteria" in item
        assert "group" in item

    def test_security_prioritized_over_docs(self):
        checks = [
            self._make_check("DOC-001", "No README", severity="medium"),
            self._make_check("SEC-001", "Hardcoded secrets", severity="critical"),
        ]
        items = generate_check_remediation_items(checks)
        assert items[0]["finding_id"] == "SEC-001"
        assert items[1]["finding_id"] == "DOC-001"

    def test_all_check_ids_have_templates(self):
        """Every known check prefix should produce a non-default template."""
        for prefix in ["SEC", "REL", "MNT", "TST", "PRF", "DOC", "OPS"]:
            check = self._make_check(f"{prefix}-001", f"Test {prefix}")
            items = generate_check_remediation_items([check])
            assert len(items) == 1
            assert items[0]["approach"] != "Review and address the failed check."

    def test_unknown_check_uses_default_template(self):
        check = self._make_check("XYZ-999", "Unknown check")
        items = generate_check_remediation_items([check])
        assert len(items) == 1
        assert "Review and address" in items[0]["approach"]

    def test_files_extracted_from_locations(self):
        check = CheckResult(
            check_id="SEC-001", name="Test", passed=False, severity="high",
            deduction=-20, locations=[{"file": "a.py"}, {"file": "b.py"}],
        )
        items = generate_check_remediation_items([check])
        assert items[0]["files_to_modify"] == ["a.py", "b.py"]
