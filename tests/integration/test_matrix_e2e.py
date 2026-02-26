"""3x3 Matrix E2E tests: Size (small/medium/large) × Complexity (simple/decent/complex).

Clones real open-source repos from GitHub and runs FORGE discovery against them.
Tests are discovery-only (dry_run=True) to keep costs manageable while still
exercising the full pipeline (Agents 1-6: analyst + 3 auditors + classifier + strategist).

Matrix:
  ┌──────────┬───────────────────────────┬────────────────────────────────┬──────────────────────────────────┐
  │          │ Simple                    │ Decent                         │ Complex                          │
  ├──────────┼───────────────────────────┼────────────────────────────────┼──────────────────────────────────┤
  │ Small    │ fabric (5.4k LOC)         │ open-saas (10.9k LOC)          │ fastapi-fullstack (12.6k LOC)    │
  │ 5-10k    │ Python AI scripts         │ Wasp SaaS starter, vibecoded   │ Full-stack FastAPI+React+Docker  │
  ├──────────┼───────────────────────────┼────────────────────────────────┼──────────────────────────────────┤
  │ Medium   │ httpie (19k LOC)          │ screenshot-to-code (19.2k LOC) │ chatbot-ui (26.9k LOC)           │
  │ 15-30k   │ Python HTTP client        │ AI vibecoded app               │ Multi-provider AI chat, complex  │
  ├──────────┼───────────────────────────┼────────────────────────────────┼──────────────────────────────────┤
  │ Large    │ juice-shop (90k LOC)      │ ghostfolio (71k LOC)           │ jan (89k LOC)                    │
  │ 50k+     │ OWASP intentionally vuln  │ Investment tracker, TS modules │ AI desktop app, Electron+TS      │
  └──────────┴───────────────────────────┴────────────────────────────────┴──────────────────────────────────┘

Requires:
  - OPENROUTER_API_KEY env var
  - pytest --run-live flag or FORGE_LIVE_TESTS=1
  - Internet access (clones repos from GitHub)

Usage:
  OPENROUTER_API_KEY=sk-or-... pytest tests/integration/test_matrix_e2e.py --run-live -v -s
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from forge.schemas import ForgeMode, ForgeResult

# ── Constants ────────────────────────────────────────────────────────

# Timeouts scale with repo size
SMALL_TIMEOUT = 600    # 10 min for 5-10k LOC
MEDIUM_TIMEOUT = 900   # 15 min for 15-30k LOC
LARGE_TIMEOUT = 1800   # 30 min for 50k+ LOC

# Workspace for cloned repos (reused across test runs to avoid re-cloning)
WORKSPACES_DIR = Path(os.environ.get(
    "FORGE_TEST_WORKSPACES",
    "/tmp/forge-matrix-workspaces",
))

# ── Repo definitions ────────────────────────────────────────────────

REPOS = {
    # Small / Simple
    "fabric": {
        "url": "https://github.com/danielmiessler/fabric",
        "loc": "~5.4k",
        "size": "small",
        "complexity": "simple",
        "description": "Python AI framework — simple scripts, flat structure",
    },
    # Small / Decent
    "open-saas": {
        "url": "https://github.com/wasp-lang/open-saas",
        "loc": "~10.9k",
        "size": "small",
        "complexity": "decent",
        "description": "Wasp SaaS template — vibecoded starter with auth, payments",
    },
    # Small / Complex
    "fastapi-fullstack": {
        "url": "https://github.com/tiangolo/full-stack-fastapi-template",
        "loc": "~12.6k",
        "size": "small",
        "complexity": "complex",
        "description": "Full-stack FastAPI+React — Docker, auth, CRUD, multi-layer",
    },
    # Medium / Simple
    "httpie": {
        "url": "https://github.com/httpie/cli",
        "loc": "~19k",
        "size": "medium",
        "complexity": "simple",
        "description": "Python HTTP client — clean CLI tool, straightforward",
    },
    # Medium / Decent
    "screenshot-to-code": {
        "url": "https://github.com/abi/screenshot-to-code",
        "loc": "~19.2k",
        "size": "medium",
        "complexity": "decent",
        "description": "AI app — vibecoded, converts screenshots to code",
    },
    # Medium / Complex
    "chatbot-ui": {
        "url": "https://github.com/mckaywrigley/chatbot-ui",
        "loc": "~26.9k",
        "size": "medium",
        "complexity": "complex",
        "description": "Multi-provider AI chat — complex state, auth, Supabase",
    },
    # Large / Simple
    "juice-shop": {
        "url": "https://github.com/juice-shop/juice-shop",
        "loc": "~90k",
        "size": "large",
        "complexity": "simple",
        "description": "OWASP Juice Shop — intentionally insecure, flat patterns",
    },
    # Large / Decent
    "ghostfolio": {
        "url": "https://github.com/ghostfolio/ghostfolio",
        "loc": "~71k",
        "size": "large",
        "complexity": "decent",
        "description": "Investment tracker — TypeScript, NestJS, multi-module",
    },
    # Large / Complex
    "jan": {
        "url": "https://github.com/janhq/jan",
        "loc": "~89k",
        "size": "large",
        "complexity": "complex",
        "description": "AI desktop app — Electron, TypeScript, complex arch",
    },
}

# ── Helpers ──────────────────────────────────────────────────────────


def _skip_unless_openrouter():
    """Skip if OPENROUTER_API_KEY is not set."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("Requires OPENROUTER_API_KEY env var")


def _clone_or_reuse(repo_key: str) -> Path:
    """Clone a repo from GitHub (shallow) or reuse existing workspace.

    Returns path to repo with an initialized git repo.
    """
    repo_info = REPOS[repo_key]
    url = repo_info["url"]
    workspace = WORKSPACES_DIR / repo_key

    if workspace.is_dir() and (workspace / ".git").is_dir():
        # Reuse existing clone — reset to clean state
        subprocess.run(
            ["git", "checkout", "."],
            cwd=workspace, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=workspace, capture_output=True, timeout=30,
        )
        return workspace

    # Fresh shallow clone
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=1", url, str(workspace)],
        check=True, capture_output=True, timeout=300,
    )

    # Configure git user for FORGE branches/commits
    subprocess.run(
        ["git", "config", "user.email", "forge-test@test.local"],
        cwd=workspace, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "FORGE Test"],
        cwd=workspace, check=True, capture_output=True,
    )

    return workspace


def _make_discovery_config(overrides: dict | None = None) -> dict:
    """Build a discovery-only config for matrix tests."""
    cfg: dict = {
        "runtime": "open_code",
        "mode": "discovery",
        "dry_run": True,
        "enable_learning": False,
        "enable_github_pr": False,
        "enable_parallel_audit": True,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


async def _run_discover(repo_path: str, config: dict) -> ForgeResult:
    """Run FORGE discovery via StandaloneDispatcher."""
    from forge.standalone import run_standalone

    return await run_standalone(repo_path=repo_path, config=config)


def _assert_discovery_basics(result: ForgeResult, repo_key: str) -> None:
    """Common structural assertions for all discovery tests."""
    assert result.forge_run_id, f"[{repo_key}] Every run must have an ID"
    assert result.success is True, (
        f"[{repo_key}] Discovery should succeed: {result.summary}"
    )
    assert result.mode == ForgeMode.DISCOVERY, (
        f"[{repo_key}] Mode should be DISCOVERY, got {result.mode}"
    )
    assert result.total_findings > 0, (
        f"[{repo_key}] Should find at least 1 issue"
    )
    assert result.findings_fixed == 0, (
        f"[{repo_key}] Discovery mode should not fix anything"
    )
    assert result.agent_invocations >= 4, (
        f"[{repo_key}] Expected >= 4 agent calls (analyst + 3 auditors), "
        f"got {result.agent_invocations}"
    )
    assert result.duration_seconds > 0, (
        f"[{repo_key}] Duration must be positive"
    )
    assert result.cost_usd >= 0.0, (
        f"[{repo_key}] Cost must be non-negative"
    )


def _assert_report_generated(repo_key: str) -> None:
    """Verify that discovery report files were generated."""
    workspace = WORKSPACES_DIR / repo_key
    report_dir = workspace / ".artifacts" / "report"

    json_path = report_dir / "discovery_report.json"
    html_path = report_dir / "discovery_report.html"

    assert json_path.is_file(), (
        f"[{repo_key}] Discovery JSON report not found at {json_path}"
    )
    assert html_path.is_file(), (
        f"[{repo_key}] Discovery HTML report not found at {html_path}"
    )

    # Validate JSON report structure
    import json
    with open(json_path) as f:
        data = json.load(f)

    assert data["phase"] == "discovery", f"[{repo_key}] Report phase mismatch"
    assert data["total_findings"] > 0, f"[{repo_key}] Report has no findings"
    assert len(data["findings"]) == data["total_findings"], (
        f"[{repo_key}] Findings count mismatch: "
        f"{len(data['findings'])} vs {data['total_findings']}"
    )
    assert data["severity_breakdown"], f"[{repo_key}] No severity breakdown"

    # Validate each finding has required fields
    for finding in data["findings"]:
        assert finding.get("title"), f"[{repo_key}] Finding missing title"
        assert finding.get("severity"), f"[{repo_key}] Finding missing severity"
        assert finding.get("category"), f"[{repo_key}] Finding missing category"
        assert finding.get("description"), f"[{repo_key}] Finding missing description"

    # HTML should be non-trivial
    html_size = html_path.stat().st_size
    assert html_size > 1000, (
        f"[{repo_key}] HTML report too small ({html_size} bytes)"
    )

    print(f"  Reports:")
    print(f"    JSON: {json_path} ({json_path.stat().st_size:,} bytes)")
    print(f"    HTML: {html_path} ({html_size:,} bytes)")

    return data


def _print_result_summary(result: ForgeResult, repo_key: str) -> None:
    """Print a human-readable summary of the discovery results."""
    repo_info = REPOS[repo_key]
    print(f"\n{'=' * 70}")
    print(f"  MATRIX RESULT: {repo_key}")
    print(f"  Size: {repo_info['size']} | Complexity: {repo_info['complexity']}")
    print(f"  LOC: {repo_info['loc']} | {repo_info['description']}")
    print(f"{'=' * 70}")
    print(f"  Run ID:          {result.forge_run_id}")
    print(f"  Success:         {result.success}")
    print(f"  Total Findings:  {result.total_findings}")
    print(f"  Deferred:        {result.findings_deferred}")
    print(f"  Agent Calls:     {result.agent_invocations}")
    print(f"  Duration:        {result.duration_seconds:.1f}s")
    print(f"  Cost:            ${result.cost_usd:.4f}")
    if result.readiness_report:
        print(f"  Readiness Score: {result.readiness_report.overall_score}/100")
        print(f"  Summary:         {result.readiness_report.summary[:200]}")
    print(f"  Pipeline Summary: {result.summary[:300]}")
    print(f"{'=' * 70}")


# ── SMALL TESTS (5-10k LOC) ─────────────────────────────────────────


class TestSmallSimple:
    """Small + Simple: fabric (5.4k LOC) — Python AI scripts."""

    @pytest.mark.live
    @pytest.mark.timeout(SMALL_TIMEOUT)
    async def test_discover_fabric(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("fabric")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "fabric")
        _assert_discovery_basics(result, "fabric")
        _assert_report_generated("fabric")


class TestSmallDecent:
    """Small + Decent: open-saas (10.9k LOC) — Wasp SaaS template."""

    @pytest.mark.live
    @pytest.mark.timeout(SMALL_TIMEOUT)
    async def test_discover_open_saas(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("open-saas")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "open-saas")
        _assert_discovery_basics(result, "open-saas")
        _assert_report_generated("open-saas")


class TestSmallComplex:
    """Small + Complex: fastapi-fullstack (12.6k LOC) — Full-stack template."""

    @pytest.mark.live
    @pytest.mark.timeout(SMALL_TIMEOUT)
    async def test_discover_fastapi_fullstack(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("fastapi-fullstack")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "fastapi-fullstack")
        _assert_discovery_basics(result, "fastapi-fullstack")
        _assert_report_generated("fastapi-fullstack")


# ── MEDIUM TESTS (15-30k LOC) ───────────────────────────────────────


class TestMediumSimple:
    """Medium + Simple: httpie (19k LOC) — Python HTTP client."""

    @pytest.mark.live
    @pytest.mark.timeout(MEDIUM_TIMEOUT)
    async def test_discover_httpie(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("httpie")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "httpie")
        _assert_discovery_basics(result, "httpie")
        _assert_report_generated("httpie")


class TestMediumDecent:
    """Medium + Decent: screenshot-to-code (19.2k LOC) — AI vibecoded app."""

    @pytest.mark.live
    @pytest.mark.timeout(MEDIUM_TIMEOUT)
    async def test_discover_screenshot_to_code(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("screenshot-to-code")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "screenshot-to-code")
        _assert_discovery_basics(result, "screenshot-to-code")
        _assert_report_generated("screenshot-to-code")


class TestMediumComplex:
    """Medium + Complex: chatbot-ui (26.9k LOC) — Multi-provider AI chat."""

    @pytest.mark.live
    @pytest.mark.timeout(MEDIUM_TIMEOUT)
    async def test_discover_chatbot_ui(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("chatbot-ui")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "chatbot-ui")
        _assert_discovery_basics(result, "chatbot-ui")
        _assert_report_generated("chatbot-ui")


# ── LARGE TESTS (50k+ LOC) ──────────────────────────────────────────


class TestLargeSimple:
    """Large + Simple: juice-shop (90k LOC) — OWASP intentionally insecure."""

    @pytest.mark.live
    @pytest.mark.timeout(LARGE_TIMEOUT)
    async def test_discover_juice_shop(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("juice-shop")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "juice-shop")
        _assert_discovery_basics(result, "juice-shop")
        _assert_report_generated("juice-shop")
        # Juice Shop is intentionally insecure — should find many issues
        assert result.total_findings >= 5, (
            f"[juice-shop] OWASP Juice Shop should have many findings, "
            f"got {result.total_findings}"
        )


class TestLargeDecent:
    """Large + Decent: ghostfolio (71k LOC) — Investment tracker, TS."""

    @pytest.mark.live
    @pytest.mark.timeout(LARGE_TIMEOUT)
    async def test_discover_ghostfolio(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("ghostfolio")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "ghostfolio")
        _assert_discovery_basics(result, "ghostfolio")
        _assert_report_generated("ghostfolio")


class TestLargeComplex:
    """Large + Complex: jan (89k LOC) — AI desktop app, Electron+TS."""

    @pytest.mark.live
    @pytest.mark.timeout(LARGE_TIMEOUT)
    async def test_discover_jan(self):
        _skip_unless_openrouter()
        repo = _clone_or_reuse("jan")
        config = _make_discovery_config()

        result = await _run_discover(str(repo), config)

        _print_result_summary(result, "jan")
        _assert_discovery_basics(result, "jan")
        _assert_report_generated("jan")
