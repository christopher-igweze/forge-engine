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


def register_mcp(api_key: str, v2p_key: str | None = None, scope: str = "user", dev: bool = False) -> bool:
    """Register FORGE MCP server with Claude Code.
    Removes existing registration first to ensure env vars are up to date.
    """
    # Remove existing registration to ensure fresh env vars
    if check_mcp_registered():
        try:
            subprocess.run(
                ["claude", "mcp", "remove", "forge"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    vibe2prod_url = "https://staging.vibe2prod.verstandai.site" if dev else "https://api.vibe2prod.net"

    cmd = [
        "claude", "mcp", "add", "--scope", scope,
        "forge",
        "-e", f"OPENROUTER_API_KEY={api_key}",
        "-e", f"VIBE2PROD_URL={vibe2prod_url}",
    ]
    if v2p_key:
        cmd.extend(["-e", f"VIBE2PROD_API_KEY={v2p_key}"])
        cmd.extend(["-e", "VIBE2PROD_DATA_SHARING=true"])
    cmd.extend(["--", "forge-mcp"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def install_skill(skill_name: str = "forge") -> bool:
    """Copy a skill to Claude Code commands directory."""
    skill_src = Path(__file__).parent / "skills" / skill_name / "SKILL.md"
    if not skill_src.exists():
        return False
    skill_dst = _home_dir() / ".claude" / "commands" / f"{skill_name}.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_src, skill_dst)
    return skill_dst.exists()


# ── Config I/O (delegates to shared config_io) ─────────────────────

from forge.config_io import load_config as _load_config, save_config as _save_config, validate_config as _validate_config


# ── Headless Mode ───────────────────────────────────────────────────


def run_headless_setup(
    api_key: str | None = None,
    v2p_key: str | None = None,
    skip_claude: bool = False,
    share_forgeignore: bool = True,
    scope: str = "user",
    dev: bool = False,
) -> dict:
    """Run setup in headless mode (no prompts). Returns status dict.

    API key is optional — omitting it enables deterministic-only mode.
    """
    if api_key and not validate_api_key(api_key):
        return {"success": False, "error": "Invalid API key format. Must start with 'sk-or-'."}

    if v2p_key and not validate_v2p_key(v2p_key):
        return {"success": False, "error": "Invalid V2P key format. Must start with 'v2p_'."}

    config = _load_config()
    config["openrouter_api_key"] = api_key or ""
    config["setup_completed"] = True
    config["share_forgeignore"] = share_forgeignore

    if v2p_key:
        config.setdefault("auth", {})["api_key"] = v2p_key
        config["data_sharing"] = True
    else:
        config["data_sharing"] = False

    # Claude Code integration
    claude_integrated = False
    if not skip_claude and detect_claude_code():
        claude_integrated = register_mcp(api_key or "", v2p_key, scope=scope, dev=dev)
        if claude_integrated:
            for skill_name in ("forge", "forgeignore"):
                install_skill(skill_name)
    config["claude_code_integrated"] = claude_integrated

    # Clean unknown keys from config
    warnings = _validate_config(config)
    for w in warnings:
        if w.startswith("Unknown config key:"):
            stale_key = w.split("'")[1]
            config.pop(stale_key, None)

    _save_config(config)

    return {
        "success": True,
        "config_path": str(_config_path()),
        "claude_code": claude_integrated,
    }


# ── Interactive TUI Mode ────────────────────────────────────────────


def run_interactive_setup(dev: bool = False) -> dict:
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

    if dev:
        console.print(Panel(
            "[bold yellow]FORGE Setup Wizard[/bold yellow]\n\n"
            "[yellow]⚠ Dev mode — data syncs to STAGING[/yellow]\n"
            "https://staging.vibe2prod.verstandai.site\n\n"
            "Configure FORGE for local code auditing.\n"
            "Your code never leaves your machine.",
            title="vibe2prod (DEV MODE)",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            "[bold blue]FORGE Setup Wizard[/bold blue]\n\n"
            "Configure FORGE for local code auditing.\n"
            "Your code never leaves your machine — only LLM API calls go to OpenRouter.",
            title="vibe2prod",
            border_style="blue",
        ))

    # Step 1/6: OpenRouter API Key (now optional)
    console.print("\n[bold]Step 1/6 — OpenRouter API Key[/bold] (optional)")
    console.print("  Enter your OpenRouter API key (press Enter to skip for deterministic-only mode)")
    console.print("  Get yours at: https://openrouter.ai/keys\n")

    existing_key = config.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
    if existing_key:
        masked = existing_key[:6] + "****" + existing_key[-4:] if len(existing_key) > 10 else "****"
        console.print(f"  Existing key detected: {masked}")
        console.print("  Press Enter to keep it, or paste a new key.\n")

    api_key = ""
    while True:
        raw = Prompt.ask(
            "  OpenRouter API key",
            default=masked if existing_key else "",
            password=False if existing_key else True,
        )
        # User pressed Enter to keep existing key
        if existing_key and (not raw or raw == masked):
            api_key = existing_key
            break
        if not raw or raw.strip() == "":
            api_key = ""
            console.print("  Running in deterministic-only mode. Opengrep, scoring, quality gate,")
            console.print("  and compliance checks all work without a key.")
            console.print("  Add one later: vibe2prod config set openrouter_api_key sk-or-...")
            break
        if validate_api_key(raw):
            api_key = raw
            break
        console.print("  [red]Invalid format. Key must start with 'sk-or-'. Press Enter to skip.[/red]")

    # Step 2/6: Vibe2Prod Dashboard (optional)
    console.print("\n[bold]Step 2/6 — Vibe2Prod Dashboard[/bold] (optional)")
    console.print("  Sync scan history, cross-repo trends, and team analytics.\n")

    v2p_key = None
    existing_v2p = config.get("auth", {}).get("api_key", "")
    if Confirm.ask("  Enable dashboard sync?", default=bool(existing_v2p)):
        if existing_v2p:
            masked_v2p = existing_v2p[:4] + "****" + existing_v2p[-4:] if len(existing_v2p) > 8 else "****"
            console.print(f"  Existing key detected: {masked_v2p}")
            console.print("  Press Enter to keep it, or paste a new key.\n")
        while True:
            v2p_key = Prompt.ask(
                "  Vibe2Prod API key",
                default=masked_v2p if existing_v2p else None,
            )
            # User pressed Enter to keep existing key
            if existing_v2p and v2p_key == masked_v2p:
                v2p_key = existing_v2p
                break
            if validate_v2p_key(v2p_key or ""):
                break
            console.print("  [red]Invalid format. Key must start with 'v2p_'.[/red]")

    # Step 3/6: Data Sharing Consent
    console.print("\n[bold]Step 3/6 — Data Sharing[/bold]")
    console.print("  Help improve FORGE by sharing anonymized .forgeignore suppression data after scans.")
    console.print("  This shares suppression patterns and reasoning only — no code, file paths, or repo names.\n")
    existing_share = config.get("share_forgeignore", False)
    share = Confirm.ask("  Share anonymized suppression data?", default=existing_share)

    # Step 4/6: Claude Code Integration
    console.print("\n[bold]Step 4/6 — Claude Code Integration[/bold]")

    claude_integrated = False
    scope = "user"
    if detect_claude_code():
        console.print("  [green]Claude Code detected![/green]")
        if Confirm.ask("  Register FORGE as MCP server + install skills?", default=True):
            scope = Prompt.ask(
                "  Register for all projects (user) or just this one (project)?",
                choices=["user", "project"],
                default="user",
            )
            with console.status("  Registering MCP server..."):
                mcp_ok = register_mcp(api_key, v2p_key, scope=scope, dev=dev)
            if mcp_ok:
                console.print(f"  [green]✓[/green] MCP server registered ({scope} scope)")
                claude_integrated = True
            else:
                console.print("  [yellow]⚠[/yellow] MCP registration failed. Register manually:")
                masked_cmd_key = api_key[:6] + "****" if api_key else "<your-key>"
                console.print(f"    claude mcp add --scope {scope} forge -e OPENROUTER_API_KEY={masked_cmd_key} -- forge-mcp")

            for skill_name in ("forge", "forgeignore"):
                ok = install_skill(skill_name)
                if ok:
                    console.print(f"  [green]✓[/green] /{skill_name} skill installed")
                else:
                    console.print(f"  [yellow]⚠[/yellow] /{skill_name} skill install failed (skill file not found in package)")
    else:
        console.print("  Claude Code not detected. You can use FORGE via CLI directly:")
        console.print("    vibe2prod scan ./your-project")

    # Save config
    config["openrouter_api_key"] = api_key
    config["setup_completed"] = True
    config["claude_code_integrated"] = claude_integrated
    config["share_forgeignore"] = share
    if v2p_key:
        config.setdefault("auth", {})["api_key"] = v2p_key
        config["data_sharing"] = True
    else:
        config["data_sharing"] = config.get("data_sharing", False)

    # Clean unknown keys from config
    warnings = _validate_config(config)
    for w in warnings:
        if w.startswith("Unknown config key:"):
            stale_key = w.split("'")[1]
            config.pop(stale_key, None)

    _save_config(config)

    # Step 5/6: Getting Started
    console.print("\n[bold]Step 5/6 — Getting Started[/bold]")
    if claude_integrated:
        console.print("  You're set! Open any project and ask Claude to scan it,")
        console.print("  or type /forge to run the full audit flow.")
    else:
        console.print(
            "  Quick start:\n"
            "    vibe2prod scan ./my-app          # Scan and get findings + remediation plan\n"
            "    vibe2prod report ./my-app        # View last scan report\n"
            "    vibe2prod status ./my-app        # Check running scan progress\n"
            "\n"
            "  Works with or without an API key. Without a key, you get Opengrep SAST\n"
            "  + deterministic scoring. With a key, you also get LLM-powered analysis.\n"
            "\n"
            "  Manage .forgeignore manually to suppress false positives.\n"
            "  See: https://docs.vibe2prod.net/forgeignore"
        )

    # Step 6/6: Summary
    console.print("\n[bold]Step 6/6 — Summary[/bold]")
    masked_key = (api_key[:6] + "****") if api_key else "Not set (deterministic-only)"
    vibe2prod_url = "https://staging.vibe2prod.verstandai.site" if dev else "https://api.vibe2prod.net"
    console.print(Panel(
        f"  API Key:        {masked_key}\n"
        f"  Dashboard:      {'Enabled' if v2p_key else 'Disabled'}\n"
        f"  Data Sharing:   {'Enabled' if share else 'Disabled'}\n"
        f"  Sync URL:       {vibe2prod_url}\n"
        f"  Claude Code:    {'Integrated (' + scope + ' scope)' if claude_integrated else 'Not integrated'}\n"
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
