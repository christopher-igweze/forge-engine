"""Tests for forge/app.py — _resolve_repo_path error handling."""

import subprocess
from unittest.mock import patch

import pytest

from forge.app import _resolve_repo_path


class TestAppResolveRepoPath:
    """Tests for app.py's _resolve_repo_path() helper."""

    def test_git_not_found_raises_valueerror(self, tmp_path, monkeypatch):
        monkeypatch.setattr("forge.app.WORKSPACES_DIR", str(tmp_path / "ws"))
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(ValueError, match="git is not installed"):
                _resolve_repo_path("https://github.com/user/repo.git", "")

    def test_clone_failure_raises_valueerror(self, tmp_path, monkeypatch):
        monkeypatch.setattr("forge.app.WORKSPACES_DIR", str(tmp_path / "ws"))
        err = subprocess.CalledProcessError(
            128, "git", stderr="fatal: repository not found"
        )
        with patch("subprocess.run", side_effect=err):
            with pytest.raises(ValueError, match="Failed to clone.*repository not found"):
                _resolve_repo_path("https://github.com/user/repo.git", "")

    def test_local_dir_returned(self, tmp_path):
        result = _resolve_repo_path("", str(tmp_path))
        assert result == str(tmp_path)
