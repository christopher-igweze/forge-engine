"""Unit tests for the FORGE MCP server."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from forge.mcp_server import (
    forge_findings,
    forge_report,
    mcp,
    _resolve_path,
)


class TestMCPImport:
    """Verify MCP server can be imported and configured."""

    def test_mcp_server_name(self):
        assert mcp.name == "forge"

    def test_mcp_has_instructions(self):
        assert "FORGE" in (mcp.instructions or "")


class TestResolvePath:
    """Test path resolution helper."""

    def test_valid_directory(self, tmp_path):
        result = _resolve_path(str(tmp_path))
        assert result == str(tmp_path)

    def test_invalid_directory(self):
        with pytest.raises(ValueError, match="is not a directory"):
            _resolve_path("/nonexistent/path/that/does/not/exist")


class TestForgeReport:
    """Test forge_report tool with mock .artifacts directory."""

    def test_no_artifacts_dir(self, tmp_path):
        result = forge_report(str(tmp_path))
        assert "error" in result
        assert "No report found" in result["error"]

    def test_empty_report_dir(self, tmp_path):
        (tmp_path / ".artifacts" / "report").mkdir(parents=True)
        result = forge_report(str(tmp_path))
        assert "error" in result
        assert "No report files" in result["error"]

    def test_reads_latest_report(self, tmp_path):
        report_dir = tmp_path / ".artifacts" / "report"
        report_dir.mkdir(parents=True)

        report_data = {
            "overall_score": 72,
            "total_findings": 15,
            "sections": [],
        }
        report_file = report_dir / "forge-run-001.json"
        report_file.write_text(json.dumps(report_data))

        result = forge_report(str(tmp_path))
        assert result["overall_score"] == 72
        assert result["total_findings"] == 15

    def test_handles_corrupt_json(self, tmp_path):
        report_dir = tmp_path / ".artifacts" / "report"
        report_dir.mkdir(parents=True)
        (report_dir / "forge-run-001.json").write_text("not valid json{{{")

        result = forge_report(str(tmp_path))
        assert "error" in result
        assert "Failed to read" in result["error"]


class TestForgeFindings:
    """Test forge_findings tool with mock .artifacts directory."""

    def test_no_scan_dir(self, tmp_path):
        result = forge_findings(str(tmp_path))
        assert len(result) == 1
        assert "error" in result[0]

    def test_empty_scan_dir(self, tmp_path):
        (tmp_path / ".artifacts" / "scan").mkdir(parents=True)
        result = forge_findings(str(tmp_path))
        assert len(result) == 1
        assert "error" in result[0]

    def test_reads_findings_list(self, tmp_path):
        scan_dir = tmp_path / ".artifacts" / "scan"
        scan_dir.mkdir(parents=True)

        findings = [
            {"id": "f1", "severity": "high", "title": "SQL Injection"},
            {"id": "f2", "severity": "medium", "title": "Missing auth"},
        ]
        (scan_dir / "security.json").write_text(json.dumps(findings))

        result = forge_findings(str(tmp_path))
        assert len(result) == 2
        assert result[0]["id"] == "f1"
        assert result[1]["severity"] == "medium"

    def test_reads_wrapped_findings(self, tmp_path):
        scan_dir = tmp_path / ".artifacts" / "scan"
        scan_dir.mkdir(parents=True)

        wrapped = {"findings": [{"id": "f1", "title": "Issue"}]}
        (scan_dir / "quality.json").write_text(json.dumps(wrapped))

        result = forge_findings(str(tmp_path))
        assert len(result) == 1
        assert result[0]["id"] == "f1"

    def test_skips_corrupt_files(self, tmp_path):
        scan_dir = tmp_path / ".artifacts" / "scan"
        scan_dir.mkdir(parents=True)

        (scan_dir / "bad.json").write_text("not json")
        findings = [{"id": "f1", "title": "Good finding"}]
        (scan_dir / "good.json").write_text(json.dumps(findings))

        result = forge_findings(str(tmp_path))
        assert len(result) == 1
        assert result[0]["id"] == "f1"
