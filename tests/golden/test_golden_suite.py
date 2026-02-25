"""Golden test suite — validates FORGE detection on intentionally-flawed codebases.

Tests the deterministic parts of the FORGE pipeline (file inventory,
triage classification, Tier 1 fixes) against known-flawed codebases
without requiring actual LLM calls.
"""

from __future__ import annotations

import os
import shutil

import pytest

from forge.execution.context_builder import build_codebase_inventory, build_file_tree
from forge.execution.tier_router import (
    _detect_framework,
    _tier1_replace_secret,
    _tier1_create_env_example,
    _tier1_add_error_boundary,
)
from forge.schemas import (
    AuditFinding,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
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

    def test_framework_detected(self, tmp_path):
        repo = _copy_golden("express_api_nosec", tmp_path)
        assert _detect_framework(repo) == "express"


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

    def test_framework_detected(self, tmp_path):
        repo = _copy_golden("fastapi_monolith", tmp_path)
        assert _detect_framework(repo) == "fastapi"

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

    def test_framework_detected(self, tmp_path):
        repo = _copy_golden("flask_exposed_secrets", tmp_path)
        assert _detect_framework(repo) == "flask"


# ── Tier 1 Fix Tests Against Golden Codebases ─────────────────────────


@pytest.mark.golden
class TestTier1SecretReplacement:
    def test_replaces_secrets_in_express_config(self, tmp_path):
        repo = _copy_golden("express_api_nosec", tmp_path)
        finding = AuditFinding(
            id="F-gold001", title="Hardcoded API key in config.js",
            description="API_KEY is hardcoded",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.CRITICAL,
            locations=[FindingLocation(file_path="src/config.js")],
        )
        result = _tier1_replace_secret(finding, repo)
        assert result.outcome == FixOutcome.COMPLETED
        # Verify the file was modified
        with open(os.path.join(repo, "src/config.js")) as f:
            content = f.read()
        assert "sk-live-" not in content
        assert "os.environ" in content or "process.env" in content or 'os.environ.get' in content


@pytest.mark.golden
class TestTier1EnvExample:
    def test_creates_env_example_for_express(self, tmp_path):
        repo = _copy_golden("express_api_nosec", tmp_path)
        finding = AuditFinding(
            id="F-gold002", title="Missing .env.example",
            description="No .env.example file",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
            suggested_fix="Create .env.example from .env",
        )
        result = _tier1_create_env_example(finding, repo)
        assert result.outcome == FixOutcome.COMPLETED
        example = open(os.path.join(repo, ".env.example")).read()
        assert "DB_URL=" in example
        assert "API_KEY=" in example
        assert "password123" not in example  # Values stripped

    def test_creates_env_example_for_flask(self, tmp_path):
        repo = _copy_golden("flask_exposed_secrets", tmp_path)
        finding = AuditFinding(
            id="F-gold003", title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
        )
        result = _tier1_create_env_example(finding, repo)
        assert result.outcome == FixOutcome.COMPLETED
        example = open(os.path.join(repo, ".env.example")).read()
        assert "DATABASE_URL=" in example
        assert "secret_password" not in example


@pytest.mark.golden
class TestTier1ErrorBoundary:
    def test_adds_error_boundary_to_react_app(self, tmp_path):
        repo = _copy_golden("react_app_noerror", tmp_path)
        finding = AuditFinding(
            id="F-gold004", title="Missing ErrorBoundary",
            description="React app has no ErrorBoundary",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
        )
        result = _tier1_add_error_boundary(finding, repo)
        assert result.outcome == FixOutcome.COMPLETED
        assert len(result.files_changed) >= 1

        # Verify ErrorBoundary component was created
        boundary_exists = any(
            os.path.isfile(os.path.join(repo, "src", "components", f"ErrorBoundary{ext}"))
            for ext in (".tsx", ".jsx", ".js")
        )
        assert boundary_exists


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
