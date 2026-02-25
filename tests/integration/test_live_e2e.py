"""Live E2E integration tests for FORGE engine.

These tests run the actual FORGE pipeline against real golden codebases,
using a live AgentField server and real LLM APIs via OpenRouter.

Requires:
  - AgentField server running (AGENTFIELD_SERVER env var)
  - OpenRouter API key (OPENROUTER_API_KEY env var)
  - pytest --run-live flag or FORGE_LIVE_TESTS=1

Usage:
  pytest tests/integration/test_live_e2e.py --run-live -v
  FORGE_LIVE_TESTS=1 pytest tests/integration/test_live_e2e.py -v

Each test:
  1. Copies a golden codebase to a temporary directory
  2. Initializes a git repo in the copy (so FORGE can branch/commit)
  3. Calls the FORGE app reasoner
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

# Timeout constants (seconds)
DISCOVERY_TIMEOUT = 300   # 5 minutes for discovery (Agents 1-5)
SCAN_TIMEOUT = 300        # 5 minutes for scan (alias for discover)
FIX_SINGLE_TIMEOUT = 180  # 3 minutes for a single fix
REMEDIATE_TIMEOUT = 900   # 15 minutes for full remediation (all 12 agents)


# ── Helpers ──────────────────────────────────────────────────────────


def _copy_golden_codebase(name: str, dest: Path) -> Path:
    """Copy a golden codebase into dest and initialize a git repo.

    Returns the path to the initialized repo.
    """
    src = GOLDEN_DIR / name
    if not src.is_dir():
        pytest.fail(f"Golden codebase not found: {src}")

    repo = dest / name
    shutil.copytree(src, repo)

    # Initialize a git repo so FORGE can create branches and commits
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


def _parse_forge_result(raw: dict) -> ForgeResult:
    """Parse a raw dict into a ForgeResult, failing the test if invalid."""
    try:
        return ForgeResult(**raw)
    except Exception as exc:
        pytest.fail(f"Failed to parse ForgeResult: {exc}\nRaw: {raw}")


def _make_live_config(overrides: dict | None = None) -> dict:
    """Build a minimal config dict for live tests.

    Uses cheap models to keep costs low while still exercising the pipeline.
    """
    cfg: dict = {
        "runtime": "open_code",
        "enable_learning": False,  # no telemetry during tests
        "enable_github_pr": False,  # no PRs during tests
    }
    if overrides:
        cfg.update(overrides)
    return cfg


# ── Discovery Tests ──────────────────────────────────────────────────


@pytest.mark.live
class TestLiveDiscovery:
    """Test the discovery pipeline against real flawed codebases.

    Discovery runs Agents 1-4 (Codebase Analyst, Security Auditor,
    Quality Auditor, Architecture Reviewer) plus Agent 5-6 (Triage).
    It produces findings but applies no fixes.
    """

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_express_api(self, skip_unless_live, tmp_path):
        """Run discovery on the express_api_nosec golden codebase.

        The express_api_nosec codebase contains:
          - Hardcoded secrets in config.js (API keys, DB password, JWT secret)
          - SQL injection in users.js (string concatenation in query)
          - Stack trace exposure in error handler
          - No rate limiting, CORS, or security headers
          - .env file with live credentials

        We expect the discovery pipeline to find security issues.
        """
        from forge.app import discover

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)
        config = _make_live_config({"mode": "discovery", "dry_run": True})

        raw = await discover(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        # Structural assertions -- not testing exact LLM output
        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0, (
            "express_api_nosec has obvious vulnerabilities; expected at least 1 finding"
        )
        assert result.findings_fixed == 0, "Discovery mode should not fix anything"
        assert result.agent_invocations >= 4, (
            "Discovery runs at least 4 agents (codebase analyst + 3 auditors)"
        )
        assert result.duration_seconds > 0
        assert result.forge_run_id, "Every run must have an ID"

        # Readiness report is NOT produced in discovery-only mode
        # (it requires the validation phase, agents 11-12)
        # The remediation plan may or may not exist depending on triage

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_flask_secrets(self, skip_unless_live, tmp_path):
        """Run discovery on flask_exposed_secrets golden codebase.

        The flask_exposed_secrets codebase contains:
          - Hardcoded SECRET_KEY = "supersecret123"
          - debug=True in production (exposes Werkzeug debugger)
          - No session cookie security flags
          - .env with DATABASE_URL, AWS keys, SendGrid API key
          - No input validation on login endpoint

        We expect the discovery pipeline to flag at least the hardcoded
        secret and debug mode.
        """
        from forge.app import discover

        repo = _copy_golden_codebase("flask_exposed_secrets", tmp_path)
        config = _make_live_config({"mode": "discovery", "dry_run": True})

        raw = await discover(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0, (
            "flask_exposed_secrets has hardcoded secrets and debug=True; "
            "expected at least 1 finding"
        )
        assert result.findings_fixed == 0, "Discovery mode should not fix anything"

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_discover_fastapi_monolith(self, skip_unless_live, tmp_path):
        """Run discovery on fastapi_monolith golden codebase.

        Tests that the pipeline handles a Python FastAPI project.
        """
        from forge.app import discover

        repo = _copy_golden_codebase("fastapi_monolith", tmp_path)
        config = _make_live_config({"mode": "discovery", "dry_run": True})

        raw = await discover(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True, f"Discovery should succeed: {result.summary}"
        assert result.total_findings >= 0  # may find issues, may not -- structural check
        assert result.findings_fixed == 0


# ── Scan Tests ───────────────────────────────────────────────────────


@pytest.mark.live
class TestLiveScan:
    """Test the scan endpoint (alias for discover).

    Scan is the free-tier entry point: produces a readiness assessment
    without applying any fixes.
    """

    @pytest.mark.timeout(SCAN_TIMEOUT)
    async def test_scan_returns_findings(self, skip_unless_live, tmp_path):
        """Scan should return findings without applying fixes.

        Uses the express_api_nosec codebase which has guaranteed security flaws.
        """
        from forge.app import scan

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)
        config = _make_live_config()

        raw = await scan(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True, f"Scan should succeed: {result.summary}"
        # scan delegates to discover, which sets mode = discovery
        assert result.mode == ForgeMode.DISCOVERY
        assert result.total_findings > 0, "Scan should detect findings"
        assert result.findings_fixed == 0, "Scan must not apply fixes"
        assert result.forge_run_id
        assert result.duration_seconds > 0

    @pytest.mark.timeout(SCAN_TIMEOUT)
    async def test_scan_react_app(self, skip_unless_live, tmp_path):
        """Scan a React/TypeScript codebase (react_app_noerror).

        This codebase has quality issues (no error boundaries, no loading
        states) but may not have critical security issues. We assert the
        pipeline runs to completion regardless.
        """
        from forge.app import scan

        repo = _copy_golden_codebase("react_app_noerror", tmp_path)
        config = _make_live_config()

        raw = await scan(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True, f"Scan should succeed: {result.summary}"
        assert result.findings_fixed == 0, "Scan must not apply fixes"


# ── Fix Single Tests ─────────────────────────────────────────────────


@pytest.mark.live
class TestLiveFixSingle:
    """Test fix_single against individual findings.

    fix_single takes a pre-constructed AuditFinding dict and runs it
    through triage -> coder -> reviewer for a single targeted fix.
    """

    @pytest.mark.timeout(FIX_SINGLE_TIMEOUT)
    async def test_fix_hardcoded_secret(self, skip_unless_live, tmp_path):
        """Fix a single hardcoded secret finding.

        Creates a minimal file with a hardcoded API key, constructs a
        finding dict pointing at it, and calls fix_single. The coder
        agent should replace the hardcoded value with an env var lookup.
        """
        from forge.app import fix_single

        # Set up a minimal repo with a hardcoded secret
        repo = tmp_path / "secret-repo"
        repo.mkdir()
        (repo / "config.py").write_text(
            'API_KEY = "sk-live-FAKEKEYFORTEST1234567890"\n'
            'DB_HOST = "localhost"\n'
        )

        # Initialize git
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

        finding = {
            "id": "F-livesec01",
            "title": "Hardcoded API key in config.py",
            "description": (
                "The file config.py contains a hardcoded API key "
                "'sk-live-FAKEKEYFORTEST1234567890'. This should be "
                "loaded from an environment variable instead."
            ),
            "category": "security",
            "severity": "high",
            "locations": [
                {
                    "file_path": "config.py",
                    "line_start": 1,
                    "line_end": 1,
                    "snippet": 'API_KEY = "sk-live-FAKEKEYFORTEST1234567890"',
                }
            ],
            "suggested_fix": "Replace hardcoded key with os.environ.get('API_KEY')",
            "agent": "security_auditor",
        }

        config = _make_live_config()

        raw = await fix_single(
            repo_path=str(repo),
            finding=finding,
            config=config,
        )

        # fix_single returns a dict, not a ForgeResult
        assert isinstance(raw, dict), f"Expected dict, got {type(raw)}"
        assert raw.get("finding_id") == "F-livesec01"

        # The fix may or may not succeed depending on the LLM, but the
        # pipeline should complete without crashing
        assert "outcome" in raw, f"Response missing 'outcome': {raw}"
        assert raw["outcome"] in (
            "completed", "completed_with_debt", "skipped",
            "failed_retryable", "failed_escalated", "deferred",
        ), f"Unexpected outcome: {raw['outcome']}"

        # If the fix succeeded, verify the file was actually changed
        if raw.get("success"):
            config_content = (repo / "config.py").read_text()
            # The hardcoded key should no longer be present verbatim
            assert "sk-live-FAKEKEYFORTEST1234567890" not in config_content, (
                "Hardcoded secret should have been removed after successful fix"
            )

    @pytest.mark.timeout(FIX_SINGLE_TIMEOUT)
    async def test_fix_sql_injection(self, skip_unless_live, tmp_path):
        """Fix a SQL injection finding in a Node.js file.

        Creates a minimal file with string-concatenated SQL, then asks
        FORGE to fix it via parameterized queries.
        """
        from forge.app import fix_single

        repo = tmp_path / "sqli-repo"
        repo.mkdir()
        (repo / "query.js").write_text(
            'const pool = require("pg").Pool();\n'
            "\n"
            "async function searchUser(name) {\n"
            "  const result = await pool.query(\n"
            "    `SELECT * FROM users WHERE name = '${name}'`\n"
            "  );\n"
            "  return result.rows;\n"
            "}\n"
            "\n"
            "module.exports = { searchUser };\n"
        )
        (repo / "package.json").write_text('{"name": "sqli-test", "version": "1.0.0"}\n')

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

        finding = {
            "id": "F-livesql01",
            "title": "SQL injection via string interpolation",
            "description": (
                "The function searchUser in query.js uses template literal "
                "string interpolation to build a SQL query with user input. "
                "This allows SQL injection attacks. Use parameterized queries."
            ),
            "category": "security",
            "severity": "critical",
            "locations": [
                {
                    "file_path": "query.js",
                    "line_start": 4,
                    "line_end": 6,
                    "snippet": "`SELECT * FROM users WHERE name = '${name}'`",
                }
            ],
            "suggested_fix": "Use parameterized query: pool.query('SELECT * FROM users WHERE name = $1', [name])",
            "agent": "security_auditor",
        }

        config = _make_live_config()

        raw = await fix_single(
            repo_path=str(repo),
            finding=finding,
            config=config,
        )

        assert isinstance(raw, dict)
        assert raw.get("finding_id") == "F-livesql01"
        assert "outcome" in raw

        # If successful, the string interpolation should be gone
        if raw.get("success"):
            query_content = (repo / "query.js").read_text()
            assert "${name}" not in query_content, (
                "String interpolation SQL injection should be fixed"
            )


# ── Full Remediation Tests ───────────────────────────────────────────


@pytest.mark.live
class TestLiveRemediate:
    """Test the full remediation pipeline (all 12 agents).

    This is the most expensive test class -- it runs discovery, triage,
    remediation (with coder/reviewer control loops), and validation.
    """

    @pytest.mark.timeout(REMEDIATE_TIMEOUT)
    async def test_full_pipeline_express_api(self, skip_unless_live, tmp_path):
        """Run full remediation on express_api_nosec.

        This is the comprehensive test -- exercises all 12 agents:
          - Agents 1-4: Discovery (codebase analyst, security/quality/arch auditors)
          - Agents 5-6: Triage (fix strategist, triage classifier)
          - Agents 7-10: Remediation (coder, test generator, code reviewer)
          - Agents 11-12: Validation (integration validator, debt tracker)

        We assert on the structural shape of the result, not exact fixes.
        """
        from forge.app import remediate

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)
        config = _make_live_config({
            "mode": "full",
            "dry_run": False,
            "max_inner_retries": 2,     # keep costs reasonable
            "max_middle_escalations": 1,
            "max_outer_replans": 0,     # no replanning in tests
        })

        raw = await remediate(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        # The pipeline should complete (success=True means no unhandled exceptions)
        assert result.success is True, f"Full pipeline failed: {result.summary}"
        assert result.mode == ForgeMode.FULL
        assert result.forge_run_id

        # Discovery should have found issues
        assert result.total_findings > 0, (
            "express_api_nosec has multiple vulnerabilities"
        )

        # At least some agent invocations occurred
        assert result.agent_invocations >= 4, (
            "Full pipeline should invoke at least discovery agents"
        )

        # Duration should be non-trivial for a real pipeline
        assert result.duration_seconds > 1.0

        # Either some findings were fixed or some were deferred (or both)
        # The LLM may not fix everything, but the pipeline should have tried
        total_handled = result.findings_fixed + result.findings_deferred
        assert total_handled >= 0  # structural check -- pipeline ran through

        # If fixes were applied, verify the artifacts directory was created
        artifacts_dir = repo / ".artifacts"
        if result.findings_fixed > 0:
            assert artifacts_dir.is_dir(), (
                "Artifacts directory should exist after fixes"
            )

        # If readiness report was generated, validate its structure
        if result.readiness_report is not None:
            report = result.readiness_report
            assert 0 <= report.overall_score <= 100
            assert report.findings_total >= 0
            assert isinstance(report.summary, str)

    @pytest.mark.timeout(REMEDIATE_TIMEOUT)
    async def test_full_pipeline_flask_secrets(self, skip_unless_live, tmp_path):
        """Run full remediation on flask_exposed_secrets.

        A smaller codebase (single app.py file) so this should be faster
        than express_api_nosec. Tests the pipeline against Python code.
        """
        from forge.app import remediate

        repo = _copy_golden_codebase("flask_exposed_secrets", tmp_path)
        config = _make_live_config({
            "mode": "full",
            "dry_run": False,
            "max_inner_retries": 2,
            "max_middle_escalations": 1,
            "max_outer_replans": 0,
        })

        raw = await remediate(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True, f"Full pipeline failed: {result.summary}"
        assert result.total_findings > 0, (
            "flask_exposed_secrets has hardcoded SECRET_KEY and debug=True"
        )
        assert result.forge_run_id
        assert result.duration_seconds > 0

    @pytest.mark.timeout(REMEDIATE_TIMEOUT)
    async def test_dry_run_does_not_modify_files(self, skip_unless_live, tmp_path):
        """Dry-run mode should discover findings but never modify code.

        Verifies that with dry_run=True, no files in the repo are changed
        even when findings are present.
        """
        from forge.app import remediate

        repo = _copy_golden_codebase("express_api_nosec", tmp_path)

        # Capture file contents before the run
        original_files = {}
        for fpath in repo.rglob("*"):
            if fpath.is_file() and ".git" not in fpath.parts:
                original_files[fpath] = fpath.read_bytes()

        config = _make_live_config({
            "mode": "full",
            "dry_run": True,
        })

        raw = await remediate(
            repo_path=str(repo),
            config=config,
        )

        result = _parse_forge_result(raw)

        assert result.success is True
        assert result.findings_fixed == 0, "dry_run should produce zero fixes"

        # Verify no source files were modified
        for fpath, original_content in original_files.items():
            current = fpath.read_bytes()
            assert current == original_content, (
                f"dry_run modified {fpath.relative_to(repo)} but should not have"
            )


# ── Pipeline Invariant Tests ─────────────────────────────────────────


@pytest.mark.live
class TestLivePipelineInvariants:
    """Tests for structural invariants that must hold across all runs."""

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_forge_run_id_is_unique(self, skip_unless_live, tmp_path):
        """Two consecutive runs must produce different forge_run_ids."""
        from forge.app import discover

        repo1 = _copy_golden_codebase("express_api_nosec", tmp_path / "run1")
        repo2 = _copy_golden_codebase("express_api_nosec", tmp_path / "run2")
        config = _make_live_config({"mode": "discovery", "dry_run": True})

        raw1 = await discover(repo_path=str(repo1), config=config)
        raw2 = await discover(repo_path=str(repo2), config=config)

        result1 = _parse_forge_result(raw1)
        result2 = _parse_forge_result(raw2)

        assert result1.forge_run_id != result2.forge_run_id, (
            "Each FORGE run must have a unique ID"
        )

    @pytest.mark.timeout(DISCOVERY_TIMEOUT)
    async def test_empty_repo_does_not_crash(self, skip_unless_live, tmp_path):
        """An empty repo should produce zero findings, not crash."""
        from forge.app import discover

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

        config = _make_live_config({"mode": "discovery", "dry_run": True})

        raw = await discover(repo_path=str(repo), config=config)

        result = _parse_forge_result(raw)

        # The pipeline should complete without crashing
        assert result.success is True, (
            f"Empty repo should not crash the pipeline: {result.summary}"
        )
        assert result.forge_run_id
