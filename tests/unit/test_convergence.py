"""Unit tests for forge.execution.convergence."""

import pytest

from forge.schemas import (
    AuditFinding,
    CategoryScore,
    CoderFixResult,
    ConvergenceIterationRecord,
    FindingCategory,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    IntegrationValidationResult,
    OuterLoopState,
    ProductionReadinessReport,
    RemediationPlan,
    RemediationItem,
    RemediationTier,
)
from forge.execution.convergence import (
    merge_findings,
    _dedup_key,
    _build_convergence_context,
)


class TestMergeFindings:
    """Tests for merge_findings()."""

    def _make_finding(self, id="F-test", title="Test", category="quality", severity="medium", **kw):
        return AuditFinding(
            id=id, title=title, description="desc",
            category=FindingCategory(category),
            severity=FindingSeverity(severity),
            **kw,
        )

    def _make_fix(self, finding_id, outcome=FixOutcome.COMPLETED):
        return CoderFixResult(
            finding_id=finding_id, outcome=outcome,
            files_changed=["test.py"], summary="Fixed",
        )

    def test_removes_fixed_findings(self):
        f1 = self._make_finding(id="F-1", title="Finding 1")
        f2 = self._make_finding(id="F-2", title="Finding 2")
        fix = self._make_fix("F-1")

        merged = merge_findings(
            existing=[f1, f2],
            new_findings=[],
            completed_fixes=[fix],
            deferred_ids=set(),
        )

        ids = {f.id for f in merged}
        assert "F-1" not in ids
        assert "F-2" in ids

    def test_adds_new_findings(self):
        f1 = self._make_finding(id="F-1", title="Existing")
        f_new = self._make_finding(id="F-new", title="New issue")

        merged = merge_findings(
            existing=[f1],
            new_findings=[f_new],
            completed_fixes=[],
            deferred_ids=set(),
        )

        assert len(merged) == 2
        ids = {f.id for f in merged}
        assert "F-new" in ids

    def test_deduplicates_by_title_and_location(self):
        f1 = self._make_finding(id="F-1", title="Same issue")
        f2 = self._make_finding(id="F-2", title="Same issue")

        merged = merge_findings(
            existing=[f1],
            new_findings=[f2],
            completed_fixes=[],
            deferred_ids=set(),
        )

        # Should deduplicate — same title + no location
        assert len(merged) == 1

    def test_reinjects_deferred_as_must_fix(self):
        f1 = self._make_finding(id="F-1", title="Deferred issue")

        merged = merge_findings(
            existing=[f1],
            new_findings=[],
            completed_fixes=[],
            deferred_ids={"F-1"},
            escalate_dropped=True,
        )

        assert len(merged) == 1
        assert merged[0].actionability == "must_fix"

    def test_converts_introduced_issues(self):
        f1 = self._make_finding(id="F-1", title="Existing")
        integration = IntegrationValidationResult(
            passed=False,
            new_issues_introduced=["New SQL injection in user.py"],
        )

        merged = merge_findings(
            existing=[f1],
            new_findings=[],
            completed_fixes=[],
            deferred_ids=set(),
            integration_result=integration,
        )

        assert len(merged) == 2
        new_issue = [f for f in merged if "SQL injection" in f.title]
        assert len(new_issue) == 1
        assert new_issue[0].actionability == "must_fix"

    def test_reinjects_dropped_findings(self):
        f1 = self._make_finding(id="F-1", title="Planned")
        f2 = self._make_finding(id="F-2", title="Dropped by strategist")

        plan = RemediationPlan(
            items=[RemediationItem(finding_id="F-1", title="Planned", tier=RemediationTier.TIER_2, priority=1)],
            execution_levels=[["F-1"]],
        )

        merged = merge_findings(
            existing=[f1, f2],
            new_findings=[],
            completed_fixes=[],
            deferred_ids=set(),
            escalate_dropped=True,
            prior_plan=plan,
        )

        dropped = [f for f in merged if f.id == "F-2"]
        assert len(dropped) == 1
        assert dropped[0].actionability == "must_fix"

    def test_no_escalation_when_disabled(self):
        f1 = self._make_finding(id="F-1", title="Deferred")

        merged = merge_findings(
            existing=[f1],
            new_findings=[],
            completed_fixes=[],
            deferred_ids={"F-1"},
            escalate_dropped=False,
        )

        assert merged[0].actionability != "must_fix"


class TestDedupKey:
    def test_uses_dedup_key_if_set(self):
        f = AuditFinding(
            id="F-1", title="Test", description="d",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            dedup_key="custom-key",
        )
        assert _dedup_key(f) == "custom-key"

    def test_falls_back_to_title_location(self):
        from forge.schemas import FindingLocation
        f = AuditFinding(
            id="F-1", title="SQL Injection", description="d",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[FindingLocation(file_path="db.py")],
        )
        assert _dedup_key(f) == "SQL Injection|db.py"


class TestBuildConvergenceContext:
    def test_includes_score(self):
        state = ForgeExecutionState()
        state.readiness_report = ProductionReadinessReport(overall_score=68)

        ctx = _build_convergence_context(state, target_score=95)
        assert "68/100" in ctx
        assert "target: 95" in ctx

    def test_includes_low_categories(self):
        state = ForgeExecutionState()
        state.readiness_report = ProductionReadinessReport(
            overall_score=68,
            category_scores=[
                CategoryScore(name="Security", score=62, weight=0.3),
                CategoryScore(name="Test Coverage", score=35, weight=0.15),
                CategoryScore(name="Performance", score=85, weight=0.1),
            ],
        )

        ctx = _build_convergence_context(state, target_score=95)
        assert "Security (62)" in ctx
        assert "Test Coverage (35)" in ctx
        assert "Performance" not in ctx  # Score 85 > 70 threshold

    def test_includes_introduced_issues(self):
        state = ForgeExecutionState()
        state.readiness_report = ProductionReadinessReport(overall_score=68)
        state.integration_result = IntegrationValidationResult(
            passed=False,
            new_issues_introduced=["New vulnerability in auth.py"],
        )

        ctx = _build_convergence_context(state, target_score=95)
        assert "New vulnerability in auth.py" in ctx
        assert "MUST-FIX" in ctx

    def test_includes_deferred_findings(self):
        state = ForgeExecutionState()
        state.readiness_report = ProductionReadinessReport(overall_score=68)
        finding = AuditFinding(
            id="F-1", title="Missing auth",
            description="d",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.CRITICAL,
        )
        state.all_findings = [finding]
        state.outer_loop.deferred_findings = ["F-1"]

        ctx = _build_convergence_context(state, target_score=95)
        assert "Missing auth" in ctx
        assert "critical" in ctx

    def test_handles_empty_state(self):
        state = ForgeExecutionState()
        ctx = _build_convergence_context(state, target_score=95)
        assert "0/100" in ctx
