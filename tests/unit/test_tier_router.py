"""Tests for FORGE tier-based dispatch router."""

import os

import pytest

from forge.execution.tier_router import (
    apply_tier0,
    apply_tier1,
    route_plan_items,
    _detect_framework,
    _tier1_replace_secret,
    _tier1_create_env_example,
)
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
)
from forge.config import ForgeConfig


class TestApplyTier0:
    def test_produces_skipped_result(self):
        finding = AuditFinding(
            id="F-001",
            title="False positive",
            description="Not a real issue",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.LOW,
        )
        item = RemediationItem(
            finding_id="F-001", title="Skip",
            tier=RemediationTier.TIER_0, priority=1,
        )
        result = apply_tier0(finding, item)
        assert result.outcome == FixOutcome.SKIPPED
        assert result.finding_id == "F-001"

    def test_summary_contains_title(self):
        finding = AuditFinding(
            id="F-002",
            title="Duplicate finding",
            description="Already reported",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.INFO,
        )
        item = RemediationItem(
            finding_id="F-002", title="Skip",
            tier=RemediationTier.TIER_0, priority=1,
        )
        result = apply_tier0(finding, item)
        assert "Duplicate finding" in result.summary

    def test_returns_coder_fix_result(self):
        finding = AuditFinding(
            id="F-003",
            title="Test",
            description="Desc",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.LOW,
        )
        item = RemediationItem(
            finding_id="F-003", title="Skip",
            tier=RemediationTier.TIER_0, priority=1,
        )
        result = apply_tier0(finding, item)
        assert isinstance(result, CoderFixResult)


class TestTier1ReplaceSecret:
    def test_replaces_hardcoded_secret(self, tmp_path):
        # Create a file with a hardcoded secret
        src = tmp_path / "src"
        src.mkdir()
        config_file = src / "config.js"
        config_file.write_text(
            'const API_KEY = "sk-12345abcdef90";\nconst other = 42;\n'
        )

        finding = AuditFinding(
            id="F-002",
            title="Hardcoded API key",
            description="API key is hardcoded",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[FindingLocation(file_path="src/config.js")],
        )
        result = _tier1_replace_secret(finding, str(tmp_path))
        assert result.outcome == FixOutcome.COMPLETED
        assert "src/config.js" in result.files_changed

        content = config_file.read_text()
        assert "os.environ" in content
        assert "sk-12345" not in content

    def test_no_match_returns_failed(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "clean.js").write_text("const x = 42;\n")

        finding = AuditFinding(
            id="F-003",
            title="No secret",
            description="Clean file",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.LOW,
            locations=[FindingLocation(file_path="src/clean.js")],
        )
        result = _tier1_replace_secret(finding, str(tmp_path))
        assert result.outcome == FixOutcome.FAILED_RETRYABLE

    def test_nonexistent_file(self, tmp_path):
        finding = AuditFinding(
            id="F-004",
            title="Missing file",
            description="File does not exist",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[FindingLocation(file_path="src/missing.js")],
        )
        result = _tier1_replace_secret(finding, str(tmp_path))
        assert result.outcome == FixOutcome.FAILED_RETRYABLE

    def test_no_locations(self, tmp_path):
        finding = AuditFinding(
            id="F-005",
            title="No location",
            description="No file reference",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[],
        )
        result = _tier1_replace_secret(finding, str(tmp_path))
        assert result.outcome == FixOutcome.FAILED_RETRYABLE

    def test_adds_import_os(self, tmp_path):
        """If file doesn't have 'import os', it should be added."""
        src = tmp_path / "src"
        src.mkdir()
        config_file = src / "settings.py"
        config_file.write_text('DB_PASSWORD = "super_secret_pw"\n')

        finding = AuditFinding(
            id="F-006",
            title="Hardcoded password",
            description="Password in source",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.CRITICAL,
            locations=[FindingLocation(file_path="src/settings.py")],
        )
        result = _tier1_replace_secret(finding, str(tmp_path))
        assert result.outcome == FixOutcome.COMPLETED
        content = config_file.read_text()
        assert "import os" in content
        assert "os.environ" in content


class TestTier1CreateEnvExample:
    def test_creates_env_example(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DB_URL=postgres://localhost/db\nAPI_KEY=secret123\n")

        finding = AuditFinding(
            id="F-004",
            title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
            suggested_fix="Create .env.example",
        )
        result = _tier1_create_env_example(finding, str(tmp_path))
        assert result.outcome == FixOutcome.COMPLETED

        example = (tmp_path / ".env.example").read_text()
        assert "DB_URL=" in example
        assert "API_KEY=" in example
        assert "secret123" not in example

    def test_already_exists_returns_skipped(self, tmp_path):
        (tmp_path / ".env").write_text("KEY=val\n")
        (tmp_path / ".env.example").write_text("KEY=\n")

        finding = AuditFinding(
            id="F-005",
            title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
        )
        result = _tier1_create_env_example(finding, str(tmp_path))
        assert result.outcome == FixOutcome.SKIPPED

    def test_no_env_file_returns_failed(self, tmp_path):
        finding = AuditFinding(
            id="F-006",
            title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
        )
        result = _tier1_create_env_example(finding, str(tmp_path))
        assert result.outcome == FixOutcome.FAILED_RETRYABLE

    def test_preserves_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# Database config\nDB_URL=postgres://localhost\n")

        finding = AuditFinding(
            id="F-007",
            title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
        )
        result = _tier1_create_env_example(finding, str(tmp_path))
        assert result.outcome == FixOutcome.COMPLETED
        example = (tmp_path / ".env.example").read_text()
        assert "# Database config" in example

    def test_env_example_has_header(self, tmp_path):
        (tmp_path / ".env").write_text("KEY=val\n")

        finding = AuditFinding(
            id="F-008",
            title="Missing .env.example",
            description="No .env.example",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
        )
        _tier1_create_env_example(finding, str(tmp_path))
        example = (tmp_path / ".env.example").read_text()
        assert "copy to .env" in example.lower() or "Environment variables" in example


class TestDetectFramework:
    def test_detect_express(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"dependencies": {"express": "^4.0"}}'
        )
        assert _detect_framework(str(tmp_path)) == "express"

    def test_detect_fastapi(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi>=0.100\nuvicorn\n")
        assert _detect_framework(str(tmp_path)) == "fastapi"

    def test_detect_flask(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.0\n")
        assert _detect_framework(str(tmp_path)) == "flask"

    def test_unknown_framework(self, tmp_path):
        assert _detect_framework(str(tmp_path)) == "unknown"

    def test_detect_fastify_as_express(self, tmp_path):
        """Fastify is treated similarly to Express."""
        (tmp_path / "package.json").write_text(
            '{"dependencies": {"fastify": "^4.0"}}'
        )
        assert _detect_framework(str(tmp_path)) == "express"

    def test_pyproject_toml_fastapi(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi>=0.100"]\n'
        )
        assert _detect_framework(str(tmp_path)) == "fastapi"


class TestRoutePlanItems:
    def test_splits_tiers(self):
        finding_t0 = AuditFinding(
            id="F-t0",
            title="False positive",
            description="Not real",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.LOW,
        )
        finding_t2 = AuditFinding(
            id="F-t2",
            title="Auth issue",
            description="Add auth",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        item_t0 = RemediationItem(
            finding_id="F-t0", title="Skip",
            tier=RemediationTier.TIER_0, priority=2,
        )
        item_t2 = RemediationItem(
            finding_id="F-t2", title="Fix auth",
            tier=RemediationTier.TIER_2, priority=1,
        )

        plan = RemediationPlan(
            items=[item_t0, item_t2],
            execution_levels=[["F-t0", "F-t2"]],
            total_items=2,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        handled, tier2_items, tier3_items = route_plan_items(
            plan, [finding_t0, finding_t2], state, "/tmp", cfg,
        )
        assert len(handled) == 1  # Tier 0
        assert len(tier2_items) == 1  # Tier 2
        assert len(tier3_items) == 0
        assert handled[0].finding_id == "F-t0"
        assert tier2_items[0].finding_id == "F-t2"

    def test_tier0_appends_to_completed_fixes(self):
        finding = AuditFinding(
            id="F-t0",
            title="False positive",
            description="Not real",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.LOW,
        )
        item = RemediationItem(
            finding_id="F-t0", title="Skip",
            tier=RemediationTier.TIER_0, priority=1,
        )
        plan = RemediationPlan(
            items=[item],
            execution_levels=[["F-t0"]],
            total_items=1,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        route_plan_items(plan, [finding], state, "/tmp", cfg)
        assert len(state.completed_fixes) == 1
        assert state.completed_fixes[0].outcome == FixOutcome.SKIPPED

    def test_tier1_disabled_promotes(self):
        finding = AuditFinding(
            id="F-t1",
            title="Secret",
            description="Hardcoded secret",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        item = RemediationItem(
            finding_id="F-t1", title="Fix",
            tier=RemediationTier.TIER_1, priority=1,
        )
        plan = RemediationPlan(
            items=[item],
            execution_levels=[["F-t1"]],
            total_items=1,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig(enable_tier1_rules=False)

        handled, tier2_items, tier3_items = route_plan_items(
            plan, [finding], state, "/tmp", cfg,
        )
        assert len(handled) == 0
        assert len(tier2_items) == 1
        assert tier2_items[0].tier == RemediationTier.TIER_2

    def test_tier3_goes_to_ai(self):
        finding = AuditFinding(
            id="F-t3",
            title="Architecture refactor",
            description="Restructure modules",
            category=FindingCategory.ARCHITECTURE,
            severity=FindingSeverity.HIGH,
        )
        item = RemediationItem(
            finding_id="F-t3", title="Refactor",
            tier=RemediationTier.TIER_3, priority=1,
        )
        plan = RemediationPlan(
            items=[item],
            execution_levels=[["F-t3"]],
            total_items=1,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        handled, tier2_items, tier3_items = route_plan_items(
            plan, [finding], state, "/tmp", cfg,
        )
        assert len(handled) == 0
        assert len(tier2_items) == 0
        assert len(tier3_items) == 1
        assert tier3_items[0].tier == RemediationTier.TIER_3

    def test_missing_finding_skipped(self):
        """If a plan item references a finding not in the list, it's skipped."""
        item = RemediationItem(
            finding_id="F-missing", title="Fix",
            tier=RemediationTier.TIER_2, priority=1,
        )
        plan = RemediationPlan(
            items=[item],
            execution_levels=[["F-missing"]],
            total_items=1,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        handled, tier2_items, tier3_items = route_plan_items(plan, [], state, "/tmp", cfg)
        assert len(handled) == 0
        assert len(tier2_items) == 0
        assert len(tier3_items) == 0

    def test_multiple_tiers_mixed(self):
        findings = []
        items = []
        for i, tier in enumerate([
            RemediationTier.TIER_0,
            RemediationTier.TIER_2,
            RemediationTier.TIER_3,
            RemediationTier.TIER_2,
        ]):
            fid = f"F-{i:03d}"
            findings.append(AuditFinding(
                id=fid, title=f"Finding {i}", description="Desc",
                category=FindingCategory.SECURITY, severity=FindingSeverity.MEDIUM,
            ))
            items.append(RemediationItem(
                finding_id=fid, title=f"Fix {i}",
                tier=tier, priority=i + 1,
            ))

        plan = RemediationPlan(
            items=items,
            execution_levels=[[f.id for f in findings]],
            total_items=len(items),
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        handled, tier2_items, tier3_items = route_plan_items(plan, findings, state, "/tmp", cfg)
        assert len(handled) == 1  # Only Tier 0
        assert len(tier2_items) == 2  # Two Tier 2
        assert len(tier3_items) == 1  # One Tier 3

    def test_splits_three_ways(self):
        """route_plan_items returns a 3-tuple: handled, tier2, tier3."""
        findings = []
        items = []
        for i, tier in enumerate([
            RemediationTier.TIER_0,
            RemediationTier.TIER_2,
            RemediationTier.TIER_3,
        ]):
            fid = f"F-3way-{i}"
            findings.append(AuditFinding(
                id=fid, title=f"Finding {i}", description="Desc",
                category=FindingCategory.SECURITY, severity=FindingSeverity.MEDIUM,
            ))
            items.append(RemediationItem(
                finding_id=fid, title=f"Fix {i}", tier=tier, priority=i + 1,
            ))

        plan = RemediationPlan(
            items=items,
            execution_levels=[[f.id for f in findings]],
            total_items=len(items),
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        result = route_plan_items(plan, findings, state, "/tmp", cfg)
        assert len(result) == 3  # 3-tuple
        handled, tier2, tier3 = result
        assert len(handled) == 1
        assert len(tier2) == 1
        assert len(tier3) == 1

    def test_tier3_not_in_tier2_list(self):
        """Tier 3 items should only appear in the third return value."""
        finding = AuditFinding(
            id="F-t3-only",
            title="Architecture issue",
            description="Cross-cutting concern",
            category=FindingCategory.ARCHITECTURE,
            severity=FindingSeverity.HIGH,
        )
        item = RemediationItem(
            finding_id="F-t3-only", title="Refactor",
            tier=RemediationTier.TIER_3, priority=1,
        )
        plan = RemediationPlan(
            items=[item],
            execution_levels=[["F-t3-only"]],
            total_items=1,
        )
        state = ForgeExecutionState()
        cfg = ForgeConfig()

        handled, tier2, tier3 = route_plan_items(plan, [finding], state, "/tmp", cfg)
        assert len(handled) == 0
        assert len(tier2) == 0
        assert len(tier3) == 1
        assert tier3[0].finding_id == "F-t3-only"
