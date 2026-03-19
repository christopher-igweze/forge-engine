"""Integration tests for FORGE v3 evaluation pipeline."""

import os

import pytest

from forge.evaluation import run_evaluation, format_cli_report


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRunEvaluation:
    def test_run_evaluation_on_forge_repo(self):
        """Run evaluation against the forge-engine repo itself."""
        result = run_evaluation(REPO_ROOT, gate_profile="forge-way")
        assert "version" in result
        assert result["version"] == "3.0"
        assert "scores" in result
        assert "quality_gate" in result
        assert "compliance" in result
        assert "deterministic_checks" in result
        assert result["scores"]["composite"] >= 0
        assert result["scores"]["composite"] <= 100

    def test_evaluation_deterministic(self):
        """Two runs on the same repo produce identical scores."""
        r1 = run_evaluation(REPO_ROOT, gate_profile="forge-way")
        r2 = run_evaluation(REPO_ROOT, gate_profile="forge-way")
        assert r1["scores"]["composite"] == r2["scores"]["composite"]
        assert r1["scores"]["band"] == r2["scores"]["band"]
        assert r1["scores"]["dimensions"] == r2["scores"]["dimensions"]

    def test_cli_report_renders(self):
        """format_cli_report doesn't crash and returns a string."""
        result = run_evaluation(REPO_ROOT, gate_profile="forge-way")
        text = format_cli_report(result)
        assert isinstance(text, str)
        assert "Composite Score" in text
        assert "Quality Gate" in text
