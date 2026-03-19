"""Tests for forge/cli.py — API key validation and CLI commands."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from forge.cli import app, _check_api_key

runner = CliRunner()


class TestCheckApiKey:
    """Tests for _check_api_key() helper."""

    def test_key_from_argument(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        result = _check_api_key("sk-or-v1-test123")
        assert result == "sk-or-v1-test123"

    def test_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-envkey")
        result = _check_api_key(None)
        assert result == "sk-or-v1-envkey"

    def test_argument_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-envkey")
        result = _check_api_key("sk-or-v1-argkey")
        assert result == "sk-or-v1-argkey"

    def test_missing_key_exits(self, monkeypatch):
        from click.exceptions import Exit
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(Exit):
            _check_api_key(None)

    def test_invalid_format_warns(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        result = _check_api_key("bad-key-format")
        assert result == "bad-key-format"
        captured = capsys.readouterr()
        assert "does not match expected OpenRouter format" in captured.err

    def test_valid_format_no_warning(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        _check_api_key("sk-or-v1-validkey123")
        captured = capsys.readouterr()
        assert "does not match" not in captured.err

    def test_sets_env_variable(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        _check_api_key("sk-or-v1-settest")
        assert os.environ["OPENROUTER_API_KEY"] == "sk-or-v1-settest"


class TestStatusCommand:
    def test_status_no_active_run(self, tmp_path):
        """status command on repo with no active run shows error."""
        (tmp_path / ".git").mkdir()
        result = runner.invoke(app, ["status", str(tmp_path)])
        assert result.exit_code == 1
        assert "No active run" in result.output

    def test_status_with_live_data(self, tmp_path):
        """status command displays live telemetry data."""
        (tmp_path / ".git").mkdir()
        tel_dir = tmp_path / ".artifacts" / "telemetry"
        tel_dir.mkdir(parents=True)
        (tel_dir / "live_status.json").write_text(json.dumps({
            "phase": "discovery",
            "elapsed_human": "1m 30s",
            "budget": {"cost_spent": 0.25, "cost_limit": 5.0, "cost_percent": 5.0,
                       "time_limit": 1800, "time_percent": 5.0},
            "totals": {"invocations": 8, "failed": 0},
            "findings": {"total": 0, "fixed": 0, "deferred": 0, "in_progress": 0},
            "active_agents": [],
            "phases_completed": [],
        }))
        result = runner.invoke(app, ["status", str(tmp_path)])
        assert result.exit_code == 0
        assert "discovery" in result.output
        assert "$0.25" in result.output


class TestConfigCommands:
    def test_config_set_and_get(self, tmp_path):
        """config set followed by config get returns the value."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            result = runner.invoke(app, ["config", "set", "models.default", "minimax/MiniMax-M1"])
            assert result.exit_code == 0
            assert "minimax/MiniMax-M1" in result.output

            result = runner.invoke(app, ["config", "get", "models.default"])
            assert result.exit_code == 0
            assert "minimax/MiniMax-M1" in result.output

    def test_config_get_all(self, tmp_path):
        """config get without key shows all config."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            runner.invoke(app, ["config", "set", "foo", "bar"])
            result = runner.invoke(app, ["config", "get"])
            assert result.exit_code == 0
            assert "foo" in result.output

    def test_config_get_missing_key(self, tmp_path):
        """config get on missing key shows error."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            result = runner.invoke(app, ["config", "get", "nonexistent"])
            assert result.exit_code == 1

    def test_config_set_json_value(self, tmp_path):
        """config set parses JSON values (bools, numbers)."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            result = runner.invoke(app, ["config", "set", "verbose", "true"])
            assert result.exit_code == 0
            assert "True" in result.output

            result = runner.invoke(app, ["config", "set", "retries", "5"])
            assert result.exit_code == 0
            assert "5" in result.output


class TestAuthCommand:
    def test_auth_login_placeholder(self):
        """auth login shows coming soon message."""
        result = runner.invoke(app, ["auth", "login"])
        assert result.exit_code == 0
        assert "coming soon" in result.output.lower() or "locally" in result.output.lower()

    def test_auth_status_not_authenticated(self, tmp_path):
        """auth status when not logged in."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            result = runner.invoke(app, ["auth", "status"])
            assert result.exit_code == 0
            assert "Not authenticated" in result.output

    def test_auth_logout(self, tmp_path):
        """auth logout clears auth config."""
        with patch("forge.cli.CONFIG_PATH", tmp_path / "config.json"):
            result = runner.invoke(app, ["auth", "logout"])
            assert result.exit_code == 0
            assert "Logged out" in result.output

    def test_auth_unknown_action(self):
        """auth with unknown action shows error."""
        result = runner.invoke(app, ["auth", "bogus"])
        assert result.exit_code == 1


class TestScanFlags:
    def test_scan_has_max_cost_flag(self):
        """scan --help shows --max-cost flag."""
        result = runner.invoke(app, ["scan", "--help"])
        assert "--max-cost" in result.output

    def test_scan_has_max_time_flag(self):
        """scan --help shows --max-time flag."""
        result = runner.invoke(app, ["scan", "--help"])
        assert "--max-time" in result.output

    def test_fix_has_max_cost_flag(self):
        """fix --help shows --max-cost flag."""
        result = runner.invoke(app, ["fix", "--help"])
        assert "--max-cost" in result.output

    def test_fix_has_max_time_flag(self):
        """fix --help shows --max-time flag."""
        result = runner.invoke(app, ["fix", "--help"])
        assert "--max-time" in result.output


class TestScanWithEvaluation:
    @patch("forge.standalone.run_standalone")
    def test_scan_prints_evaluation(self, mock_run, tmp_path):
        """scan command prints v3 evaluation when present."""
        (tmp_path / ".git").mkdir()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.forge_run_id = "test-123"
        mock_result.mode.value = "discovery"
        mock_result.duration_seconds = 10.0
        mock_result.total_findings = 5
        mock_result.findings_fixed = 0
        mock_result.findings_deferred = 0
        mock_result.agent_invocations = 3
        mock_result.cost_usd = 0.05
        mock_result.readiness_report = None
        mock_result.evaluation = {
            "scores": {"composite": 72, "band": "B", "band_label": "Near Ready"},
            "quality_gate": {"passed": True, "profile": "forge-way", "failures": []},
        }
        mock_run.return_value = mock_result

        result = runner.invoke(app, ["scan", str(tmp_path)], env={"OPENROUTER_API_KEY": "sk-or-test"})
        assert "72/100" in result.output or "Evaluation" in result.output
