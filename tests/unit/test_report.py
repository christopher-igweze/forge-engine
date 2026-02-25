"""Tests for FORGE production readiness report generation."""

import json
import os

import pytest

from forge.execution.report import (
    generate_reports,
    _score_color,
    _score_label,
    _esc,
)
from forge.schemas import (
    CategoryScore,
    DebtEntry,
    FindingSeverity,
    ProductionReadinessReport,
)


class TestScoreColor:
    def test_green(self):
        assert _score_color(80) == "#22c55e"
        assert _score_color(100) == "#22c55e"

    def test_yellow(self):
        assert _score_color(60) == "#eab308"
        assert _score_color(79) == "#eab308"

    def test_orange(self):
        assert _score_color(40) == "#f97316"
        assert _score_color(59) == "#f97316"

    def test_red(self):
        assert _score_color(0) == "#ef4444"
        assert _score_color(39) == "#ef4444"

    def test_boundary_80(self):
        """Score of exactly 80 should be green."""
        assert _score_color(80) == "#22c55e"

    def test_boundary_60(self):
        """Score of exactly 60 should be yellow."""
        assert _score_color(60) == "#eab308"

    def test_boundary_40(self):
        """Score of exactly 40 should be orange."""
        assert _score_color(40) == "#f97316"


class TestScoreLabel:
    def test_production_ready(self):
        assert _score_label(80) == "Production Ready"

    def test_needs_improvement(self):
        assert _score_label(60) == "Needs Improvement"

    def test_significant_issues(self):
        assert _score_label(40) == "Significant Issues"

    def test_not_ready(self):
        assert _score_label(0) == "Not Production Ready"

    def test_boundary_values(self):
        assert _score_label(79) == "Needs Improvement"
        assert _score_label(59) == "Significant Issues"
        assert _score_label(39) == "Not Production Ready"


class TestEscapeHtml:
    def test_escapes_all_chars(self):
        assert _esc('a & b < c > d "e"') == 'a &amp; b &lt; c &gt; d &quot;e&quot;'

    def test_plain_text_unchanged(self):
        assert _esc("hello world") == "hello world"

    def test_empty_string(self):
        assert _esc("") == ""

    def test_ampersand_first(self):
        """Ampersand must be escaped first to avoid double-escaping."""
        assert _esc("&lt;") == "&amp;lt;"


class TestGenerateReports:
    def test_generates_json_and_html(self, tmp_path):
        report = ProductionReadinessReport(
            overall_score=75,
            findings_total=10,
            findings_fixed=7,
            findings_deferred=3,
            summary="Good progress",
            category_scores=[
                CategoryScore(name="Security", score=80, weight=0.3),
            ],
            recommendations=["Add tests"],
            debt_items=[
                DebtEntry(
                    title="Issue A",
                    description="Desc",
                    severity=FindingSeverity.MEDIUM,
                    reason_deferred="Too complex",
                ),
            ],
        )
        paths = generate_reports(report, str(tmp_path), run_id="test-run")

        assert "json" in paths
        assert "html" in paths

        # Validate JSON
        with open(paths["json"]) as f:
            data = json.load(f)
        assert data["overall_score"] == 75
        assert data["findings_total"] == 10

        # Validate HTML contains key elements
        with open(paths["html"]) as f:
            html = f.read()
        assert "FORGE Production Readiness Report" in html
        assert "test-run" in html
        assert "75" in html
        assert "Security" in html
        assert "Issue A" in html
        assert "Add tests" in html

    def test_empty_report(self, tmp_path):
        report = ProductionReadinessReport()
        paths = generate_reports(report, str(tmp_path))
        assert "json" in paths
        assert "html" in paths

    def test_report_directory_created(self, tmp_path):
        report = ProductionReadinessReport(overall_score=50)
        paths = generate_reports(report, str(tmp_path))
        report_dir = tmp_path / "report"
        assert report_dir.is_dir()
        assert os.path.isfile(paths["json"])
        assert os.path.isfile(paths["html"])

    def test_json_roundtrip(self, tmp_path):
        """JSON report can be deserialized back into a valid dict."""
        report = ProductionReadinessReport(
            overall_score=85,
            findings_total=5,
            findings_fixed=5,
            summary="All fixed",
            recommendations=["Deploy!"],
        )
        paths = generate_reports(report, str(tmp_path))
        with open(paths["json"]) as f:
            data = json.load(f)
        assert data["overall_score"] == 85
        assert data["findings_fixed"] == 5
        assert data["summary"] == "All fixed"
        assert "Deploy!" in data["recommendations"]

    def test_html_contains_debt_table(self, tmp_path):
        report = ProductionReadinessReport(
            overall_score=60,
            debt_items=[
                DebtEntry(
                    title="Missing tests",
                    description="No unit tests",
                    severity=FindingSeverity.HIGH,
                    reason_deferred="Time constraint",
                ),
            ],
        )
        paths = generate_reports(report, str(tmp_path))
        with open(paths["html"]) as f:
            html = f.read()
        assert "Technical Debt" in html
        assert "Missing tests" in html
        assert "Time constraint" in html

    def test_html_score_color_embedded(self, tmp_path):
        """HTML should contain the correct color for the score."""
        report = ProductionReadinessReport(overall_score=30)
        paths = generate_reports(report, str(tmp_path))
        with open(paths["html"]) as f:
            html = f.read()
        # Score 30 should get red color
        assert "#ef4444" in html
