"""Tests for forge.execution.worktree — crash recovery and lock handling."""

import os
import subprocess

import pytest

from forge.execution.worktree import (
    WORKTREE_DIR,
    _unlock_worktree,
    cleanup_all_worktrees,
    create_worktree,
    get_current_branch,
    recover_worktrees,
    remove_worktree,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo for testing."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True, check=True,
    )
    # Create initial commit so HEAD exists
    test_file = os.path.join(repo, "README.md")
    with open(test_file, "w") as f:
        f.write("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, capture_output=True, check=True,
    )
    return repo


@pytest.mark.unit
class TestGetCurrentBranch:
    def test_returns_branch_name(self, git_repo):
        branch = get_current_branch(git_repo)
        assert branch in ("main", "master")

    def test_non_git_dir_returns_main(self, tmp_path):
        """A directory that isn't a git repo should return 'main'."""
        non_git = str(tmp_path / "not-a-repo")
        os.makedirs(non_git)
        branch = get_current_branch(non_git)
        assert branch == "main"


@pytest.mark.unit
class TestCreateAndRemoveWorktree:
    def test_create_worktree(self, git_repo):
        wt_path = create_worktree(git_repo, "F-test-001")
        assert os.path.isdir(wt_path)
        assert WORKTREE_DIR in wt_path
        # Clean up
        remove_worktree(git_repo, wt_path)

    def test_create_sanitizes_id(self, git_repo):
        wt_path = create_worktree(git_repo, "F/test 001")
        assert "f-test-001" in wt_path.lower()
        remove_worktree(git_repo, wt_path)

    def test_create_replaces_stale(self, git_repo):
        """Creating a worktree with the same ID replaces a stale one."""
        _wt1 = create_worktree(git_repo, "F-dup")
        # Simulate stale state by leaving it
        wt2 = create_worktree(git_repo, "F-dup")
        assert os.path.isdir(wt2)
        remove_worktree(git_repo, wt2)

    def test_remove_cleans_up(self, git_repo):
        wt_path = create_worktree(git_repo, "F-cleanup")
        assert os.path.isdir(wt_path)
        remove_worktree(git_repo, wt_path)
        assert not os.path.isdir(wt_path)


@pytest.mark.unit
class TestUnlockWorktree:
    def test_unlock_removes_lock_file(self, git_repo):
        wt_path = create_worktree(git_repo, "F-locked")
        # Simulate a lock
        wt_name = os.path.basename(wt_path)
        git_dir = os.path.join(git_repo, ".git", "worktrees", wt_name)
        if os.path.isdir(git_dir):
            lock_file = os.path.join(git_dir, "locked")
            with open(lock_file, "w") as f:
                f.write("locked by test\n")
            assert os.path.exists(lock_file)
            _unlock_worktree(git_repo, wt_path)
            assert not os.path.exists(lock_file)
        remove_worktree(git_repo, wt_path)

    def test_unlock_nonexistent_is_noop(self, git_repo):
        """Unlocking a nonexistent worktree doesn't raise."""
        _unlock_worktree(git_repo, "/nonexistent/path")


@pytest.mark.unit
class TestCleanupAllWorktrees:
    def test_cleans_up_everything(self, git_repo):
        wt1 = create_worktree(git_repo, "F-a")
        wt2 = create_worktree(git_repo, "F-b")
        cleanup_all_worktrees(git_repo)
        assert not os.path.isdir(wt1)
        assert not os.path.isdir(wt2)
        worktree_root = os.path.join(git_repo, WORKTREE_DIR)
        assert not os.path.isdir(worktree_root)

    def test_cleanup_no_worktrees_is_noop(self, git_repo):
        """Cleanup when there are no worktrees doesn't fail."""
        cleanup_all_worktrees(git_repo)


@pytest.mark.unit
class TestRecoverWorktrees:
    def test_recover_finds_nothing(self, git_repo):
        recovered = recover_worktrees(git_repo)
        assert recovered == []

    def test_recover_finds_locked_worktree(self, git_repo):
        wt_path = create_worktree(git_repo, "F-stale")
        # Lock via git so `git worktree list --porcelain` reports it
        subprocess.run(
            ["git", "worktree", "lock", wt_path],
            cwd=git_repo, capture_output=True, check=True,
        )
        recovered = recover_worktrees(git_repo)
        assert len(recovered) >= 1
        # Clean up any remains
        cleanup_all_worktrees(git_repo)
