"""Tests for forge/cli.py — API key validation."""

import os
import pytest

from forge.cli import _check_api_key


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
