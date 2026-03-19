"""Golden test suite — validates FORGE detection on intentionally-flawed codebases.

Tests the deterministic parts of the FORGE pipeline (file inventory)
against known-flawed codebases without requiring actual LLM calls.
"""

from __future__ import annotations

import os
import shutil

import pytest

from forge.execution.context_builder import build_codebase_inventory, build_file_tree
from forge.schemas import (
    AuditFinding,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
)

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "codebases")


def _copy_golden(name: str, tmp_path) -> str:
    """Copy a golden codebase to tmp_path for isolated testing."""
    src = os.path.join(GOLDEN_DIR, name)
    dst = str(tmp_path / name)
    shutil.copytree(src, dst)
    return dst


# ── Codebase Inventory Tests ─────────────────────────────────────────


@pytest.mark.golden
class TestExpressApiInventory:
    def test_file_count(self, tmp_path):
        repo = _copy_golden("express_api_nosec", tmp_path)
        inv = build_codebase_inventory(repo)
        assert len(inv) >= 4  # index.js, config.js, users.js, health.js

    def test_language_detection(self, tmp_path):
        repo = _copy_golden("express_api_nosec", tmp_path)
        inv = build_codebase_inventory(repo)
        js_files = [f for f in inv if f["language"] == "javascript"]
        assert len(js_files) >= 4


@pytest.mark.golden
class TestReactAppInventory:
    def test_file_count(self, tmp_path):
        repo = _copy_golden("react_app_noerror", tmp_path)
        inv = build_codebase_inventory(repo)
        assert len(inv) >= 4

    def test_has_tsx_files(self, tmp_path):
        repo = _copy_golden("react_app_noerror", tmp_path)
        inv = build_codebase_inventory(repo)
        tsx_files = [f for f in inv if f["language"] == "typescript"]
        assert len(tsx_files) >= 3


@pytest.mark.golden
class TestFastapiInventory:
    def test_file_count(self, tmp_path):
        repo = _copy_golden("fastapi_monolith", tmp_path)
        inv = build_codebase_inventory(repo)
        assert len(inv) >= 3

    def test_main_is_large(self, tmp_path):
        repo = _copy_golden("fastapi_monolith", tmp_path)
        inv = build_codebase_inventory(repo)
        main = next(f for f in inv if "main.py" in f["path"])
        assert main["loc"] > 50  # God module indicator


@pytest.mark.golden
class TestFlaskInventory:
    def test_file_count(self, tmp_path):
        repo = _copy_golden("flask_exposed_secrets", tmp_path)
        inv = build_codebase_inventory(repo)
        assert len(inv) >= 1


# ── File Tree Tests ──────────────────────────────────────────────────


@pytest.mark.golden
class TestFileTree:
    @pytest.mark.parametrize("codebase", [
        "express_api_nosec",
        "react_app_noerror",
        "fastapi_monolith",
        "flask_exposed_secrets",
    ])
    def test_file_tree_produces_output(self, codebase, tmp_path):
        repo = _copy_golden(codebase, tmp_path)
        tree = build_file_tree(repo)
        assert len(tree) > 0
        assert "src/" in tree or "main.py" in tree or "app.py" in tree
