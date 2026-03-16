"""FORGE MCP Server — AI-powered codebase auditing.

One tool: forge_scan. Scans your codebase and returns findings.
Use the /forge skill in Claude Code to fix findings autonomously.

Usage:
    pip install vibe2prod
    claude mcp add forge -- python -m forge.mcp_server

Optional env vars:
    VIBE2PROD_API_KEY  — Link scans to your vibe2prod.net account (telemetry)
    VIBE2PROD_URL      — API base URL (default: https://api.vibe2prod.net)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

_VIBE2PROD_URL = os.environ.get("VIBE2PROD_URL", "https://api.vibe2prod.net")
_VIBE2PROD_API_KEY = os.environ.get("VIBE2PROD_API_KEY", "")

mcp = FastMCP(
    "forge",
    instructions=(
        "FORGE: AI-powered codebase auditing engine. "
        "Run forge_scan to discover security, quality, and architecture issues. "
        "Then use the /forge skill to fix them."
    ),
)


def _resolve_path(path: str) -> str:
    """Resolve and validate a repo path."""
    resolved = str(Path(path).resolve())
    if not Path(resolved).is_dir():
        raise ValueError(f"'{path}' is not a directory")
    return resolved


def _machine_id() -> str:
    """Stable anonymous machine identifier (hashed hostname + username)."""
    raw = f"{os.uname().nodename}:{os.getenv('USER', 'unknown')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _send_telemetry(event: str, data: dict) -> None:
    """Fire-and-forget telemetry POST to vibe2prod API."""
    try:
        import httpx

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if _VIBE2PROD_API_KEY:
            headers["X-API-Key"] = _VIBE2PROD_API_KEY

        payload = {"event": event, "machine_id": _machine_id(), **data}
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{_VIBE2PROD_URL}/api/telemetry",
                json=payload,
                headers=headers,
            )
    except Exception:
        pass  # Never fail on telemetry


@mcp.tool()
async def forge_scan(path: str, model: str | None = None) -> dict:
    """Scan a codebase for security, quality, and architecture issues.

    Returns a complete report with findings, severity breakdown,
    readiness score, and remediation suggestions.

    After scanning, use the /forge skill to fix the findings.

    Args:
        path: Path to the repository to scan.
        model: Optional model override (default: minimax/minimax-m2.5).

    Returns:
        Complete scan report with findings, scores, and suggestions.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        return {
            "error": "OPENROUTER_API_KEY not set",
            "message": (
                "Add your OpenRouter API key when registering the MCP server:\n\n"
                "  claude mcp add forge -e OPENROUTER_API_KEY=your-key -- python -m forge.mcp_server\n\n"
                "Get a key at https://openrouter.ai (free signup)."
            ),
        }

    from forge.standalone import run_standalone

    repo_path = _resolve_path(path)
    start = time.monotonic()

    config: dict = {
        "mode": "discovery",
        "dry_run": True,
        "repo_path": repo_path,
    }
    if model:
        config["models"] = {"default": model}

    result = await run_standalone(repo_path=repo_path, config=config)
    report = result.model_dump(mode="json")
    duration = time.monotonic() - start

    # Send telemetry (fire-and-forget)
    await _send_telemetry("scan_complete", {
        "version": "1.0.0",
        "model": model or "minimax/minimax-m2.5",
        "mode": "cli_discovery",
        "findings_count": report.get("total_findings", 0),
        "duration_seconds": round(duration, 2),
        "cost_usd": report.get("cost_usd", 0),
    })

    return report


@mcp.tool()
def forge_status(path: str) -> dict:
    """Get real-time status of a running FORGE scan.

    Shows cost, time elapsed, current phase, and active agents.
    Updated after every LLM call.

    Args:
        path: Path to the repository being scanned.
    """
    status_file = Path(path) / ".artifacts" / "telemetry" / "live_status.json"
    if not status_file.exists():
        return {"status": "no_active_run"}
    try:
        return json.loads(status_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "error"}


def main() -> None:
    """Entry point for the FORGE MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
