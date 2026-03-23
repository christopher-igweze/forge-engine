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
