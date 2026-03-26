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

    @patch("forge.setup_wizard.check_mcp_registered", return_value=False)
    @patch("subprocess.run")
    def test_register_mcp_project_scope(self, mock_run, mock_check):
        mock_run.return_value = MagicMock(returncode=0)
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test", scope="project")
        self.assertTrue(result)
        cmd = mock_run.call_args[0][0]
        assert "--scope" in cmd
        scope_idx = cmd.index("--scope")
        assert cmd[scope_idx + 1] == "project"


class TestInstallSkill(unittest.TestCase):

    def _setup_fake_env(self, tmpdir, skill_name="forge"):
        """Create a fake package directory with skill source and home dir."""
        fakepkg = Path(tmpdir) / "fakepkg"
        fakepkg.mkdir()
        skill_dir = fakepkg / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill_name} Skill")
        fake_module = fakepkg / "setup_wizard.py"
        fake_module.write_text("")
        home_dir = Path(tmpdir) / "home"
        home_dir.mkdir()
        return fakepkg, fake_module, home_dir

    def test_install_skill_copies_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, fake_module, home_dir = self._setup_fake_env(tmpdir, "forge")
            import forge.setup_wizard as sw
            orig_file = sw.__file__
            try:
                sw.__file__ = str(fake_module)
                with patch("forge.setup_wizard._home_dir", return_value=home_dir):
                    result = sw.install_skill()
                    self.assertTrue(result)
                    self.assertTrue((home_dir / ".claude" / "commands" / "forge.md").exists())
            finally:
                sw.__file__ = orig_file

    def test_install_skill_custom_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fakepkg, fake_module, home_dir = self._setup_fake_env(tmpdir, "forgeignore")
            import forge.setup_wizard as sw
            orig_file = sw.__file__
            try:
                sw.__file__ = str(fake_module)
                with patch("forge.setup_wizard._home_dir", return_value=home_dir):
                    result = sw.install_skill("forgeignore")
                    self.assertTrue(result)
                    self.assertTrue((home_dir / ".claude" / "commands" / "forgeignore.md").exists())
            finally:
                sw.__file__ = orig_file

    def test_install_skill_missing_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_module = Path(tmpdir) / "setup_wizard.py"
            fake_module.write_text("")
            import forge.setup_wizard as sw
            orig_file = sw.__file__
            try:
                sw.__file__ = str(fake_module)
                result = sw.install_skill()
                self.assertFalse(result)
            finally:
                sw.__file__ = orig_file


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

    def test_headless_no_api_key_succeeds(self):
        """Headless mode works without an API key (deterministic-only)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch("forge.setup_wizard.detect_claude_code", return_value=False):
                from forge.setup_wizard import run_headless_setup
                result = run_headless_setup()
                self.assertTrue(result["success"])
                data = json.loads(config_path.read_text())
                self.assertEqual(data["openrouter_api_key"], "")

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

    def test_headless_share_forgeignore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch("forge.setup_wizard.detect_claude_code", return_value=False):
                from forge.setup_wizard import run_headless_setup
                result = run_headless_setup(api_key="sk-or-test", share_forgeignore=False)
                self.assertTrue(result["success"])
                data = json.loads(config_path.read_text())
                self.assertFalse(data["share_forgeignore"])

    def test_headless_scope_param(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.config_io.CONFIG_PATH", config_path), \
                 patch("forge.setup_wizard.detect_claude_code", return_value=True), \
                 patch("forge.setup_wizard.register_mcp", return_value=True) as mock_mcp, \
                 patch("forge.setup_wizard.install_skill", return_value=True):
                from forge.setup_wizard import run_headless_setup
                result = run_headless_setup(api_key="sk-or-test", scope="project")
                self.assertTrue(result["success"])
                mock_mcp.assert_called_once_with("sk-or-test", None, scope="project")
