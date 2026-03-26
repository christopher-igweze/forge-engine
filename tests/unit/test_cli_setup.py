"""Tests for CLI setup command and config security."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.config_io import save_config
from forge.cli import _check_api_key


class TestSaveConfig(unittest.TestCase):
    """Test secure config file writing."""

    def test_save_config_sets_600_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path):
                save_config({"openrouter_api_key": "sk-or-test"})
                mode = stat.S_IMODE(config_path.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_save_config_atomic_write(self):
        """Config file should not be corrupted by partial writes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path):
                save_config({"key": "value1"})
                save_config({"key": "value2"})
                data = json.loads(config_path.read_text())
                self.assertEqual(data["key"], "value2")


class TestCheckApiKeyWithConfig(unittest.TestCase):
    """Test _check_api_key falls back to config file."""

    def test_env_var_takes_precedence_over_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"openrouter_api_key": "sk-or-from-config"}))
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-from-env"}):
                key = _check_api_key(None)
                self.assertEqual(key, "sk-or-from-env")

    def test_config_file_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"openrouter_api_key": "sk-or-from-config"}))
            env = os.environ.copy()
            env.pop("OPENROUTER_API_KEY", None)
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch.dict(os.environ, env, clear=True):
                key = _check_api_key(None)
                self.assertEqual(key, "sk-or-from-config")


class TestSetupCommand(unittest.TestCase):

    @patch("forge.setup_wizard.run_headless_setup")
    def test_headless_mode_with_api_key(self, mock_headless):
        mock_headless.return_value = {"success": True, "config_path": "/tmp/config.json", "claude_code": False}
        from typer.testing import CliRunner
        from forge.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["setup", "--api-key", "sk-or-test", "--no-interactive"])
        self.assertEqual(result.exit_code, 0)
        mock_headless.assert_called_once()

    @patch("forge.setup_wizard.run_headless_setup")
    def test_headless_json_output(self, mock_headless):
        mock_headless.return_value = {"success": True, "config_path": "/tmp/config.json", "claude_code": False}
        from typer.testing import CliRunner
        from forge.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["setup", "--api-key", "sk-or-test", "--no-interactive", "--json"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertTrue(data["success"])


class TestFirstRunDetection(unittest.TestCase):

    def test_scan_without_config_or_env_runs_deterministic_mode(self):
        """Scan without API key should run in deterministic-only mode, not fail."""
        from typer.testing import CliRunner
        from forge.cli import app
        runner = CliRunner()
        with patch("forge.config_io.CONFIG_PATH", Path("/nonexistent/config.json")), \
             patch.dict(os.environ, {}, clear=True), \
             patch("forge.cli.asyncio.run") as mock_run:
            os.environ.pop("OPENROUTER_API_KEY", None)
            mock_run.return_value = unittest.mock.MagicMock(
                success=True, forge_run_id="test", mode=unittest.mock.MagicMock(value="full"),
                duration_seconds=1.0, total_findings=0, findings_fixed=0,
                findings_deferred=0, agent_invocations=0, cost_usd=0,
                readiness_report=None, evaluation=None, aivss_score=None,
                model_dump=lambda mode: {},
            )
            result = runner.invoke(app, ["scan", "/tmp"])
            self.assertIn("deterministic", result.output.lower())
