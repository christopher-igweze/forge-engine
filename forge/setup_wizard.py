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
