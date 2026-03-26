"""Unit tests for the FORGE MCP server."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.mcp_server import (
    forge_config,
    forge_scan,
    mcp,
    _resolve_path,
    _sync_forgeignore,
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
    async def test_bad_path_returns_error(self):
        result = await forge_scan("/nonexistent/path/that/does/not/exist")
        assert "error" in result
        assert result["error"] == "invalid_path"

    @pytest.mark.asyncio
    async def test_works_without_api_key(self, tmp_path):
        """Verify forge_scan works without OPENROUTER_API_KEY set."""
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "success": True,
            "total_findings": 5,
            "agents_status": {"deterministic_only": True},
        }

        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with patch.dict(os.environ, env, clear=True), \
             patch("forge.standalone.run_standalone", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            result = await forge_scan(str(tmp_path))

        # Should return results, not an error
        assert "error" not in result
        assert result["success"] is True
        assert result["total_findings"] == 5
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_scan_exception_returns_error(self, tmp_path):
        """Verify forge_scan returns error dict on internal exception."""
        with patch("forge.standalone.run_standalone", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await forge_scan(str(tmp_path))

        assert result["error"] == "scan_failed"


class TestForgeConfig:
    """Test forge_config tool."""

    def test_returns_url_and_version(self):
        result = forge_config()
        assert "vibe2prod_url" in result
        assert "version" in result
        assert "share_forgeignore" in result

    def test_share_forgeignore_default_false(self):
        env = {k: v for k, v in os.environ.items() if k != "VIBE2PROD_DATA_SHARING"}
        with patch.dict(os.environ, env, clear=True):
            result = forge_config()
        assert result["share_forgeignore"] is False

    def test_share_forgeignore_from_env(self):
        with patch.dict(os.environ, {"VIBE2PROD_DATA_SHARING": "true"}):
            result = forge_config()
        assert result["share_forgeignore"] is True


class TestSyncForgeignore:
    """Test _sync_forgeignore consent and dispatch."""

    @pytest.mark.asyncio
    async def test_skips_without_consent(self, tmp_path):
        """Should not call sync when consent is not given."""
        env = {k: v for k, v in os.environ.items() if k != "VIBE2PROD_DATA_SHARING"}
        with patch.dict(os.environ, env, clear=True), \
             patch("forge.mcp_server.load_config", return_value={}, create=True), \
             patch("forge.execution.forgeignore.sync_forgeignore_training", new_callable=AsyncMock) as mock_sync:
            # Also patch the config_io import inside _sync_forgeignore
            with patch("forge.config_io.load_config", return_value={}):
                await _sync_forgeignore(str(tmp_path))
        mock_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_sync_with_env_consent(self, tmp_path):
        """Should call sync when VIBE2PROD_DATA_SHARING=true."""
        with patch.dict(os.environ, {"VIBE2PROD_DATA_SHARING": "true"}), \
             patch("forge.execution.forgeignore.sync_forgeignore_training", new_callable=AsyncMock) as mock_sync:
            await _sync_forgeignore(str(tmp_path))
        mock_sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_sync_with_config_consent(self, tmp_path):
        """Should call sync when config has share_forgeignore=true."""
        env = {k: v for k, v in os.environ.items() if k != "VIBE2PROD_DATA_SHARING"}
        with patch.dict(os.environ, env, clear=True), \
             patch("forge.config_io.load_config", return_value={"share_forgeignore": True}), \
             patch("forge.execution.forgeignore.sync_forgeignore_training", new_callable=AsyncMock) as mock_sync:
            await _sync_forgeignore(str(tmp_path))
        mock_sync.assert_called_once()
