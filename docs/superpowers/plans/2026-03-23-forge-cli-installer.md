# FORGE CLI Installer & Setup Wizard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `vibe2prod setup` command with Rich TUI wizard (interactive) and headless mode (AI agents), plus config-file-backed API key resolution and Claude Code auto-integration.

**Architecture:** New `forge/setup_wizard.py` module with dual-mode wizard (TTY detection). Extends existing `_check_api_key()` in `cli.py` to fall back to `~/.vibe2prod/config.json`. Atomic config writes with `0o600` permissions. Claude Code detected via `shutil.which("claude")` + `~/.claude/` directory check.

**Tech Stack:** Typer (CLI), Rich (TUI panels/prompts/spinners), subprocess (Claude Code MCP registration), shutil/pathlib (file ops)

**Spec:** `docs/superpowers/specs/2026-03-20-forge-cli-installer-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `forge/config_io.py` | Create | Shared config I/O: `load_config()`, `save_config()` with atomic writes + 0o600 permissions |
| `forge/setup_wizard.py` | Create | Setup wizard logic: TUI flow, headless mode, Claude Code detection, MCP registration, skill install |
| `forge/cli.py` | Modify | Add `setup` subcommand, use shared config_io, extend `_check_api_key()` for config fallback, first-run check |
| `pyproject.toml` | Modify | Add `rich>=13.0` to dependencies, add `[tool.setuptools.package-data]` for SKILL.md |
| `scripts/install.sh` | Create | curl installer script (checks Python, pip installs, runs setup) |
| `tests/unit/test_setup_wizard.py` | Create | Unit tests for wizard logic, config handling, Claude Code detection, skill install |
| `tests/unit/test_cli_setup.py` | Create | Integration tests for the `setup` subcommand (headless mode) |

---

### Task 1: Add Rich dependency

**Files:**
- Modify: `pyproject.toml:8-22`

- [ ] **Step 1: Add rich to dependencies**

In `pyproject.toml`, add `rich>=13.0` to the `dependencies` list (after `pyyaml`):

```toml
dependencies = [
    "pydantic>=2.0",
    "typer>=0.12",
    "tree-sitter>=0.23",
    "tree-sitter-python>=0.23",
    "tree-sitter-javascript>=0.23",
    "tree-sitter-typescript>=0.23",
    "tree-sitter-go>=0.23",
    "tree-sitter-rust>=0.23",
    "tree-sitter-java>=0.23",
    "tree-sitter-ruby>=0.23",
    "networkx>=3.0",
    "mcp>=1.0",
    "pyyaml>=6.0",
    "rich>=13.0",
]
```

- [ ] **Step 2: Add package-data for SKILL.md distribution**

Add this section to `pyproject.toml` (after `[tool.setuptools.packages.find]`):

```toml
[tool.setuptools.package-data]
forge = ["skills/forge/SKILL.md"]
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "📦 feat(deps): add rich>=13.0 for setup wizard TUI + package-data for SKILL.md"
```

---

### Task 2: Shared config I/O module + secure cli.py

**Files:**
- Create: `forge/config_io.py`
- Modify: `forge/cli.py:42-51` (replace `_load_config` / `_save_config` with imports from `config_io`)
- Modify: `forge/cli.py:102-125` (existing `_check_api_key`)

- [ ] **Step 1: Write failing test for secure config write**

Create `tests/unit/test_cli_setup.py`:

```python
"""Tests for CLI setup command and config security."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestSaveConfig(unittest.TestCase):
    """Test secure config file writing."""

    def test_save_config_sets_600_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.cli.CONFIG_PATH", config_path):
                from forge.cli import _save_config
                _save_config({"openrouter_api_key": "sk-or-test"})
                mode = stat.S_IMODE(config_path.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_save_config_atomic_write(self):
        """Config file should not be corrupted by partial writes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            with patch("forge.cli.CONFIG_PATH", config_path):
                from forge.cli import _save_config
                # Write initial config
                _save_config({"key": "value1"})
                # Write again
                _save_config({"key": "value2"})
                data = json.loads(config_path.read_text())
                self.assertEqual(data["key"], "value2")


class TestCheckApiKeyWithConfig(unittest.TestCase):
    """Test _check_api_key falls back to config file."""

    def test_env_var_takes_precedence_over_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"openrouter_api_key": "sk-or-from-config"}))
            with patch("forge.cli.CONFIG_PATH", config_path), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-from-env"}):
                from forge.cli import _check_api_key
                key = _check_api_key(None)
                self.assertEqual(key, "sk-or-from-env")

    def test_config_file_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".vibe2prod" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"openrouter_api_key": "sk-or-from-config"}))
            with patch("forge.cli.CONFIG_PATH", config_path), \
                 patch.dict(os.environ, {}, clear=True):
                # Remove OPENROUTER_API_KEY from env if present
                os.environ.pop("OPENROUTER_API_KEY", None)
                from forge.cli import _check_api_key
                key = _check_api_key(None)
                self.assertEqual(key, "sk-or-from-config")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_cli_setup.py -v
```

Expected: FAIL (permissions not set, config fallback not implemented)

- [ ] **Step 3: Create shared config_io module**

Create `forge/config_io.py`:

```python
"""Shared config I/O — used by cli.py and setup_wizard.py.

Single source of truth for loading/saving ~/.vibe2prod/config.json
with atomic writes and secure permissions.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_PATH = Path.home() / ".vibe2prod" / "config.json"


def load_config() -> dict:
    """Load config from ~/.vibe2prod/config.json."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(data: dict) -> None:
    """Write config atomically with owner-only permissions (0o600)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=CONFIG_PATH.parent,
        prefix=".config_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: Update cli.py to use shared config_io**

In `forge/cli.py`, replace the old `_load_config` / `_save_config` / `CONFIG_PATH` with:

```python
from forge.config_io import load_config as _load_config, save_config as _save_config, CONFIG_PATH
```

Remove the old `_load_config`, `_save_config`, and `CONFIG_PATH` definitions (lines 39-51).

- [ ] **Step 5: Extend _check_api_key to fall back to config file**

In `forge/cli.py`, update `_check_api_key`:

```python
def _check_api_key(api_key: str | None) -> str:
    """Resolve the OpenRouter API key: flag > env > config file."""
    key = api_key or os.getenv("OPENROUTER_API_KEY")

    # Fall back to config file
    if not key:
        config = _load_config()
        key = config.get("openrouter_api_key")

    if not key:
        typer.echo(
            "Error: No API key found.\n"
            "Run 'vibe2prod setup' to configure, or set OPENROUTER_API_KEY:\n\n"
            "  export OPENROUTER_API_KEY=sk-or-v1-...\n"
            "  vibe2prod scan ./my-app\n",
            err=True,
        )
        raise typer.Exit(1)

    if not key.startswith("sk-or-"):
        typer.echo(
            "Warning: API key does not match expected OpenRouter format (sk-or-...).\n"
            "If this is intentional, you can ignore this warning.",
            err=True,
        )

    os.environ["OPENROUTER_API_KEY"] = key
    return key
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_cli_setup.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add forge/config_io.py forge/cli.py tests/unit/test_cli_setup.py
git commit -m "🔧 feat(cli): shared config_io module, secure writes (0o600, atomic) + config file API key fallback"
```

---

### Task 3: Setup wizard core module

**Files:**
- Create: `forge/setup_wizard.py`
- Test: `tests/unit/test_setup_wizard.py`

- [ ] **Step 1: Write failing tests for wizard helpers**

Create `tests/unit/test_setup_wizard.py`:

```python
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


class TestRegisterMCP(unittest.TestCase):

    @patch("subprocess.run")
    def test_register_mcp_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test")
        self.assertTrue(result)
        cmd = mock_run.call_args[0][0]
        assert "claude" in cmd
        assert "--scope" in cmd
        assert "user" in cmd

    @patch("subprocess.run")
    def test_register_mcp_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        from forge.setup_wizard import register_mcp
        result = register_mcp("sk-or-test")
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_register_mcp_with_v2p_key(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        from forge.setup_wizard import register_mcp
        register_mcp("sk-or-test", v2p_key="v2p_test")
        cmd = mock_run.call_args[0][0]
        # Should have both env vars
        assert any("VIBE2PROD_API_KEY" in str(c) for c in cmd)

    @patch("subprocess.run")
    def test_check_mcp_registered(self, mock_run):
        """Skip registration if already registered."""
        mock_run.return_value = MagicMock(returncode=0, stdout="forge  openrouter  forge-mcp")
        from forge.setup_wizard import check_mcp_registered
        self.assertTrue(check_mcp_registered())

    @patch("subprocess.run")
    def test_check_mcp_not_registered(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        from forge.setup_wizard import check_mcp_registered
        self.assertFalse(check_mcp_registered())


class TestInstallSkill(unittest.TestCase):

    def test_install_skill_copies_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake SKILL.md source
            src_dir = Path(tmpdir) / "skills" / "forge"
            src_dir.mkdir(parents=True)
            (src_dir / "SKILL.md").write_text("# FORGE Skill")
            # Mock destination
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
            with patch("forge.setup_wizard._config_path", return_value=config_path), \
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_setup_wizard.py -v
```

Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement forge/setup_wizard.py**

```python
"""Setup wizard — TUI (interactive) and headless (AI agent) modes.

Handles: API key collection, config file creation, Claude Code integration.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _home_dir() -> Path:
    """Return home directory (mockable for testing)."""
    return Path.home()


def _config_path() -> Path:
    """Return config file path (mockable for testing)."""
    return _home_dir() / ".vibe2prod" / "config.json"


# ── Validation ──────────────────────────────────────────────────────


def validate_api_key(key: str | None) -> bool:
    """Validate OpenRouter API key format."""
    if not key:
        return False
    return key.startswith("sk-or-")


def validate_v2p_key(key: str | None) -> bool:
    """Validate Vibe2Prod API key format. Empty is OK (optional)."""
    if key is None:
        return False
    if key == "":
        return True  # User skipped
    return key.startswith("v2p_")


# ── Claude Code Detection ───────────────────────────────────────────


def detect_claude_code() -> bool:
    """Check if Claude Code CLI is installed."""
    if shutil.which("claude") is not None:
        return True
    return _home_dir().joinpath(".claude").is_dir()


def check_mcp_registered() -> bool:
    """Check if FORGE MCP server is already registered with Claude Code."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and "forge" in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def register_mcp(api_key: str, v2p_key: str | None = None) -> bool:
    """Register FORGE MCP server with Claude Code (user-level scope).
    Skips if already registered (idempotent).
    """
    if check_mcp_registered():
        return True  # Already registered
    cmd = [
        "claude", "mcp", "add", "--scope", "user",
        "forge",
        "-e", f"OPENROUTER_API_KEY={api_key}",
    ]
    if v2p_key:
        cmd.extend(["-e", f"VIBE2PROD_API_KEY={v2p_key}"])
    cmd.extend(["--", "forge-mcp"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _skill_src_path() -> Path:
    """Return path to SKILL.md source (mockable for testing)."""
    return Path(__file__).parent / "skills" / "forge" / "SKILL.md"


def install_skill() -> bool:
    """Copy /forge skill to Claude Code commands directory."""
    skill_src = _skill_src_path()
    if not skill_src.exists():
        return False
    skill_dst = _home_dir() / ".claude" / "commands" / "forge.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_src, skill_dst)
    return skill_dst.exists()


# ── Config I/O (delegates to shared config_io) ─────────────────────

from forge.config_io import load_config as _load_config, save_config as _save_config


# ── Headless Mode ───────────────────────────────────────────────────


def run_headless_setup(
    api_key: str,
    v2p_key: str | None = None,
    skip_claude: bool = False,
) -> dict:
    """Run setup in headless mode (no prompts). Returns status dict."""
    if not validate_api_key(api_key):
        return {"success": False, "error": "Invalid API key format. Must start with 'sk-or-'."}

    if v2p_key and not validate_v2p_key(v2p_key):
        return {"success": False, "error": "Invalid V2P key format. Must start with 'v2p_'."}

    config = _load_config()
    config["openrouter_api_key"] = api_key
    config["setup_completed"] = True

    if v2p_key:
        config.setdefault("auth", {})["api_key"] = v2p_key
        config["data_sharing"] = True
    else:
        config["data_sharing"] = False

    # Claude Code integration
    claude_integrated = False
    if not skip_claude and detect_claude_code():
        claude_integrated = register_mcp(api_key, v2p_key)
        if claude_integrated:
            install_skill()
    config["claude_code_integrated"] = claude_integrated

    _save_config(config)

    return {
        "success": True,
        "config_path": str(_config_path()),
        "claude_code": claude_integrated,
    }


# ── Interactive TUI Mode ────────────────────────────────────────────


def run_interactive_setup() -> dict:
    """Run the Rich TUI setup wizard. Returns status dict."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt, Confirm
        from rich import print as rprint
    except ImportError:
        print("Error: 'rich' package not installed. Install with: pip install rich>=13.0", file=sys.stderr)
        return {"success": False, "error": "rich not installed"}

    console = Console()
    config = _load_config()

    console.print(Panel(
        "[bold blue]FORGE Setup Wizard[/bold blue]\n\n"
        "Configure FORGE for local code auditing.\n"
        "Your code never leaves your machine — only LLM API calls go to OpenRouter.",
        title="vibe2prod",
        border_style="blue",
    ))

    # Step 1/4: OpenRouter API Key
    console.print("\n[bold]Step 1/4 — OpenRouter API Key[/bold] (required)")
    console.print("Get yours at: https://openrouter.ai/keys\n")

    existing_key = config.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
    if existing_key:
        masked = existing_key[:6] + "****" + existing_key[-4:] if len(existing_key) > 10 else "****"
        console.print(f"  Existing key detected: {masked}")

    while True:
        api_key = Prompt.ask(
            "  OpenRouter API key",
            default=existing_key or None,
            password=True,
        )
        if validate_api_key(api_key):
            break
        console.print("  [red]Invalid format. Key must start with 'sk-or-'.[/red]")

    # Step 2/4: Vibe2Prod Dashboard (optional)
    console.print("\n[bold]Step 2/4 — Vibe2Prod Dashboard[/bold] (optional)")
    console.print("  Sync scan history, cross-repo trends, and team analytics.\n")

    v2p_key = None
    if Confirm.ask("  Enable dashboard sync?", default=False):
        existing_v2p = config.get("auth", {}).get("api_key", "")
        while True:
            v2p_key = Prompt.ask(
                "  Vibe2Prod API key",
                default=existing_v2p or None,
                password=True,
            )
            if validate_v2p_key(v2p_key or ""):
                break
            console.print("  [red]Invalid format. Key must start with 'v2p_'.[/red]")

    # Step 3/4: Claude Code Integration
    console.print("\n[bold]Step 3/4 — Claude Code Integration[/bold]")

    claude_integrated = False
    if detect_claude_code():
        console.print("  [green]Claude Code detected![/green]")
        if Confirm.ask("  Register FORGE as MCP server + install /forge skill?", default=True):
            with console.status("  Registering MCP server..."):
                mcp_ok = register_mcp(api_key, v2p_key)
            if mcp_ok:
                console.print("  [green]✓[/green] MCP server registered (user scope)")
                claude_integrated = True
            else:
                console.print("  [yellow]⚠[/yellow] MCP registration failed. Register manually:")
                console.print(f"    claude mcp add --scope user forge -e OPENROUTER_API_KEY={api_key[:6]}**** -- forge-mcp")

            skill_ok = install_skill()
            if skill_ok:
                console.print("  [green]✓[/green] /forge skill installed")
            else:
                console.print("  [yellow]⚠[/yellow] Skill install failed (skill file not found in package)")
    else:
        console.print("  Claude Code not detected. You can use FORGE via CLI directly:")
        console.print("    vibe2prod scan ./your-project")

    # Save config
    config["openrouter_api_key"] = api_key
    config["setup_completed"] = True
    config["claude_code_integrated"] = claude_integrated
    if v2p_key:
        config.setdefault("auth", {})["api_key"] = v2p_key
        config["data_sharing"] = True
    else:
        config["data_sharing"] = config.get("data_sharing", False)

    _save_config(config)

    # Step 4/4: Summary
    console.print("\n[bold]Step 4/4 — Summary[/bold]")
    masked_key = api_key[:6] + "****"
    console.print(Panel(
        f"  API Key:        {masked_key}\n"
        f"  Dashboard:      {'Enabled' if v2p_key else 'Disabled'}\n"
        f"  Claude Code:    {'Integrated' if claude_integrated else 'Not integrated'}\n"
        f"  Config:         {_config_path()}\n"
        f"\n  [bold green]Setup complete![/bold green] Next: vibe2prod scan ./your-project",
        title="Configuration",
        border_style="green",
    ))

    return {
        "success": True,
        "config_path": str(_config_path()),
        "claude_code": claude_integrated,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_setup_wizard.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add forge/setup_wizard.py tests/unit/test_setup_wizard.py
git commit -m "✨ feat(setup): add setup wizard module with TUI + headless modes"
```

---

### Task 4: Wire setup command into CLI

**Files:**
- Modify: `forge/cli.py`

- [ ] **Step 1: Add test for setup subcommand**

Append to `tests/unit/test_cli_setup.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_cli_setup.py::TestSetupCommand -v
```

Expected: FAIL (setup command not registered)

- [ ] **Step 3: Add setup command to cli.py**

Add this command to `forge/cli.py` (after the `auth` command, before `report`):

```python
@app.command()
def setup(
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="OpenRouter API key"),
    v2p_key: str | None = typer.Option(None, "--v2p-key", help="Vibe2Prod dashboard API key"),
    no_interactive: bool = typer.Option(False, "--no-interactive", help="Headless mode (no prompts)"),
    reset: bool = typer.Option(False, "--reset", help="Re-run wizard with existing values pre-populated"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output result as JSON"),
) -> None:
    """Configure FORGE — API keys, Claude Code integration, dashboard sync.

    Interactive mode (default):
        vibe2prod setup

    Headless mode (for AI agents):
        vibe2prod setup --api-key sk-or-... --no-interactive

    Reconfigure:
        vibe2prod setup --reset

    Example:
        vibe2prod setup --api-key $OPENROUTER_API_KEY --no-interactive --json
    """
    from forge.setup_wizard import run_headless_setup, run_interactive_setup

    # Determine mode: headless if --no-interactive or not a TTY
    headless = no_interactive or not sys.stdin.isatty()

    if headless:
        if not api_key:
            typer.echo("Error: --api-key required in headless mode.", err=True)
            raise typer.Exit(1)
        result = run_headless_setup(api_key=api_key, v2p_key=v2p_key)
    else:
        # --reset is implicit: interactive mode always pre-populates from existing config.
        # Running `vibe2prod setup` and `vibe2prod setup --reset` behave the same.
        result = run_interactive_setup()

    if json_output:
        typer.echo(json.dumps(result))
    elif not headless:
        pass  # TUI already printed everything
    else:
        if result.get("success"):
            typer.echo("Setup complete.")
        else:
            typer.echo(f"Setup failed: {result.get('error', 'unknown')}", err=True)

    raise typer.Exit(0 if result.get("success") else 2)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_cli_setup.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add forge/cli.py tests/unit/test_cli_setup.py
git commit -m "✨ feat(cli): add 'vibe2prod setup' command with headless + interactive modes"
```

---

### Task 5: First-run detection

**Files:**
- Modify: `forge/cli.py` (scan and fix commands)

- [ ] **Step 1: Add test for first-run detection**

Append to `tests/unit/test_cli_setup.py`:

```python
class TestFirstRunDetection(unittest.TestCase):

    def test_scan_without_config_or_env_shows_setup_message(self):
        from typer.testing import CliRunner
        from forge.cli import app
        runner = CliRunner()
        with patch("forge.cli.CONFIG_PATH", Path("/nonexistent/config.json")), \
             patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENROUTER_API_KEY", None)
            result = runner.invoke(app, ["scan", "/tmp"])
            self.assertIn("setup", result.output.lower())
            self.assertNotEqual(result.exit_code, 0)
```

- [ ] **Step 2: Verify test passes**

The existing `_check_api_key()` (updated in Task 2) already prints "Run 'vibe2prod setup'" when no key is found. This test should already pass.

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_cli_setup.py::TestFirstRunDetection -v
```

- [ ] **Step 3: Commit (if changes were needed)**

```bash
git add forge/cli.py tests/unit/test_cli_setup.py
git commit -m "🧪 test(cli): add first-run detection test"
```

---

### Task 6: curl install script

**Files:**
- Create: `scripts/install.sh`

- [ ] **Step 1: Create the installer script**

```bash
#!/usr/bin/env bash
# FORGE CLI Installer — https://vibe2prod.com
# Usage: curl -fsSL https://get.vibe2prod.com | bash
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BOLD}FORGE CLI Installer${NC}"
echo "===================="
echo ""

# Check Python 3.10+
check_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(check_python) || {
    echo -e "${RED}Error: Python 3.10+ not found.${NC}"
    echo "Install Python: https://www.python.org/downloads/"
    exit 1
}

echo -e "${GREEN}✓${NC} Found Python: $($PYTHON --version)"

# Install via pip or pipx
if command -v pipx &>/dev/null; then
    echo "Installing via pipx..."
    pipx install vibe2prod
elif command -v pip3 &>/dev/null; then
    echo "Installing via pip3..."
    pip3 install --user vibe2prod
elif command -v pip &>/dev/null; then
    echo "Installing via pip..."
    pip install --user vibe2prod
else
    echo -e "${RED}Error: pip not found.${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} vibe2prod installed"

# Run setup
if [ -t 0 ]; then
    echo ""
    vibe2prod setup
else
    echo ""
    echo -e "${YELLOW}Non-interactive environment detected.${NC}"
    echo "Run 'vibe2prod setup' to configure."
fi
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/install.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/install.sh
git commit -m "📦 feat(install): add curl installer script"
```

---

### Task 7: Run full test suite + verify

- [ ] **Step 1: Run all new tests**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/test_setup_wizard.py tests/unit/test_cli_setup.py -v
```

Expected: All pass

- [ ] **Step 2: Run full test suite for regressions**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. pytest tests/unit/ -q
```

Expected: All existing + new tests pass

- [ ] **Step 3: Manual smoke test (headless)**

```bash
cd /Users/christopher/Code/AntiGravity/forge-engine && PYTHONPATH=. python -m forge.cli setup --api-key sk-or-test123 --no-interactive --json
```

Expected: JSON output with `{"success": true, ...}`, config file at `~/.vibe2prod/config.json`

- [ ] **Step 4: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "🔧 fix(setup): address test/smoke-test findings"
```

---

## Dependency Graph

```
Task 1 (deps) ─────────────────────────────────────┐
Task 2 (config security + API key fallback) ────────┤
Task 3 (setup wizard module) ──────────────────────→├─→ Task 7 (full verification)
Task 4 (wire setup command) ───────────────────────→│
Task 5 (first-run detection) ──────────────────────→│
Task 6 (install script) ──────────────────────────→─┘
```

Tasks 1-6 are sequential (each builds on the prior). Task 7 is the final verification gate.
