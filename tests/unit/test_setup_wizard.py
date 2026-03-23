"""Tests for setup wizard logic."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestValidateApiKey(unittest.TestCase):

    def test_valid_openrouter_key(self):
        from forge.setup_wizard import validate_api_key
        self.assertTrue(validate_api_key("sk-or-v1-abc123"))

    def test_invalid_prefix(self):
        from forge.setup_wizard import validate_api_key
        self.assertFalse(validate_api_key("sk-abc123"))

    def test_empty_key(self):
        from forge.setup_wizard import validate_api_key
        self.assertFalse(validate_api_key(""))

    def test_none_key(self):
        from forge.setup_wizard import validate_api_key
        self.assertFalse(validate_api_key(None))


class TestValidateV2PKey(unittest.TestCase):

    def test_valid_v2p_key(self):
        from forge.setup_wizard import validate_v2p_key
        self.assertTrue(validate_v2p_key("v2p_abc123"))

    def test_invalid_prefix(self):
        from forge.setup_wizard import validate_v2p_key
        self.assertFalse(validate_v2p_key("abc123"))

    def test_empty_is_valid(self):
        """Empty string means user skipped — that's OK."""
        from forge.setup_wizard import validate_v2p_key
        self.assertTrue(validate_v2p_key(""))


class TestDetectClaudeCode(unittest.TestCase):

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_detected_via_which(self, mock_which):
        from forge.setup_wizard import detect_claude_code
        self.assertTrue(detect_claude_code())

    @patch("shutil.which", return_value=None)
    def test_detected_via_directory(self, mock_which):
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir) / ".claude"
            claude_dir.mkdir()
            with patch("forge.setup_wizard._home_dir", return_value=Path(tmpdir)):
                from forge.setup_wizard import detect_claude_code
                self.assertTrue(detect_claude_code())

    @patch("shutil.which", return_value=None)
    def test_not_detected(self, mock_which):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("forge.setup_wizard._home_dir", return_value=Path(tmpdir)):
                from forge.setup_wizard import detect_claude_code
                self.assertFalse(detect_claude_code())


class TestCheckMCPRegistered(unittest.TestCase):

    @patch("subprocess.run")
    def test_registered(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="forge  openrouter  forge-mcp")
        from forge.setup_wizard import check_mcp_registered
        self.assertTrue(check_mcp_registered())

    @patch("subprocess.run")
    def test_not_registered(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from forge.setup_wizard import check_mcp_registered
        self.assertFalse(check_mcp_registered())

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_claude_not_found(self, mock_run):
        from forge.setup_wizard import check_mcp_registered
        self.assertFalse(check_mcp_registered())


class TestRegisterMCP(unittest.TestCase):

    @patch("forge.setup_wizard.check_mcp_registered", return_value=False)
    @patch("subprocess.run")
    def test_register_mcp_success(self, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test")
        self.assertTrue(result)
        cmd = mock_run.call_args[0][0]
        assert "claude" in cmd
        assert "--scope" in cmd
        assert "user" in cmd

    @patch("forge.setup_wizard.check_mcp_registered", return_value=False)
    @patch("subprocess.run")
    def test_register_mcp_failure(self, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=1)
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test")
        self.assertFalse(result)

    @patch("forge.setup_wizard.check_mcp_registered", return_value=True)
    def test_register_mcp_already_registered(self, mock_check):
        """Idempotent: skip if already registered."""
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test")
        self.assertTrue(result)

    @patch("forge.setup_wizard.check_mcp_registered", return_value=False)
    @patch("subprocess.run")
    def test_register_mcp_with_v2p_key(self, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        from forge.setup_wizard import register_mcp
        register_mcp("sk-or-test", v2p_key="v2p_test")
        cmd = mock_run.call_args[0][0]
        assert any("VIBE2PROD_API_KEY" in str(c) for c in cmd)


class TestInstallSkill(unittest.TestCase):

    def test_install_skill_copies_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "skills" / "forge"
            src_dir.mkdir(parents=True)
            (src_dir / "SKILL.md").write_text("# FORGE Skill")
            dst_dir = Path(tmpdir) / ".claude" / "commands"
            with patch("forge.setup_wizard._home_dir", return_value=Path(tmpdir)), \
                 patch("forge.setup_wizard._skill_src_path", return_value=src_dir / "SKILL.md"):
                from forge.setup_wizard import install_skill
                result = install_skill()
                self.assertTrue(result)
                self.assertTrue((dst_dir / "forge.md").exists())

    def test_install_skill_missing_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("forge.setup_wizard._skill_src_path", return_value=Path(tmpdir) / "nonexistent"):
                from forge.setup_wizard import install_skill
                result = install_skill()
                self.assertFalse(result)


class TestHeadlessSetup(unittest.TestCase):

    def test_headless_writes_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch("forge.setup_wizard.detect_claude_code", return_value=False):
                from forge.setup_wizard import run_headless_setup
                result = run_headless_setup(api_key="sk-or-test123")
                self.assertTrue(result["success"])
                data = json.loads(config_path.read_text())
                self.assertEqual(data["openrouter_api_key"], "sk-or-test123")
                self.assertTrue(data["setup_completed"])

    def test_headless_invalid_key_fails(self):
        from forge.setup_wizard import run_headless_setup
        result = run_headless_setup(api_key="invalid-key")
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_headless_with_v2p_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch("forge.setup_wizard.detect_claude_code", return_value=False):
                from forge.setup_wizard import run_headless_setup
                result = run_headless_setup(api_key="sk-or-test", v2p_key="v2p_test")
                self.assertTrue(result["success"])
                data = json.loads(config_path.read_text())
                self.assertEqual(data["auth"]["api_key"], "v2p_test")
                self.assertTrue(data["data_sharing"])

    def test_headless_invalid_v2p_key_fails(self):
        from forge.setup_wizard import run_headless_setup
        result = run_headless_setup(api_key="sk-or-test", v2p_key="bad-key")
        self.assertFalse(result["success"])
