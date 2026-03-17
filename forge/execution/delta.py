"""Delta mode: only scan files changed since last scan."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

HEAD_SHA_FILENAME = "last_head_sha.txt"


def get_changed_files(repo_path: str, since_sha: str | None = None) -> list[str] | None:
    """Get list of files changed since a given commit SHA.

    Returns None if delta cannot be computed (no SHA, not a git repo, etc.)
    which signals the caller to fall back to full scan.
    """
    if not since_sha:
        return None

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", since_sha, "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git diff failed: %s", result.stderr.strip())
            return None

        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        logger.info("Delta mode: %d files changed since %s", len(files), since_sha[:8])
        return files
    except Exception:
        logger.warning("Failed to compute delta, falling back to full scan", exc_info=True)
        return None


def get_head_sha(repo_path: str) -> str | None:
    """Get current HEAD commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def load_last_head_sha(artifacts_dir: str) -> str | None:
    """Load the HEAD SHA from the last scan."""
    path = Path(artifacts_dir) / HEAD_SHA_FILENAME
    if path.exists():
        try:
            sha = path.read_text().strip()
            if sha:
                return sha
        except Exception:
            pass
    return None


def save_head_sha(artifacts_dir: str, sha: str) -> None:
    """Save the current HEAD SHA for the next delta scan."""
    path = Path(artifacts_dir) / HEAD_SHA_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n")
