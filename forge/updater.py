"""vibe2prod update — smart upgrade pipeline.

Upgrades the package and syncs all components (skills, hooks, MCP, config),
only touching what changed.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()

MANIFEST_PATH = Path(__file__).parent / "manifest.json"
VERSION_CACHE_PATH = Path.home() / ".vibe2prod" / ".version_cache.json"


def _load_manifest() -> dict:
    """Load the packaged manifest."""
    if not MANIFEST_PATH.exists():
        return {}
    return json.loads(MANIFEST_PATH.read_text())


def _get_installed_version() -> str:
    """Get the currently installed version."""
    try:
        return importlib.metadata.version("vibe2prod")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0-dev"


def _get_pypi_version() -> str | None:
    """Fetch the latest version from PyPI. Returns None on failure."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/vibe2prod/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception as e:
        logger.debug("PyPI check failed: %s", e)
        return None


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _step_package(dry_run: bool = False, force: bool = False) -> dict:
    """Step 1: Upgrade the pip package."""
    current = _get_installed_version()
    latest = _get_pypi_version()

    if latest is None:
        return {"name": "package", "status": "skipped", "detail": "PyPI unreachable"}

    if current == latest and not force:
        return {"name": "package", "status": "unchanged", "detail": f"v{current}"}

    if dry_run:
        return {"name": "package", "status": "would_update", "detail": f"{current} → {latest}"}

    # Try pip upgrade — use --no-cache-dir to avoid stale index
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", f"vibe2prod=={latest}",
             "--no-cache-dir", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"name": "package", "status": "updated", "detail": f"{current} → {latest}"}
    except subprocess.CalledProcessError as e:
        return {"name": "package", "status": "error", "detail": str(e.stderr)[:200]}


def _step_skills(dry_run: bool = False, force: bool = False) -> dict:
    """Step 2: Sync skill files to ~/.claude/commands/."""
    manifest = _load_manifest()
    skills = manifest.get("skills", {})

    if not skills:
        return {"name": "skills", "status": "skipped", "detail": "No skills in manifest"}

    updated = []
    unchanged = []
    errors = []

    for name, info in skills.items():
        src = Path(__file__).parent / info["file"]
        dst = Path(info["install_to"]).expanduser()

        if not src.exists():
            errors.append(f"{name}: source missing")
            continue

        # Check if update needed
        needs_update = force or not dst.exists()
        if not needs_update and "hash" in info:
            needs_update = not dst.exists() or _sha256_file(dst) != info["hash"]
        elif not needs_update:
            # No hash in manifest, compare content directly
            needs_update = not dst.exists() or dst.read_bytes() != src.read_bytes()

        if not needs_update:
            unchanged.append(name)
            continue

        if dry_run:
            updated.append(name)
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            updated.append(name)
        except Exception as e:
            errors.append(f"{name}: {e}")

    if errors:
        return {"name": "skills", "status": "error", "detail": ", ".join(errors)}
    if updated:
        status = "would_update" if dry_run else "updated"
        return {"name": "skills", "status": status, "detail": ", ".join(updated)}
    return {"name": "skills", "status": "unchanged"}


def _step_hooks(dry_run: bool = False, force: bool = False) -> dict:
    """Step 3: Sync Claude Code hooks."""
    manifest = _load_manifest()
    hooks = manifest.get("hooks", {})

    if not hooks:
        return {"name": "hooks", "status": "unchanged", "detail": "No hooks defined"}

    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {"name": "hooks", "status": "skipped", "detail": "Claude Code not configured"}

    # Read current settings
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        return {"name": "hooks", "status": "skipped", "detail": "Cannot read settings.json"}

    # TODO: Compare and sync hooks when hook definitions are added
    return {"name": "hooks", "status": "unchanged"}


def _step_mcp(dry_run: bool = False, force: bool = False) -> dict:
    """Step 4: Sync MCP server registration."""
    manifest = _load_manifest()
    mcp_config = manifest.get("mcp", {})

    if not mcp_config:
        return {"name": "mcp", "status": "unchanged", "detail": "No MCP config"}

    # Check if claude CLI exists
    claude_path = shutil.which("claude")
    if not claude_path:
        return {"name": "mcp", "status": "skipped", "detail": "Claude Code not installed"}

    # Check current MCP registration
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if mcp_config.get("name", "forge") in result.stdout:
            if not force:
                return {"name": "mcp", "status": "unchanged"}
    except Exception:
        pass  # If check fails, try to register anyway

    if dry_run:
        return {"name": "mcp", "status": "would_update", "detail": "Would re-register MCP server"}

    # Register
    scope = mcp_config.get("scope", "user")
    cmd = ["claude", "mcp", "add", "--scope", scope, mcp_config.get("name", "forge")]

    env_flags = []
    for env_var in mcp_config.get("env", []):
        val = os.environ.get(env_var)
        if val:
            env_flags.extend(["-e", f"{env_var}={val}"])

    cmd.extend(env_flags)
    cmd.append("--")
    cmd.append(mcp_config.get("command", "forge-mcp"))
    cmd.extend(mcp_config.get("args", []))

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
        return {"name": "mcp", "status": "updated", "detail": "MCP server re-registered"}
    except subprocess.CalledProcessError as e:
        return {"name": "mcp", "status": "error", "detail": str(e.stderr)[:200]}


def _step_config(dry_run: bool = False, force: bool = False) -> dict:
    """Step 5: Migrate config schema."""
    from forge.config_io import load_config, save_config, CONFIG_PATH

    manifest = _load_manifest()
    target_version = manifest.get("config_schema_version", 1)

    config = load_config()
    current_version = config.get("config_schema_version", 1)

    if current_version >= target_version and not force:
        return {"name": "config", "status": "unchanged", "detail": f"schema v{current_version}"}

    if dry_run:
        return {"name": "config", "status": "would_update", "detail": f"schema v{current_version} → v{target_version}"}

    # Back up before migration
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(".json.bak")
        shutil.copy2(CONFIG_PATH, backup)

    # Run migrations sequentially
    try:
        from forge.migrations import run_migrations
        config = run_migrations(config, current_version, target_version)
        save_config(config)
        return {"name": "config", "status": "migrated", "detail": f"schema v{current_version} → v{target_version}"}
    except Exception as e:
        return {"name": "config", "status": "error", "detail": f"Migration failed: {e}"}


def run_update(dry_run: bool = False, force: bool = False, json_output: bool = False) -> dict:
    """Run the full update pipeline. Returns structured result."""
    previous_version = _get_installed_version()

    steps = []
    step_functions = [
        ("Package", _step_package),
        ("Skills", _step_skills),
        ("Hooks", _step_hooks),
        ("MCP", _step_mcp),
        ("Config", _step_config),
    ]

    for label, func in step_functions:
        if not json_output:
            console.print(f"  Step: {label}...", end=" ")

        result = func(dry_run=dry_run, force=force)
        steps.append(result)

        if not json_output:
            status = result["status"]
            detail = result.get("detail", "")
            if status in ("updated", "migrated"):
                console.print(f"[green]✓ {status}[/green] {detail}")
            elif status == "unchanged":
                console.print(f"[dim]unchanged[/dim] {detail}")
            elif status in ("would_update",):
                console.print(f"[yellow]would update[/yellow] {detail}")
            elif status == "skipped":
                console.print(f"[yellow]skipped[/yellow] {detail}")
            elif status == "error":
                console.print(f"[red]error[/red] {detail}")

    new_version = _get_installed_version()
    updated_count = sum(1 for s in steps if s["status"] in ("updated", "migrated"))
    unchanged_count = sum(1 for s in steps if s["status"] == "unchanged")

    result = {
        "previous_version": previous_version,
        "new_version": new_version,
        "steps": steps,
    }

    if not json_output:
        if updated_count == 0 and not dry_run:
            console.print(f"\n  Already up to date (v{new_version})")
        else:
            console.print(f"\n  Done. {updated_count} updated, {unchanged_count} unchanged.")

    return result


def check_for_update_background() -> None:
    """Non-blocking background check for updates. Called from CLI callback."""
    import threading

    def _check():
        try:
            # Load cache
            cache = {}
            if VERSION_CACHE_PATH.exists():
                try:
                    cache = json.loads(VERSION_CACHE_PATH.read_text())
                except Exception:
                    pass

            # Check if cache is fresh
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            checked_at = cache.get("checked_at")
            ttl_hours = cache.get("ttl_hours", 24)

            if checked_at:
                try:
                    last_check = datetime.fromisoformat(checked_at)
                    if (now - last_check).total_seconds() < ttl_hours * 3600:
                        # Cache is fresh — check if we need to notify
                        _maybe_notify(cache)
                        return
                except Exception:
                    pass

            # Cache is stale — fetch from PyPI
            latest = _get_pypi_version()
            if latest is None:
                return

            current = _get_installed_version()
            cache = {
                "current": current,
                "latest": latest,
                "checked_at": now.isoformat(),
                "ttl_hours": 24,
            }

            # Write cache
            VERSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            VERSION_CACHE_PATH.write_text(json.dumps(cache, indent=2))

            _maybe_notify(cache)
        except Exception:
            pass  # Never block the main command

    def _maybe_notify(cache: dict):
        current = cache.get("current", "")
        latest = cache.get("latest", "")
        if not latest or not current or latest == current:
            return

        # Check suppression
        last_notified = cache.get("last_notified")
        if last_notified:
            try:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(last_notified)
                now = datetime.now(timezone.utc)
                if (now - last).total_seconds() < 3600:  # 1 hour suppression
                    return
            except Exception:
                pass

        # Show notification
        console.print(f"\n  [dim]Update available: {current} → {latest} — run `vibe2prod update` to upgrade[/dim]")

        # Update suppression timestamp
        from datetime import datetime, timezone
        cache["last_notified"] = datetime.now(timezone.utc).isoformat()
        try:
            VERSION_CACHE_PATH.write_text(json.dumps(cache, indent=2))
        except Exception:
            pass

    thread = threading.Thread(target=_check, daemon=True)
    thread.start()
