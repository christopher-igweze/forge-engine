"""Git worktree management for parallel fix isolation.

Each Tier 2/3 fix gets its own git worktree so coders can make changes
without interfering with each other. After a fix is approved, the
worktree branch is merged back into the main branch.

Includes crash recovery: locked worktrees, stale lock files in
.git/worktrees/, and orphaned filesystem directories are all handled
so a prior crash never blocks the next FORGE run.

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


def _unlock_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove the lock file for a worktree if it exists.

    When ``git worktree remove`` is interrupted (e.g. process crash, SIGKILL),
    a ``locked`` file is left inside ``.git/worktrees/<name>/`` which prevents
    subsequent ``git worktree add`` or ``git worktree remove`` from operating
    on that worktree.  This helper detects and removes that lock file.

    Args:
        repo_path: Path to the main repository.
        worktree_path: Absolute path to the worktree directory.
    """
    wt_name = os.path.basename(worktree_path)

    # Resolve the .git directory (handles both regular repos and worktrees)
    git_dir = os.path.join(repo_path, ".git")
    if os.path.isfile(git_dir):
        # Inside a worktree — read the real gitdir path
        with open(git_dir) as f:
            git_dir = f.read().strip().removeprefix("gitdir: ")

    lock_file = os.path.join(git_dir, "worktrees", wt_name, "locked")
    if os.path.isfile(lock_file):
        try:
            os.remove(lock_file)
            logger.info("Removed stale lock file: %s", lock_file)
        except OSError as e:
            logger.warning("Could not remove lock file %s: %s", lock_file, e)


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

    # Clean up stale worktree if it exists (unlock first in case of prior crash)
    if os.path.isdir(worktree_dir):
        logger.info("Cleaning up stale worktree: %s", worktree_dir)
        _unlock_worktree(repo_path, worktree_dir)
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

    # Symlink node_modules from main repo if available (avoids re-install per worktree)
    main_nm = os.path.join(repo_path, "node_modules")
    wt_nm = os.path.join(worktree_dir, "node_modules")
    if os.path.isdir(main_nm) and not os.path.exists(wt_nm):
        try:
            os.symlink(main_nm, wt_nm)
            logger.debug("Symlinked node_modules into worktree: %s", worktree_dir)
        except OSError as e:
            logger.warning("Could not symlink node_modules: %s", e)

    return worktree_dir


def install_project_deps(repo_path: str, timeout: int = 120) -> bool:
    """Install project dependencies in the main repo before remediation.

    Detects the project type from manifest files and runs the appropriate
    install command. Called once before worktrees are created so that
    symlinks can share the installed dependencies.

    Returns True if deps were installed (or none needed), False on failure.
    """
    pkg_json = os.path.join(repo_path, "package.json")
    req_txt = os.path.join(repo_path, "requirements.txt")
    pyproject = os.path.join(repo_path, "pyproject.toml")

    installed = False

    # Node.js
    if os.path.isfile(pkg_json) and not os.path.isdir(os.path.join(repo_path, "node_modules")):
        logger.info("Installing Node.js dependencies in %s", repo_path)
        try:
            result = subprocess.run(
                ["npm", "install", "--prefer-offline", "--no-audit", "--no-fund"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                logger.info("npm install succeeded")
                installed = True
            else:
                logger.warning("npm install failed: %s", result.stderr[:500])
        except FileNotFoundError:
            logger.warning("npm not found — skipping dependency install")
        except subprocess.TimeoutExpired:
            logger.warning("npm install timed out after %ds", timeout)

    # Python
    if os.path.isfile(req_txt) and not os.path.isdir(os.path.join(repo_path, ".venv")):
        logger.info("Installing Python dependencies in %s", repo_path)
        try:
            subprocess.run(
                ["python3", "-m", "venv", ".venv"],
                cwd=repo_path, capture_output=True, text=True, timeout=30,
            )
            pip = os.path.join(repo_path, ".venv", "bin", "pip")
            result = subprocess.run(
                [pip, "install", "-r", "requirements.txt"],
                cwd=repo_path, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                logger.info("pip install succeeded")
                installed = True
            else:
                logger.warning("pip install failed: %s", result.stderr[:500])
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("Python dep install failed: %s", e)

    if not installed and not os.path.isfile(pkg_json) and not os.path.isfile(req_txt):
        logger.debug("No dependency manifest found — nothing to install")

    return True


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

    # Ensure the main repo working tree is clean before merge.
    # A prior failed merge or abort can leave dirty state that blocks checkout.
    _run_git(["checkout", "."], cwd=repo_path, check=False)
    _run_git(["clean", "-fd"], cwd=repo_path, check=False)

    # Merge the worktree branch into the target branch (from main repo)
    try:
        _run_git(["checkout", target_branch], cwd=repo_path)

        merge_msg = f"forge: merge {branch}"
        result = _run_git(
            ["merge", "--no-ff", branch, "-m", merge_msg],
            cwd=repo_path, check=False,
        )

        if result.returncode == 0:
            logger.info("Merged %s into %s", branch, target_branch)
            return True

        # Merge had conflicts — try rebase-first strategy to preserve both sides.
        # Step 1: Abort the failed merge
        logger.warning("Merge conflict for %s, attempting rebase-first strategy", branch)
        _run_git(["merge", "--abort"], cwd=repo_path, check=False)

        # Step 2: Rebase the coder branch onto target to replay changes
        rebase_result = _run_git(
            ["rebase", target_branch, branch],
            cwd=repo_path, check=False,
        )

        if rebase_result.returncode == 0:
            # Rebase succeeded — retry merge (should be fast-forward now)
            _run_git(["checkout", target_branch], cwd=repo_path, check=False)
            retry_result = _run_git(
                ["merge", "--no-ff", branch, "-m", merge_msg],
                cwd=repo_path, check=False,
            )
            if retry_result.returncode == 0:
                logger.info("Rebase-then-merge succeeded for %s into %s", branch, target_branch)
                return True
            logger.warning("Merge still failed after rebase for %s", branch)
            _run_git(["merge", "--abort"], cwd=repo_path, check=False)
        else:
            # Rebase failed — abort it
            logger.warning("Rebase failed for %s, falling back to -X theirs", branch)
            _run_git(["rebase", "--abort"], cwd=repo_path, check=False)

        # Step 3: Fall back to -X theirs (last resort, may overwrite prior fixes)
        _run_git(["checkout", target_branch], cwd=repo_path, check=False)
        result = _run_git(
            ["merge", "--no-ff", "-X", "theirs", branch, "-m", merge_msg],
            cwd=repo_path, check=False,
        )
        if result.returncode == 0:
            logger.warning("Fell back to -X theirs for %s — marking as debt", branch)
            return "debt"  # Caller should mark as COMPLETED_WITH_DEBT

        logger.error("Merge conflict for %s (even with -X theirs): %s",
                      branch, result.stderr)
        _run_git(["merge", "--abort"], cwd=repo_path, check=False)
        return False

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

    # Unlock before removal — a crash may have left a lock file
    _unlock_worktree(repo_path, worktree_path)

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

    Handles three layers of stale state:
      1. Git-tracked worktrees that are locked (prior crash).
      2. ``.git/worktrees/`` lock files that prevent pruning.
      3. ``.forge-worktrees/`` directory remains that git no longer knows about.
    """
    worktree_root = os.path.join(repo_path, WORKTREE_DIR)

    # --- Phase 1: initial prune (removes worktrees whose directories vanished)
    _run_git(["worktree", "prune"], cwd=repo_path, check=False)

    # --- Phase 2: remove lock files from .git/worktrees/ so prune/remove work
    git_wt_dir = os.path.join(repo_path, ".git", "worktrees")
    if os.path.isdir(git_wt_dir):
        for entry in os.listdir(git_wt_dir):
            lock_file = os.path.join(git_wt_dir, entry, "locked")
            if os.path.isfile(lock_file):
                try:
                    os.remove(lock_file)
                    logger.info("Removed lock file during cleanup: %s", lock_file)
                except OSError as e:
                    logger.warning("Could not remove lock file %s: %s", lock_file, e)

    # --- Phase 3: re-prune now that locks are gone
    _run_git(["worktree", "prune"], cwd=repo_path, check=False)

    # --- Phase 4: remove any FORGE worktrees that git still tracks
    if os.path.isdir(worktree_root):
        result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("worktree ") and WORKTREE_DIR in line:
                    wt_path = line.split(" ", 1)[1]
                    remove_worktree(repo_path, wt_path)

    # --- Phase 5: nuke stale filesystem dirs that git doesn't know about
    if os.path.isdir(worktree_root):
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


def recover_worktrees(repo_path: str) -> list[str]:
    """Recover from worktrees left behind by a prior crash.

    Scans all git-tracked worktrees that live under ``.forge-worktrees/``
    and removes any that are in a bad state (locked, missing HEAD, or
    otherwise unusable).  Safe to call at the start of every FORGE run.

    Args:
        repo_path: Path to the main repository.

    Returns:
        List of worktree paths that were recovered (removed).
    """
    recovered: list[str] = []

    result = _run_git(
        ["worktree", "list", "--porcelain"],
        cwd=repo_path, check=False,
    )
    if result.returncode != 0:
        logger.warning("Could not list worktrees: %s", result.stderr)
        return recovered

    # Parse porcelain output into worktree records.
    # Each record is separated by a blank line.  Fields:
    #   worktree <path>
    #   HEAD <sha>
    #   branch <ref>
    #   locked           (optional)
    #   prunable         (optional)
    current_path: str | None = None
    is_locked = False
    has_head = False

    def _flush_record() -> None:
        """Check the current record and recover if needed."""
        nonlocal current_path, is_locked, has_head
        if current_path and WORKTREE_DIR in current_path:
            needs_recovery = is_locked or not has_head
            # Also check if the directory itself is missing
            if not needs_recovery and not os.path.isdir(current_path):
                needs_recovery = True
            if needs_recovery:
                logger.info("Recovering bad worktree: %s (locked=%s, has_head=%s)",
                            current_path, is_locked, has_head)
                _unlock_worktree(repo_path, current_path)
                remove_worktree(repo_path, current_path)
                recovered.append(current_path)
        current_path = None
        is_locked = False
        has_head = False

    for line in result.stdout.splitlines() + [""]:  # trailing blank to flush
        if line.startswith("worktree "):
            _flush_record()
            current_path = line.split(" ", 1)[1]

        elif line.startswith("HEAD "):
            sha = line.split(" ", 1)[1]
            # A valid HEAD is a 40-char hex SHA
            has_head = len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)

        elif line.strip() == "locked":
            is_locked = True

        elif line == "":
            _flush_record()

    # Final prune to clean up any dangling references
    if recovered:
        _run_git(["worktree", "prune"], cwd=repo_path, check=False)
        logger.info("Recovered %d worktree(s): %s", len(recovered), recovered)

    return recovered


def get_current_branch(repo_path: str) -> str:
    """Get the current branch name of the repo."""
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "main"
