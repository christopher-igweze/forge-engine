"""Unit tests for the FORGE MCP server."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.mcp_server import (
    forge_findings,
    forge_fix,
    forge_report,
    forge_scan,
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


class TestForgeScan:
    """Test forge_scan tool with mocked run_standalone."""

    @pytest.mark.asyncio
    async def test_passes_discovery_config(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True, "total_findings": 3}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            result = await forge_scan(str(tmp_path))

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        config = call_kwargs.kwargs["config"]
        assert config["mode"] == "discovery"
        assert config["dry_run"] is True
        assert config["repo_path"] == str(tmp_path.resolve())
        assert "models" not in config

    @pytest.mark.asyncio
    async def test_model_override(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            await forge_scan(str(tmp_path), model="anthropic/claude-haiku-4.5")

        config = mock_run.call_args.kwargs["config"]
        assert config["models"] == {"default": "anthropic/claude-haiku-4.5"}

    @pytest.mark.asyncio
    async def test_model_dump_json_mode(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result):
            await forge_scan(str(tmp_path))

        mock_result.model_dump.assert_called_once_with(mode="json")

    @pytest.mark.asyncio
    async def test_bad_path_raises_valueerror(self):
        with pytest.raises(ValueError, match="is not a directory"):
            await forge_scan("/nonexistent/path/that/does/not/exist")


class TestForgeFix:
    """Test forge_fix tool with mocked run_standalone."""

    @pytest.mark.asyncio
    async def test_passes_full_mode_config(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True, "findings_fixed": 5}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            result = await forge_fix(str(tmp_path))

        mock_run.assert_called_once()
        config = mock_run.call_args.kwargs["config"]
        assert config["mode"] == "full"
        assert config["dry_run"] is False
        assert config["repo_path"] == str(tmp_path.resolve())

    @pytest.mark.asyncio
    async def test_dry_run_parameter(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            await forge_fix(str(tmp_path), dry_run=True)

        config = mock_run.call_args.kwargs["config"]
        assert config["dry_run"] is True

    @pytest.mark.asyncio
    async def test_model_override(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            await forge_fix(str(tmp_path), model="openai/gpt-4o")

        config = mock_run.call_args.kwargs["config"]
        assert config["models"] == {"default": "openai/gpt-4o"}

    @pytest.mark.asyncio
    async def test_model_dump_json_mode(self, tmp_path):
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"success": True}

        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result):
            await forge_fix(str(tmp_path))

        mock_result.model_dump.assert_called_once_with(mode="json")

    @pytest.mark.asyncio
    async def test_bad_path_raises_valueerror(self):
        with pytest.raises(ValueError, match="is not a directory"):
            await forge_fix("/nonexistent/path/that/does/not/exist")
