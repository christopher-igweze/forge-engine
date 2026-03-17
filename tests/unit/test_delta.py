"""Tests for delta mode utilities (git HEAD SHA and changed files)."""
import subprocess

import pytest


class TestGetHeadSha:
    def test_returns_string_in_git_repo(self):
        """get_head_sha should return a hex string in the forge-engine repo."""
        from forge.execution.delta import get_head_sha

        sha = get_head_sha("/Users/christopher/Documents/AntiGravity/forge-engine")
        assert isinstance(sha, str)
        assert len(sha) == 40  # full SHA

    def test_returns_none_for_nonexistent_path(self):
        from forge.execution.delta import get_head_sha

        sha = get_head_sha("/nonexistent/path/that/doesnt/exist")
        assert sha is None


class TestGetChangedFiles:
    def test_none_sha_returns_none(self):
        from forge.execution.delta import get_changed_files

        result = get_changed_files(
            "/Users/christopher/Documents/AntiGravity/forge-engine",
            None,
        )
        assert result is None

    def test_invalid_sha_returns_none(self):
        from forge.execution.delta import get_changed_files

        result = get_changed_files(
            "/Users/christopher/Documents/AntiGravity/forge-engine",
            "0000000000000000000000000000000000000000",
        )
        assert result is None
