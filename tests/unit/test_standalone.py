"""Tests for forge/standalone.py — _resolve_repo_path error handling."""

import subprocess
from unittest.mock import patch

import pytest

from forge.standalone import _resolve_repo_path


class TestResolveRepoPath:
    """Tests for _resolve_repo_path() helper."""

    def test_local_dir_returned(self, tmp_path):
        result = _resolve_repo_path("", str(tmp_path))
        assert result == str(tmp_path)

    def test_no_url_no_path_raises(self):
        with pytest.raises(ValueError, match="Either repo_url or repo_path"):
            _resolve_repo_path("", "")

    def test_clone_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
        with patch("subprocess.run") as mock_run:
            result = _resolve_repo_path("https://github.com/user/repo.git", "")
            mock_run.assert_called_once()
            assert "repo" in result

    def test_git_not_found_raises_valueerror(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(ValueError, match="git is not installed"):
                _resolve_repo_path("https://github.com/user/repo.git", "")

    def test_clone_failure_raises_valueerror(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
        err = subprocess.CalledProcessError(
            128, "git", stderr="fatal: repository not found"
        )
        with patch("subprocess.run", side_effect=err):
            with pytest.raises(ValueError, match="Failed to clone.*repository not found"):
                _resolve_repo_path("https://github.com/user/repo.git", "")
