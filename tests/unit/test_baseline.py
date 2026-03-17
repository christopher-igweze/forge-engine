"""Tests for baseline comparison and persistence."""
import json
import pytest
from pathlib import Path
from forge.execution.baseline import Baseline, BaselineComparison


@pytest.fixture
def tmp_artifacts(tmp_path):
    artifacts = tmp_path / ".artifacts"
    artifacts.mkdir()
    return str(artifacts)


def _make_finding(fp: str, title: str = "Test", category: str = "security", severity: str = "high"):
    return {"fingerprint": fp, "id": f"F-{fp[:8]}", "title": title, "category": category, "severity": severity}


class TestBaselineLoadSave:
    def test_load_missing_returns_empty(self, tmp_artifacts):
        b = Baseline.load(tmp_artifacts)
        assert len(b.fingerprints) == 0
        assert len(b.suppressions) == 0

    def test_save_and_load_roundtrip(self, tmp_artifacts):
        b = Baseline()
        b.update_from_scan("scan-1", [_make_finding("aaa111", "Finding A")])
        b.save(tmp_artifacts)

        loaded = Baseline.load(tmp_artifacts)
        assert "aaa111" in loaded.fingerprints
        assert loaded.fingerprints["aaa111"].title == "Finding A"

    def test_corrupt_file_returns_empty(self, tmp_artifacts):
        Path(tmp_artifacts, "baseline.json").write_text("not json")
        b = Baseline.load(tmp_artifacts)
        assert len(b.fingerprints) == 0


class TestBaselineComparison:
    def test_first_scan_all_new(self, tmp_artifacts):
        b = Baseline()
        findings = [_make_finding("aaa"), _make_finding("bbb")]
        comparison = b.update_from_scan("scan-1", findings)
        assert len(comparison.new_findings) == 2
        assert len(comparison.recurring_findings) == 0
        assert len(comparison.fixed_findings) == 0

    def test_second_scan_recurring(self, tmp_artifacts):
        b = Baseline()
        b.update_from_scan("scan-1", [_make_finding("aaa")])
        comparison = b.update_from_scan("scan-2", [_make_finding("aaa")])
        assert len(comparison.new_findings) == 0
        assert len(comparison.recurring_findings) == 1

    def test_finding_fixed(self, tmp_artifacts):
        b = Baseline()
        b.update_from_scan("scan-1", [_make_finding("aaa"), _make_finding("bbb")])
        comparison = b.update_from_scan("scan-2", [_make_finding("aaa")])
        assert len(comparison.fixed_findings) == 1
        assert comparison.fixed_findings[0]["fingerprint"] == "bbb"

    def test_regression_detected(self, tmp_artifacts):
        b = Baseline()
        b.update_from_scan("scan-1", [_make_finding("aaa")])
        b.update_from_scan("scan-2", [])  # aaa is fixed
        comparison = b.update_from_scan("scan-3", [_make_finding("aaa")])  # aaa is back
        assert len(comparison.regressed_findings) == 1

    def test_suppressed_finding(self, tmp_artifacts):
        b = Baseline()
        b.suppress("aaa", "Intentional design")
        comparison = b.update_from_scan("scan-1", [_make_finding("aaa")])
        assert len(comparison.suppressed_findings) == 1
        assert len(comparison.new_findings) == 0

    def test_scan_count_increments(self, tmp_artifacts):
        b = Baseline()
        b.update_from_scan("scan-1", [_make_finding("aaa")])
        b.update_from_scan("scan-2", [_make_finding("aaa")])
        assert b.fingerprints["aaa"].scan_count == 2
