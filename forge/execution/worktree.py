"""Git worktree management for parallel fix isolation.

Each Tier 2/3 fix gets its own git worktree so coders can make changes
without interfering with each other. After a fix is approved, the
worktree branch is merged back into the main branch.

Adapted from SWE-AF's workspace.py pattern.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Where worktrees live relative to the repo root
WORKTREE_DIR = ".forge-worktrees"


def _run_git(
    args: list[str],
    cwd: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=check,
    )


def create_worktree(
    repo_path: str,
    finding_id: str,
    base_branch: str = "HEAD",
) -> str:
    """Create an isolated git worktree for a single fix.

    Args:
        repo_path: Path to the main repository.
        finding_id: Finding ID used to name the branch/worktree.
        base_branch: Branch to base the worktree on (default: current HEAD).

    Returns:
        Absolute path to the worktree directory.
    """
    # Sanitize finding ID for branch/dir naming
    safe_id = finding_id.replace("/", "-").replace(" ", "-").lower()
    branch_name = f"forge/fix-{safe_id}"
    worktree_dir = os.path.join(repo_path, WORKTREE_DIR, f"fix-{safe_id}")

    # Clean up stale worktree if it exists
    if os.path.isdir(worktree_dir):
        logger.info("Cleaning up stale worktree: %s", worktree_dir)
        remove_worktree(repo_path, worktree_dir)

    # Ensure the worktree parent dir exists
    os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)

    # Create the worktree with a new branch
    try:
        _run_git(
            ["worktree", "add", "-b", branch_name, worktree_dir, base_branch],
            cwd=repo_path,
        )
        logger.info("Created worktree: %s (branch: %s)", worktree_dir, branch_name)
    except subprocess.CalledProcessError as e:
        # Branch may already exist — try without -b
        logger.warning("Branch %s may exist, retrying without -b: %s", branch_name, e.stderr)
        try:
            _run_git(["branch", "-D", branch_name], cwd=repo_path, check=False)
            _run_git(
                ["worktree", "add", "-b", branch_name, worktree_dir, base_branch],
                cwd=repo_path,
            )
        except subprocess.CalledProcessError as e2:
            logger.error("Failed to create worktree for %s: %s", finding_id, e2.stderr)
            raise

    return worktree_dir


def merge_worktree(
    repo_path: str,
    worktree_path: str,
    target_branch: str = "main",
) -> bool:
    """Merge a worktree's branch back into the target branch.

    Args:
        repo_path: Path to the main repository.
        worktree_path: Path to the worktree to merge.
        target_branch: Branch to merge into.

    Returns:
        True if merge succeeded, False otherwise.
    """
    # Get the branch name of the worktree
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree_path, check=False,
    )
    if result.returncode != 0:
        logger.error("Could not determine worktree branch: %s", result.stderr)
        return False

    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        logger.error("Worktree has detached HEAD — cannot merge")
        return False

    # Commit any uncommitted changes in the worktree
    status = _run_git(["status", "--porcelain"], cwd=worktree_path, check=False)
    if status.stdout.strip():
        _run_git(["add", "-A"], cwd=worktree_path, check=False)
        _run_git(
            ["commit", "-m", f"forge: auto-commit remaining changes from {branch}"],
            cwd=worktree_path, check=False,
        )

    # Merge the worktree branch into the target branch (from main repo)
    try:
        _run_git(["checkout", target_branch], cwd=repo_path)
        result = _run_git(
            ["merge", "--no-ff", branch, "-m", f"forge: merge {branch}"],
            cwd=repo_path, check=False,
        )
        if result.returncode != 0:
            logger.error("Merge conflict for %s: %s", branch, result.stderr)
            # Abort the merge
            _run_git(["merge", "--abort"], cwd=repo_path, check=False)
            return False

        logger.info("Merged %s into %s", branch, target_branch)
        return True

    except subprocess.CalledProcessError as e:
        logger.error("Merge failed for %s: %s", branch, e.stderr)
        return False


def remove_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a worktree and its branch.

    Args:
        repo_path: Path to the main repository.
        worktree_path: Path to the worktree to remove.
    """
    # Get branch name before removal
    branch = ""
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree_path, check=False,
    )
    if result.returncode == 0:
        branch = result.stdout.strip()

    # Remove the worktree
    _run_git(
        ["worktree", "remove", "--force", worktree_path],
        cwd=repo_path, check=False,
    )

    # Fallback: remove directory if git worktree remove failed
    if os.path.isdir(worktree_path):
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Clean up the branch
    if branch and branch not in ("main", "master", "HEAD"):
        _run_git(["branch", "-D", branch], cwd=repo_path, check=False)

    logger.info("Removed worktree: %s", worktree_path)


def cleanup_all_worktrees(repo_path: str) -> None:
    """Remove all FORGE worktrees and prune stale entries.

    Called after a FORGE run completes (success or failure).
    """
    worktree_root = os.path.join(repo_path, WORKTREE_DIR)

    # Prune stale worktree references
    _run_git(["worktree", "prune"], cwd=repo_path, check=False)

    # Remove the worktree directory
    if os.path.isdir(worktree_root):
        # List remaining worktrees
        result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("worktree ") and WORKTREE_DIR in line:
                    wt_path = line.split(" ", 1)[1]
                    remove_worktree(repo_path, wt_path)

        # Clean up directory
        shutil.rmtree(worktree_root, ignore_errors=True)

    # Clean up forge/* branches
    result = _run_git(
        ["branch", "--list", "forge/fix-*"],
        cwd=repo_path, check=False,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch:
                _run_git(["branch", "-D", branch], cwd=repo_path, check=False)

    logger.info("Cleaned up all FORGE worktrees")


def get_current_branch(repo_path: str) -> str:
    """Get the current branch name of the repo."""
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "main"
