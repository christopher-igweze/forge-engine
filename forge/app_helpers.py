"""Utility functions and constants shared by the FORGE pipeline.

Extracted from ``forge/app.py`` for clarity.  All names are re-exported
from ``forge.app`` so existing ``from forge.app import ...`` still works.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from forge.execution.json_utils import safe_parse_agent_response
from forge.schemas import ForgeExecutionState

NODE_ID = os.getenv("FORGE_NODE_ID", "forge-engine")
WORKSPACES_DIR = os.getenv("WORKSPACES_DIR", "/workspaces")

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────


def _resolve_repo_path(repo_url: str, repo_path: str) -> str:
    """Determine the repo path -- clone if needed, else use provided."""
    if repo_path and Path(repo_path).is_dir():
        return repo_path

    if repo_url:
        # Derive workspace path from URL
        match = re.search(r"/([^/]+?)(?:\.git)?$", repo_url.rstrip("/"))
        name = match.group(1) if match else "repo"
        workspace = os.path.join(WORKSPACES_DIR, name)

        if Path(workspace).is_dir() and any(Path(workspace).iterdir()):
            logger.info("Reusing existing workspace: %s", workspace)
            return workspace
        elif Path(workspace).is_dir():
            shutil.rmtree(workspace)
            logger.info("Removed stale empty workspace: %s", workspace)

        # Clone
        logger.info("Cloning %s → %s", repo_url, workspace)
        os.makedirs(workspace, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", repo_url, workspace],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise ValueError(
                "git is not installed or not found in PATH. "
                "Install git and retry."
            )
        except subprocess.CalledProcessError as e:
            raise ValueError(
                f"Failed to clone {repo_url}: "
                f"{e.stderr.strip() or e.stdout.strip() or str(e)}"
            )
        return workspace

    raise ValueError("Either repo_url or repo_path must be provided")


def _filter_execution_levels(
    levels: list[list[str]],
    keep_ids: set[str],
) -> list[list[str]]:
    """Filter execution levels to only include specified finding IDs."""
    filtered = []
    for level in levels:
        kept = [fid for fid in level if fid in keep_ids]
        if kept:
            filtered.append(kept)
    return filtered


def _unwrap_to_model(result):
    """Handle AgentField envelope unwrapping with resilient JSON parsing."""
    return safe_parse_agent_response(result, fallback=result)


def _build_summary(state: ForgeExecutionState) -> str:
    """Build a human-readable summary of the FORGE run."""
    parts = [f"FORGE run {state.forge_run_id}"]

    if state.all_findings:
        parts.append(f"Found {len(state.all_findings)} issues")
        by_sev = {}
        for f in state.all_findings:
            by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
        sev_str = ", ".join(f"{v} {k}" for k, v in sorted(by_sev.items()))
        parts.append(f"({sev_str})")

    if state.remediation_plan:
        parts.append(
            f"Remediation plan: {state.remediation_plan.total_items} items "
            f"across {len(state.remediation_plan.execution_levels)} levels"
        )

    if state.completed_fixes:
        from forge.schemas import FixOutcome
        actually_fixed = [
            f for f in state.completed_fixes
            if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
        ]
        if actually_fixed:
            parts.append(f"Fixed: {len(actually_fixed)}")

    if state.outer_loop.deferred_findings:
        parts.append(f"Deferred: {len(state.outer_loop.deferred_findings)}")

    return ". ".join(parts) + "."
