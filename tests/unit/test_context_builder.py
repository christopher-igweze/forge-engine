"""Tests for FORGE context builder -- file inventory and selection."""

import os

import pytest

from forge.execution.context_builder import (
    build_codebase_inventory,
    build_file_tree,
    read_file_safe,
    select_files_for_pass,
    _should_skip_file,
    _estimate_tokens,
    SKIP_EXTENSIONS,
)
from forge.schemas import (
    AuditPassType,
    AuthBoundaryEntry,
    CodebaseMap,
    EntryPoint,
    FileEntry,
)


@pytest.fixture
def sample_repo(tmp_path):
    """Create a sample repo structure for testing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "# Auth module\nclass AuthService:\n    pass\n"
    )
    (tmp_path / "src" / "app.ts").write_text(
        "import express from 'express';\nconst app = express();\n"
    )
    (tmp_path / "src" / "utils.py").write_text(
        "# Utility functions\ndef helper():\n    pass\n"
    )
    (tmp_path / "config.json").write_text('{"key": "value"}')
    (tmp_path / "package.json").write_text('{"name": "test", "dependencies": {}}')
    (tmp_path / "image.png").write_text("not a real png")  # Should be skipped
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("module.exports = {};")
    return tmp_path


class TestShouldSkipFile:
    def test_skip_binary_extensions(self):
        for ext in [".png", ".jpg", ".lock", ".map", ".woff"]:
            assert _should_skip_file(f"file{ext}") is True

    def test_allow_source_files(self):
        for ext in [".py", ".ts", ".js", ".tsx"]:
            assert _should_skip_file(f"file{ext}") is False

    def test_skip_node_modules(self):
        assert _should_skip_file("node_modules/dep.js") is True

    def test_skip_git_dir(self):
        assert _should_skip_file(".git/config") is True

    def test_skip_pycache(self):
        assert _should_skip_file("__pycache__/module.cpython-312.pyc") is True

    def test_skip_venv(self):
        assert _should_skip_file("venv/lib/site-packages/mod.py") is True

    def test_allow_regular_path(self):
        assert _should_skip_file("src/controllers/auth.ts") is False

    def test_skip_all_defined_extensions(self):
        """Every extension in SKIP_EXTENSIONS should be skipped."""
        for ext in SKIP_EXTENSIONS:
            assert _should_skip_file(f"test{ext}") is True, f"Expected {ext} to be skipped"


class TestBuildCodebaseInventory:
    def test_finds_source_files(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        paths = [f["path"] for f in inventory]
        assert any("auth.py" in p for p in paths)
        assert any("app.ts" in p for p in paths)

    def test_skips_node_modules(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        paths = [f["path"] for f in inventory]
        assert not any("node_modules" in p for p in paths)

    def test_skips_binary_files(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        paths = [f["path"] for f in inventory]
        assert not any("image.png" in p for p in paths)

    def test_language_detection(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        py_files = [f for f in inventory if f["path"].endswith(".py")]
        assert all(f["language"] == "python" for f in py_files)
        ts_files = [f for f in inventory if f["path"].endswith(".ts")]
        assert all(f["language"] == "typescript" for f in ts_files)

    def test_loc_counted(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        for f in inventory:
            assert f["loc"] >= 0

    def test_config_files_have_zero_loc(self, sample_repo):
        """Config/data files appear in inventory for context but loc=0."""
        inventory = build_codebase_inventory(str(sample_repo))
        json_files = [f for f in inventory if f["path"].endswith(".json")]
        assert len(json_files) > 0, "JSON files should be in inventory"
        for f in json_files:
            assert f["loc"] == 0, f"JSON file {f['path']} should have loc=0"
            assert f["language"] == "json"

    def test_returns_list_of_dicts(self, sample_repo):
        inventory = build_codebase_inventory(str(sample_repo))
        assert isinstance(inventory, list)
        for item in inventory:
            assert isinstance(item, dict)
            assert "path" in item
            assert "language" in item
            assert "loc" in item

    def test_empty_repo(self, tmp_path):
        inventory = build_codebase_inventory(str(tmp_path))
        assert inventory == []


class TestBuildFileTree:
    def test_produces_output(self, sample_repo):
        tree = build_file_tree(str(sample_repo))
        assert "src/" in tree
        assert "auth.py" in tree

    def test_skips_node_modules(self, sample_repo):
        tree = build_file_tree(str(sample_repo))
        assert "node_modules" not in tree

    def test_includes_nested_files(self, sample_repo):
        tree = build_file_tree(str(sample_repo))
        assert "app.ts" in tree
        assert "utils.py" in tree

    def test_empty_repo(self, tmp_path):
        tree = build_file_tree(str(tmp_path))
        # Should at least have the root
        assert tree  # non-empty string


class TestReadFileSafe:
    def test_reads_file(self, sample_repo):
        content = read_file_safe(str(sample_repo / "src" / "auth.py"))
        assert "AuthService" in content

    def test_truncates_large_files(self, tmp_path):
        large_file = tmp_path / "large.py"
        large_file.write_text("x" * 20000)
        content = read_file_safe(str(large_file), max_chars=100)
        assert len(content) < 200
        assert "truncated" in content

    def test_nonexistent_file(self):
        content = read_file_safe("/nonexistent/file.py")
        assert content == ""

    def test_exact_max_chars(self, tmp_path):
        """File exactly at max_chars should not be truncated."""
        exact_file = tmp_path / "exact.py"
        exact_file.write_text("x" * 100)
        content = read_file_safe(str(exact_file), max_chars=100)
        assert len(content) == 100
        assert "truncated" not in content

    def test_small_file_not_truncated(self, tmp_path):
        small_file = tmp_path / "small.py"
        small_file.write_text("hello\n")
        content = read_file_safe(str(small_file))
        assert content == "hello\n"


class TestSelectFilesForPass:
    def test_auth_pass_prioritizes_auth_files(self, sample_repo):
        codebase_map = CodebaseMap(
            files=[
                FileEntry(path="src/auth.py", language="python", loc=3),
                FileEntry(path="src/app.ts", language="typescript", loc=2),
                FileEntry(path="src/utils.py", language="python", loc=3),
            ],
            auth_boundaries=[AuthBoundaryEntry(path="src/auth.py", auth_type="jwt")],
        )
        result = select_files_for_pass(
            str(sample_repo), AuditPassType.AUTH_FLOW, codebase_map,
        )
        # Result is a formatted string, auth.py should appear before utils.py
        assert isinstance(result, str)
        auth_pos = result.find("auth.py")
        utils_pos = result.find("utils.py")
        # auth.py should appear (it has high relevance), and if utils.py appears,
        # auth.py should be first
        assert auth_pos >= 0
        if utils_pos >= 0:
            assert auth_pos < utils_pos

    def test_returns_string(self, sample_repo):
        codebase_map = CodebaseMap(
            files=[FileEntry(path="src/auth.py", language="python", loc=3)],
        )
        result = select_files_for_pass(
            str(sample_repo), AuditPassType.AUTH_FLOW, codebase_map,
        )
        assert isinstance(result, str)

    def test_empty_codebase_map(self, sample_repo):
        codebase_map = CodebaseMap()
        result = select_files_for_pass(
            str(sample_repo), AuditPassType.AUTH_FLOW, codebase_map,
        )
        assert result == "(no relevant files found)"


class TestEstimateTokens:
    def test_estimates(self):
        # CHARS_PER_TOKEN = 4, so 400 chars = 100 tokens
        assert _estimate_tokens("a" * 400) == 100

    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_short_string(self):
        # 3 chars / 4 = 0 (integer division)
        assert _estimate_tokens("abc") == 0

    def test_exact_multiple(self):
        assert _estimate_tokens("a" * 80) == 20
