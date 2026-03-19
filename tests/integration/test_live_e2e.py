"""Live E2E integration tests for FORGE engine.

These tests run the actual FORGE pipeline against real golden codebases,
using real LLM APIs via OpenRouter in standalone mode.

Requires:
  - OpenRouter API key (OPENROUTER_API_KEY env var)
  - pytest --run-live flag or FORGE_LIVE_TESTS=1

Usage:
  OPENROUTER_API_KEY=sk-or-... pytest tests/integration/test_live_e2e.py --run-live -v
  FORGE_LIVE_TESTS=1 pytest tests/integration/test_live_e2e.py -v

Each test:
  1. Copies a golden codebase to a temporary directory
  2. Initializes a git repo in the copy
  3. Calls the FORGE pipeline via run_standalone
  4. Asserts on structural properties of the ForgeResult (not exact LLM output)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forge.schemas import ForgeMode, ForgeResult

# ── Constants ────────────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden" / "codebases"

DISCOVERY_TIMEOUT = 300   # 5 minutes for discovery
SCAN_TIMEOUT = 300        # 5 minutes for scan


# ── Helpers ──────────────────────────────────────────────────────────


def _copy_golden_codebase(name: str, dest: Path) -> Path:
    """Copy a golden codebase into dest and initialize a git repo."""
    src = GOLDEN_DIR / name
    if not src.is_dir():
        pytest.fail(f"Golden codebase not found: {src}")

    repo = dest / name
    shutil.copytree(src, repo)

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "forge-test@test.local"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "FORGE Test"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, check=True, capture_output=True,
    )

    return repo


def _make_live_config(overrides: dict | None = None) -> dict:
    """Build a minimal config dict for live tests."""
    cfg: dict = {
        "runtime": "open_code",
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def _skip_unless_openrouter():
    """Skip if OPENROUTER_API_KEY is not set."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("Requires OPENROUTER_API_KEY env var")


async def _run_discover(repo_path: str, config: dict) -> ForgeResult:
    """Run discovery via run_standalone."""
    from forge.standalone import run_standalone

    cfg = dict(config)
    cfg["mode"] = "discovery"
    return await run_standalone(repo_path=repo_path, config=cfg)


# ── Discovery Tests ──────────────────────────────────────────────────


@pytest.mark.live
class TestLiveDiscovery:
    """Test the discovery pipeline against real flawed codebases."""

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_express_api(self, tmp_path):
        """Run discovery on the express_api_nosec golden codebase."""
        _skip_unless_openrouter()

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)
        config = _make_live_config({"mode": "discovery", })

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0
        assert result.findings_fixed == 0
        assert result.duration_seconds > 0
        assert result.forge_run_id

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_flask_secrets(self, tmp_path):
        """Run discovery on flask_exposed_secrets golden codebase."""
        _skip_unless_openrouter()

        repo = _copy_golden_codebase("flask_exposed_secrets", tmp_path)
        config = _make_live_config({"mode": "discovery", })

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0
        assert result.findings_fixed == 0

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_fastapi_monolith(self, tmp_path):
        """Run discovery on fastapi_monolith golden codebase."""
        _skip_unless_openrouter()

        repo = _copy_golden_codebase("fastapi_monolith", tmp_path)
        config = _make_live_config({"mode": "discovery", })

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.total_findings >= 0
        assert result.findings_fixed == 0


# ── Scan Tests ───────────────────────────────────────────────────────


@pytest.mark.live
class TestLiveScan:
    """Test the scan endpoint (alias for discover)."""

    @pytest.mark.timeout(SCAN_TIMEOUT)
    async def test_scan_returns_findings(self, tmp_path):
        """Scan should return findings without applying fixes."""
        _skip_unless_openrouter()

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)
        config = _make_live_config()

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, f"Scan should succeed: {result.summary}"
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0
        assert result.findings_fixed == 0
        assert result.forge_run_id
        assert result.duration_seconds > 0

    @pytest.mark.timeout(SCAN_TIMEOUT)
    async def test_scan_react_app(self, tmp_path):
        """Scan a React/TypeScript codebase (react_app_noerror)."""
        _skip_unless_openrouter()

        repo = _copy_golden_codebase("react_app_noerror", tmp_path)
        config = _make_live_config()

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, f"Scan should succeed: {result.summary}"
        assert result.findings_fixed == 0


# ── Pipeline Invariant Tests ─────────────────────────────────────────


@pytest.mark.live
class TestLivePipelineInvariants:
    """Tests for structural invariants that must hold across all runs."""

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_forge_run_id_is_unique(self, tmp_path):
        """Two consecutive runs must produce different forge_run_ids."""
        _skip_unless_openrouter()

        repo1 = _copy_golden_codebase("express_api_nosec", tmp_path / "run1")
        repo2 = _copy_golden_codebase("express_api_nosec", tmp_path / "run2")
        config = _make_live_config({"mode": "discovery", })

        result1 = await _run_discover(repo_path=str(repo1), config=config)
        result2 = await _run_discover(repo_path=str(repo2), config=config)

        assert result1.forge_run_id != result2.forge_run_id

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_empty_repo_does_not_crash(self, tmp_path):
        """An empty repo should produce zero findings, not crash."""
        _skip_unless_openrouter()

        repo = tmp_path / "empty-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Empty project\n")

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "forge-test@test.local"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "FORGE Test"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=repo, check=True, capture_output=True,
        )

        config = _make_live_config({"mode": "discovery", })

        result = await _run_discover(repo_path=str(repo), config=config)

        assert result.success is True, (
            f"Empty repo should not crash the pipeline: {result.summary}"
        )
        assert result.forge_run_id
