"""Live E2E model validation: MiniMax M2.5 as coder on real-world repos.

Runs FORGE full remediation against 3 forked repos of increasing complexity,
validates that MiniMax M2.5 produces quality fixes, and creates PRs.

Repos (forked to christopher-igweze):
  1. Simple:  api-server-flask          (~567 LOC,  Python Flask)
  2. Medium:  restpie3                  (~1942 LOC, Python Flask+Peewee)
  3. Complex: node-express-mongodb-jwt-rest-api-skeleton (~4613 LOC, Express+MongoDB)

Usage:
  FORGE_LIVE_TESTS=1 pytest tests/integration/test_model_validation_e2e.py -v -s
  # Or run a single tier:
  FORGE_LIVE_TESTS=1 pytest tests/integration/test_model_validation_e2e.py -k simple -v -s

Cost tracking: Each agent invocation is streamed to
  .forge-benchmarks/model_validation/<repo>/invocations_live.jsonl
so costs survive mid-run failures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge.execution.telemetry import ForgeTelemetry
from forge.schemas import ForgeMode, ForgeResult

# ── Config ────────────────────────────────────────────────────────────

GITHUB_OWNER = "christopher-igweze"

REPOS = {
    "simple": {
        "name": "api-server-flask",
        "url": f"https://github.com/{GITHUB_OWNER}/api-server-flask.git",
        "default_branch": "main",
        "description": "~567 LOC Python Flask API",
        "timeout": 900,  # 15 min
    },
    "medium": {
        "name": "restpie3",
        "url": f"https://github.com/{GITHUB_OWNER}/restpie3.git",
        "default_branch": "master",
        "description": "~1942 LOC Python Flask + Peewee",
        "timeout": 1200,  # 20 min
    },
    "complex": {
        "name": "node-express-mongodb-jwt-rest-api-skeleton",
        "url": f"https://github.com/{GITHUB_OWNER}/node-express-mongodb-jwt-rest-api-skeleton.git",
        "default_branch": "master",
        "description": "~4613 LOC Express + MongoDB",
        "timeout": 1800,  # 30 min
    },
}

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BENCHMARK_DIR = _REPO_ROOT / ".forge-benchmarks" / "model_validation"

# ── Helpers ───────────────────────────────────────────────────────────


def _skip_unless_openrouter():
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("Requires OPENROUTER_API_KEY env var")


def _clone_repo(repo_cfg: dict, dest: Path) -> Path:
    """Clone a forked repo into dest. Returns repo path."""
    repo_path = dest / repo_cfg["name"]
    if repo_path.exists():
        shutil.rmtree(repo_path)

    subprocess.run(
        ["git", "clone", "--depth=1", repo_cfg["url"], str(repo_path)],
        check=True, capture_output=True, text=True,
    )
    # Configure git user for FORGE commits
    subprocess.run(
        ["git", "config", "user.email", "forge@antigravity.dev"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "FORGE Engine"],
        cwd=repo_path, check=True, capture_output=True,
    )
    return repo_path


def _make_config() -> dict:
    """Build config for full remediation using default models (MiniMax M2.5 coders)."""
    return {
        "runtime": "open_code",
        "mode": "full",
        "dry_run": False,
        "enable_learning": True,
        "enable_github_pr": False,  # We handle PR creation ourselves
        "max_inner_retries": 2,
        "max_middle_escalations": 1,
        "max_outer_replans": 0,
    }


def _push_and_create_pr(
    repo_path: Path,
    repo_cfg: dict,
    result: ForgeResult,
) -> str | None:
    """Push FORGE changes to fork and create a PR. Returns PR URL or None."""
    # Check if FORGE made any commits beyond the initial one
    log_result = subprocess.run(
        ["git", "log", "--oneline", f"{repo_cfg['default_branch']}..HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    )

    # If no new commits on current branch, check if we're still on default branch
    # and look for any uncommitted changes
    diff_result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=repo_path, capture_output=True, text=True,
    )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout.strip()

    # If FORGE didn't create a branch, create one from current state
    branch_name = current_branch
    if current_branch == repo_cfg["default_branch"]:
        branch_name = f"forge/remediate-m2.5-{int(time.time())}"
        # Stage and commit any uncommitted changes
        subprocess.run(
            ["git", "add", "-A"], cwd=repo_path, capture_output=True,
        )
        diff_check = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if diff_check.stdout.strip():
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=repo_path, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "forge: apply MiniMax M2.5 remediation"],
                cwd=repo_path, check=True, capture_output=True,
            )
        else:
            # Check if there are commits beyond default branch
            if not log_result.stdout.strip():
                print(f"  No changes to push for {repo_cfg['name']}")
                return None
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=repo_path, check=True, capture_output=True,
            )

    # Push to fork
    push_result = subprocess.run(
        ["git", "push", "origin", branch_name, "--force"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if push_result.returncode != 0:
        print(f"  Push failed: {push_result.stderr}")
        return None

    # Create PR via gh CLI
    findings_fixed = result.findings_fixed if result else 0
    total_findings = result.total_findings if result else 0
    score = (
        result.readiness_report.overall_score
        if result and result.readiness_report
        else "N/A"
    )

    pr_body = (
        f"## FORGE Remediation (MiniMax M2.5 Validation)\n\n"
        f"- **Model**: `minimax/minimax-m2.5` (coders + synthesizer)\n"
        f"- **Findings detected**: {total_findings}\n"
        f"- **Findings fixed**: {findings_fixed}\n"
        f"- **Readiness score**: {score}\n\n"
        f"This PR was created by FORGE engine as part of E2E model validation "
        f"testing MiniMax M2.5 as a replacement for Claude Sonnet 4.6.\n\n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}"
    )

    pr_result = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", f"{GITHUB_OWNER}/{repo_cfg['name']}",
            "--title", f"forge: MiniMax M2.5 remediation ({findings_fixed}/{total_findings} fixed)",
            "--body", pr_body,
            "--base", repo_cfg["default_branch"],
            "--head", branch_name,
        ],
        cwd=repo_path, capture_output=True, text=True,
    )

    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        print(f"  PR created: {pr_url}")
        return pr_url
    else:
        print(f"  PR creation failed: {pr_result.stderr}")
        return None


def _write_summary(
    tier: str,
    repo_cfg: dict,
    result: ForgeResult | None,
    telemetry: ForgeTelemetry,
    elapsed_s: float,
    pr_url: str | None,
    error: str = "",
) -> None:
    """Write a summary JSON for this test run."""
    summary_dir = _BENCHMARK_DIR / repo_cfg["name"]
    summary_dir.mkdir(parents=True, exist_ok=True)

    tel_summary = telemetry.summary()

    entry = {
        "tier": tier,
        "repo": repo_cfg["name"],
        "description": repo_cfg["description"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed_s,
        "pr_url": pr_url,
        "error": error,
        "model_used": "minimax/minimax-m2.5",
        "total_cost_usd": tel_summary["total_cost_usd"],
        "total_tokens": tel_summary["total_tokens"],
        "total_invocations": tel_summary["total_invocations"],
        "cost_by_agent": tel_summary["cost_by_agent"],
        "cost_by_model": tel_summary["cost_by_model"],
    }

    if result:
        entry.update({
            "success": result.success,
            "total_findings": result.total_findings,
            "findings_fixed": result.findings_fixed,
            "findings_deferred": result.findings_deferred,
            "readiness_score": (
                result.readiness_report.overall_score
                if result.readiness_report else None
            ),
        })

    summary_path = summary_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(entry, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"  {tier.upper()} | {repo_cfg['name']}")
    print(f"  Cost: ${tel_summary['total_cost_usd']:.4f}")
    print(f"  Tokens: {tel_summary['total_tokens']:,}")
    print(f"  Invocations: {tel_summary['total_invocations']}")
    if result:
        print(f"  Findings: {result.total_findings} found, {result.findings_fixed} fixed")
        if result.readiness_report:
            print(f"  Score: {result.readiness_report.overall_score}")
    if pr_url:
        print(f"  PR: {pr_url}")
    if error:
        print(f"  ERROR: {error}")
    print(f"  Time: {elapsed_s:.0f}s")
    print(f"{'='*60}\n")


async def _run_forge_remediation(
    tier: str,
    repo_cfg: dict,
    tmp_path: Path,
) -> tuple[ForgeResult | None, ForgeTelemetry, str | None]:
    """Run the full FORGE cycle: clone → scan → fix → PR."""
    from forge.standalone import run_standalone

    # Set up per-action cost streaming
    log_dir = _BENCHMARK_DIR / repo_cfg["name"]
    log_dir.mkdir(parents=True, exist_ok=True)
    stream_path = str(log_dir / "invocations_live.jsonl")

    # Clear previous run data
    if os.path.exists(stream_path):
        os.remove(stream_path)

    telemetry = ForgeTelemetry(
        run_id=f"model-validation-{tier}-{repo_cfg['name']}",
        stream_log_path=stream_path,
    )

    # Clone the fork
    print(f"\n  Cloning {repo_cfg['name']}...")
    repo_path = _clone_repo(repo_cfg, tmp_path)

    config = _make_config()
    result = None
    pr_url = None
    error = ""
    start = time.monotonic()

    with telemetry.activate():
        try:
            print(f"  Running FORGE full remediation...")
            result = await run_standalone(
                repo_path=str(repo_path),
                config=config,
            )
            print(f"  FORGE completed: {result.total_findings} findings, "
                  f"{result.findings_fixed} fixed")

            # Push and create PR
            print(f"  Creating PR...")
            pr_url = _push_and_create_pr(repo_path, repo_cfg, result)

        except Exception as e:
            error = str(e)
            print(f"  FORGE failed: {error}")

    elapsed = round(time.monotonic() - start, 2)

    # Write summary (survives even if assertions fail later)
    _write_summary(tier, repo_cfg, result, telemetry, elapsed, pr_url, error)

    return result, telemetry, pr_url


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.live
class TestModelValidation:
    """Validate MiniMax M2.5 as coder model across 3 difficulty tiers."""

    @pytest.mark.timeout(900)
    @pytest.mark.asyncio
    async def test_simple_flask_api(self, tmp_path):
        """Tier 1: Simple Flask API (~567 LOC)."""
        _skip_unless_openrouter()
        repo_cfg = REPOS["simple"]

        result, telemetry, pr_url = await _run_forge_remediation(
            "simple", repo_cfg, tmp_path,
        )

        # Assertions
        assert result is not None, "FORGE should complete without error"
        assert result.success is True, "Pipeline should succeed"
        assert result.total_findings > 0, "Should detect findings in insecure Flask API"
        assert result.findings_fixed > 0, "MiniMax M2.5 should fix at least some findings"
        assert telemetry.total_cost < 2.0, f"Cost ${telemetry.total_cost:.4f} exceeds $2 budget"

    @pytest.mark.timeout(1200)
    @pytest.mark.asyncio
    async def test_medium_flask_peewee(self, tmp_path):
        """Tier 2: Medium Flask + Peewee API (~1942 LOC)."""
        _skip_unless_openrouter()
        repo_cfg = REPOS["medium"]

        result, telemetry, pr_url = await _run_forge_remediation(
            "medium", repo_cfg, tmp_path,
        )

        assert result is not None, "FORGE should complete without error"
        assert result.success is True, "Pipeline should succeed"
        assert result.total_findings > 0, "Should detect findings in Flask+Peewee API"
        assert telemetry.total_cost < 5.0, f"Cost ${telemetry.total_cost:.4f} exceeds $5 budget"

    @pytest.mark.timeout(1800)
    @pytest.mark.asyncio
    async def test_complex_express_mongodb(self, tmp_path):
        """Tier 3: Complex Express + MongoDB API (~4613 LOC)."""
        _skip_unless_openrouter()
        repo_cfg = REPOS["complex"]

        result, telemetry, pr_url = await _run_forge_remediation(
            "complex", repo_cfg, tmp_path,
        )

        assert result is not None, "FORGE should complete without error"
        assert result.success is True, "Pipeline should succeed"
        assert result.total_findings > 0, "Should detect findings in Express+MongoDB API"
        assert telemetry.total_cost < 10.0, f"Cost ${telemetry.total_cost:.4f} exceeds $10 budget"
