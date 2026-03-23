"""Live E2E tests for Hive Discovery pipeline.

These tests run the actual Hive Discovery pipeline against real golden
codebases using real LLM calls via OpenRouter. They test the full
Layer 0 -> Layer 1 -> Layer 2 pipeline end-to-end.

Requires:
  - OpenRouter API key (OPENROUTER_API_KEY env var)
  - pytest --run-live flag or FORGE_LIVE_TESTS=1

Usage:
  OPENROUTER_API_KEY=sk-or-... pytest tests/integration/test_hive_live_e2e.py --run-live -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forge.schemas import ForgeMode, ForgeResult

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden" / "codebases"

HIVE_TIMEOUT = 300  # 5 minutes


def _copy_golden_codebase(name: str, dest: Path) -> Path:
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


def _skip_unless_openrouter():
    """Skip if OPENROUTER_API_KEY is not set."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("Requires OPENROUTER_API_KEY env var")


@pytest.mark.live
class TestHiveDiscoveryLive:
    """Run the full Hive Discovery pipeline with real LLM calls."""

    @pytest.mark.timeout(HIVE_TIMEOUT)
    async def test_hive_discovery_express_api(self, tmp_path):
        """Full hive discovery on express_api_nosec golden codebase.

        Tests Layer 0 (deterministic graph), Layer 1 (swarm workers with
        real minimax-m2.5 calls), and Layer 2 (real sonnet-4.6 synthesis).

        Expects:
          - Graph built with file nodes and segments
          - Workers produce security/quality/architecture findings
          - Synthesis cross-references and deduplicates
          - Final result has CodebaseMap, findings, triage, plan
        """
        _skip_unless_openrouter()
        from forge.reasoners.hive_discovery import run_hive_discovery

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)

        result = await run_hive_discovery(
            repo_path=str(repo),
            worker_model="minimax/minimax-m2.5",
            synthesis_model="anthropic/claude-sonnet-4.6",
            ai_provider="openrouter_direct",
            target_segments=2,
            enable_wave2=True,
            worker_types=["security", "quality", "architecture"],
        )

        # Structural assertions
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "codebase_map" in result, "Missing codebase_map"
        assert "findings" in result, "Missing findings"
        assert "triage_result" in result, "Missing triage_result"
        assert "remediation_plan" in result, "Missing remediation_plan"
        assert "graph" in result, "Missing graph"
        assert "stats" in result, "Missing stats"

        # Graph was built
        graph = result["graph"]
        assert graph["stats"]["total_files"] > 0, "Graph should have files"
        assert len(graph["segments"]) > 0, "Graph should have segments"

        # Findings were produced
        findings = result["findings"]
        assert len(findings) > 0, (
            "express_api_nosec has hardcoded secrets, SQL injection — "
            "expected at least 1 finding from synthesis"
        )

        # Stats are reasonable
        stats = result["stats"]
        assert stats["total_invocations"] > 0
        assert stats["layer0_time_seconds"] >= 0
        assert stats["wave1_findings"] >= 0
        assert stats["synthesis_findings"] >= 0

        print(f"\n--- Hive Discovery Results ---")
        print(f"Segments: {stats.get('segments', '?')}")
        print(f"Wave 1 findings: {stats.get('wave1_findings', '?')}")
        print(f"Wave 2 findings: {stats.get('wave2_findings', '?')}")
        print(f"Synthesis findings: {stats.get('synthesis_findings', '?')}")
        print(f"Total invocations: {stats.get('total_invocations', '?')}")
        print(f"Total time: {stats.get('total_time_seconds', '?')}s")

        for f in findings[:5]:
            print(f"  [{f.get('severity', '?')}] {f.get('title', '?')}")

    @pytest.mark.timeout(HIVE_TIMEOUT)
    async def test_hive_discovery_flask_secrets(self, tmp_path):
        """Hive discovery on flask_exposed_secrets — Python codebase."""
        _skip_unless_openrouter()
        from forge.reasoners.hive_discovery import run_hive_discovery

        repo = _copy_golden_codebase("flask_exposed_secrets", tmp_path)

        result = await run_hive_discovery(
            repo_path=str(repo),
            worker_model="minimax/minimax-m2.5",
            synthesis_model="anthropic/claude-sonnet-4.6",
            ai_provider="openrouter_direct",
            target_segments=1,  # small codebase
            enable_wave2=True,
        )

        assert isinstance(result, dict)
        assert result["graph"]["stats"]["total_files"] > 0
        findings = result["findings"]
        assert len(findings) > 0, (
            "flask_exposed_secrets has SECRET_KEY and debug=True"
        )

        stats = result["stats"]
        print(f"\n--- Flask Hive Results ---")
        print(f"Findings: {len(findings)}, Invocations: {stats['total_invocations']}")
        print(f"Time: {stats['total_time_seconds']}s")

    @pytest.mark.timeout(HIVE_TIMEOUT)
    async def test_hive_discovery_wave2_disabled(self, tmp_path):
        """Verify Wave 2 can be disabled to reduce cost."""
        _skip_unless_openrouter()
        from forge.reasoners.hive_discovery import run_hive_discovery

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)

        result = await run_hive_discovery(
            repo_path=str(repo),
            worker_model="minimax/minimax-m2.5",
            synthesis_model="anthropic/claude-sonnet-4.6",
            ai_provider="openrouter_direct",
            target_segments=1,
            enable_wave2=False,
            worker_types=["security", "quality"],
        )

        assert isinstance(result, dict)
        stats = result["stats"]
        assert stats["wave2_findings"] == 0, "Wave 2 was disabled"
        assert stats["total_invocations"] > 0
        assert len(result["findings"]) >= 0  # may still have findings from wave 1 + synthesis

        print(f"\n--- Wave2 Disabled Results ---")
        print(f"Findings: {len(result['findings'])}")
        print(f"Invocations: {stats['total_invocations']} (no wave 2)")
        print(f"Time: {stats['total_time_seconds']}s")
