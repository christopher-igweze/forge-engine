"""Tests for post-scan pattern extraction pipeline."""

import json

import pytest

from forge.patterns.extractor import append_findings_history, update_pattern_prevalence
from forge.patterns.loader import PatternLibrary
from forge.patterns.schema import VulnerabilityPattern
from forge.schemas import AuditFinding, FindingCategory, FindingSeverity


def _make_finding(
    pattern_id: str = "",
    pattern_slug: str = "",
    title: str = "Test finding",
) -> AuditFinding:
    return AuditFinding(
        title=title,
        description="Test description",
        category=FindingCategory.SECURITY,
        severity=FindingSeverity.HIGH,
        pattern_id=pattern_id,
        pattern_slug=pattern_slug,
    )


class TestAppendFindingsHistory:
    def test_creates_jsonl_file(self, tmp_path):
        findings = [_make_finding(), _make_finding()]
        path = append_findings_history(findings, str(tmp_path))
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_entries_are_valid_json(self, tmp_path):
        findings = [_make_finding(pattern_id="VP-001")]
        path = append_findings_history(findings, str(tmp_path))
        entry = json.loads(path.read_text().strip())
        assert entry["pattern_id"] == "VP-001"
        assert entry["category"] == "security"
        assert entry["severity"] == "high"
        assert "timestamp" in entry

    def test_appends_to_existing(self, tmp_path):
        findings1 = [_make_finding(title="First")]
        findings2 = [_make_finding(title="Second")]
        append_findings_history(findings1, str(tmp_path))
        path = append_findings_history(findings2, str(tmp_path))
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_truncates_long_description(self, tmp_path):
        finding = AuditFinding(
            title="Long",
            description="x" * 1000,
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        path = append_findings_history([finding], str(tmp_path))
        entry = json.loads(path.read_text().strip())
        assert len(entry["description"]) == 500

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        path = append_findings_history([_make_finding()], str(nested))
        assert path.exists()


class TestUpdatePatternPrevalence:
    def test_counts_matched_patterns(self):
        lib = PatternLibrary([
            VulnerabilityPattern(id="VP-001", name="A", slug="a"),
            VulnerabilityPattern(id="VP-002", name="B", slug="b"),
        ])
        findings = [
            _make_finding(pattern_id="VP-001"),
            _make_finding(pattern_id="VP-001"),
            _make_finding(pattern_id="VP-002"),
        ]
        counts = update_pattern_prevalence(findings, lib)
        assert counts == {"VP-001": 2, "VP-002": 1}

    def test_updates_times_detected(self):
        lib = PatternLibrary([
            VulnerabilityPattern(id="VP-001", name="A", slug="a"),
        ])
        findings = [
            _make_finding(pattern_id="VP-001"),
            _make_finding(pattern_id="VP-001"),
            _make_finding(pattern_id="VP-001"),
        ]
        update_pattern_prevalence(findings, lib)
        assert lib.get("VP-001").times_detected == 3

    def test_ignores_unmatched_findings(self):
        lib = PatternLibrary([
            VulnerabilityPattern(id="VP-001", name="A", slug="a"),
        ])
        findings = [
            _make_finding(),  # no pattern_id
            _make_finding(pattern_id="VP-001"),
        ]
        counts = update_pattern_prevalence(findings, lib)
        assert counts == {"VP-001": 1}

    def test_ignores_unknown_pattern_ids(self):
        lib = PatternLibrary([
            VulnerabilityPattern(id="VP-001", name="A", slug="a"),
        ])
        findings = [_make_finding(pattern_id="VP-999")]
        counts = update_pattern_prevalence(findings, lib)
        assert counts == {"VP-999": 1}
        # VP-001 unaffected
        assert lib.get("VP-001").times_detected == 0

    def test_empty_findings(self):
        lib = PatternLibrary.load_default()
        counts = update_pattern_prevalence([], lib)
        assert counts == {}
