"""Tests for FORGE <-> SWE-AF data model adapter."""

from __future__ import annotations

import os

import pytest

from forge.execution.sweaf_adapter import (
    compute_execution_levels,
    finding_to_planned_issue,
    sweaf_result_to_coder_fix_results,
    write_issue_files,
)
from forge.schemas import (
    AuditFinding,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    RemediationItem,
    RemediationTier,
)


def _make_finding(
    fid="F-001",
    severity=FindingSeverity.HIGH,
    category=FindingCategory.SECURITY,
    data_flow="",
    cwe_id="",
    owasp_ref="",
):
    return AuditFinding(
        id=fid,
        title=f"Finding {fid}",
        description=f"Description for {fid}",
        category=category,
        severity=severity,
        data_flow=data_flow,
        cwe_id=cwe_id,
        owasp_ref=owasp_ref,
        locations=[
            FindingLocation(file_path="src/auth.py", line_start=42, snippet="bad_code()"),
        ],
    )


def _make_item(fid="F-001", tier=RemediationTier.TIER_3):
    return RemediationItem(
        finding_id=fid,
        title=f"Fix {fid}",
        tier=tier,
        priority=1,
        approach="Refactor auth module",
        acceptance_criteria=["Auth is secure", "Tests pass"],
        files_to_modify=["src/auth.py"],
    )


class TestFindingToPlannedIssue:
    def test_basic_conversion(self):
        finding = _make_finding()
        item = _make_item()

        issue = finding_to_planned_issue(item, finding)

        assert issue["name"] == "fix-f-001"
        assert issue["title"] == "Fix F-001"
        assert "Description for F-001" in issue["description"]
        assert issue["acceptance_criteria"] == ["Auth is secure", "Tests pass"]
        assert issue["files_to_modify"] == ["src/auth.py"]

    def test_security_context_packed(self):
        finding = _make_finding(
            data_flow="user_input -> parse() -> db.query()",
            cwe_id="CWE-89",
            owasp_ref="A03:2021",
        )
        item = _make_item()

        issue = finding_to_planned_issue(item, finding)
        desc = issue["description"]

        assert "user_input -> parse() -> db.query()" in desc
        assert "CWE-89" in desc
        assert "A03:2021" in desc
        assert "src/auth.py:42" in desc
        assert "bad_code()" in desc

    def test_guidance_critical_severity(self):
        finding = _make_finding(severity=FindingSeverity.CRITICAL)
        item = _make_item()

        issue = finding_to_planned_issue(item, finding)

        assert issue["guidance"]["needs_deeper_qa"] is True

    def test_guidance_low_severity(self):
        finding = _make_finding(severity=FindingSeverity.LOW)
        item = _make_item()

        issue = finding_to_planned_issue(item, finding)

        assert issue["guidance"]["needs_deeper_qa"] is False

    def test_depends_on_mapping(self):
        item = _make_item()
        item.depends_on = ["F-002", "F-003"]
        finding = _make_finding()

        issue = finding_to_planned_issue(item, finding)

        assert issue["depends_on"] == ["fix-f-002", "fix-f-003"]

    def test_default_acceptance_criteria(self):
        """When item has no acceptance criteria, sensible defaults are used."""
        finding = _make_finding()
        item = _make_item()
        item.acceptance_criteria = []

        issue = finding_to_planned_issue(item, finding)

        assert len(issue["acceptance_criteria"]) == 3
        assert any("Finding F-001" in ac for ac in issue["acceptance_criteria"])


class TestComputeExecutionLevels:
    def test_no_deps_single_level(self):
        issues = [
            {"name": "fix-a", "depends_on": []},
            {"name": "fix-b", "depends_on": []},
            {"name": "fix-c", "depends_on": []},
        ]
        levels = compute_execution_levels(issues)
        assert len(levels) == 1
        assert set(levels[0]) == {"fix-a", "fix-b", "fix-c"}

    def test_chain_dependency(self):
        issues = [
            {"name": "fix-a", "depends_on": []},
            {"name": "fix-b", "depends_on": ["fix-a"]},
            {"name": "fix-c", "depends_on": ["fix-b"]},
        ]
        levels = compute_execution_levels(issues)
        assert len(levels) == 3
        assert levels[0] == ["fix-a"]
        assert levels[1] == ["fix-b"]
        assert levels[2] == ["fix-c"]

    def test_diamond_dependency(self):
        issues = [
            {"name": "fix-a", "depends_on": []},
            {"name": "fix-b", "depends_on": ["fix-a"]},
            {"name": "fix-c", "depends_on": ["fix-a"]},
            {"name": "fix-d", "depends_on": ["fix-b", "fix-c"]},
        ]
        levels = compute_execution_levels(issues)
        assert len(levels) == 3
        assert levels[0] == ["fix-a"]
        assert set(levels[1]) == {"fix-b", "fix-c"}
        assert levels[2] == ["fix-d"]

    def test_circular_deps_fallback(self):
        issues = [
            {"name": "fix-a", "depends_on": ["fix-b"]},
            {"name": "fix-b", "depends_on": ["fix-a"]},
        ]
        levels = compute_execution_levels(issues)
        # Should fall back to single level
        assert len(levels) == 1
        assert set(levels[0]) == {"fix-a", "fix-b"}


class TestSweafResultMapping:
    def test_completed_maps_to_completed(self):
        result = {
            "issues": {
                "fix-f-001": {"status": "completed", "files_changed": ["src/auth.py"], "summary": "Fixed"},
            },
        }
        findings = {"F-001": _make_finding()}

        mapped = sweaf_result_to_coder_fix_results(result, findings)
        assert len(mapped) == 1
        assert mapped[0].outcome == FixOutcome.COMPLETED
        assert mapped[0].finding_id == "F-001"

    def test_partial_maps_to_debt(self):
        result = {
            "issues": {
                "fix-f-001": {"status": "partial", "summary": "Partial fix"},
            },
        }
        findings = {"F-001": _make_finding()}

        mapped = sweaf_result_to_coder_fix_results(result, findings)
        assert mapped[0].outcome == FixOutcome.COMPLETED_WITH_DEBT

    def test_failed_maps_to_failed_retryable(self):
        result = {
            "issues": {
                "fix-f-001": {"status": "failed", "summary": "Could not fix"},
            },
        }
        findings = {"F-001": _make_finding()}

        mapped = sweaf_result_to_coder_fix_results(result, findings)
        assert len(mapped) == 1
        assert mapped[0].outcome == FixOutcome.FAILED_RETRYABLE
        assert mapped[0].finding_id == "F-001"


class TestWriteIssueFiles:
    def test_writes_correct_content(self, tmp_path):
        issues = [
            {
                "name": "fix-f-001",
                "title": "Fix SQL injection",
                "description": "Parameterize queries",
                "acceptance_criteria": ["No SQL injection", "Tests pass"],
                "files_to_modify": ["src/db.py"],
            },
        ]

        issues_dir = write_issue_files(issues, str(tmp_path))
        filepath = os.path.join(issues_dir, "fix-f-001.md")

        assert os.path.isfile(filepath)
        content = open(filepath).read()
        assert "# Fix SQL injection" in content
        assert "Parameterize queries" in content
        assert "- [ ] No SQL injection" in content
        assert "- `src/db.py`" in content
