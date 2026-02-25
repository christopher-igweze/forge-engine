"""Tests for FORGE schema validation and constraints."""

import pytest
from pydantic import ValidationError

from forge.schemas import (
    AuditFinding,
    ArchitectureReviewResult,
    CategoryScore,
    FindingCategory,
    FindingSeverity,
    FindingLocation,
    ForgeExecutionState,
)


class TestAuditFinding:
    def test_auto_generated_id(self):
        f = AuditFinding(
            title="Test",
            description="Desc",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        assert f.id.startswith("F-")
        assert len(f.id) == 10  # F- + 8 hex chars

    def test_confidence_upper_bound(self):
        with pytest.raises(ValidationError):
            AuditFinding(
                title="Test",
                description="Desc",
                category=FindingCategory.SECURITY,
                severity=FindingSeverity.HIGH,
                confidence=1.5,
            )

    def test_confidence_lower_bound(self):
        with pytest.raises(ValidationError):
            AuditFinding(
                title="Test",
                description="Desc",
                category=FindingCategory.SECURITY,
                severity=FindingSeverity.HIGH,
                confidence=-0.1,
            )

    def test_valid_confidence_range(self):
        for c in (0.0, 0.5, 1.0):
            f = AuditFinding(
                title="Test",
                description="Desc",
                category=FindingCategory.SECURITY,
                severity=FindingSeverity.HIGH,
                confidence=c,
            )
            assert f.confidence == c

    def test_default_confidence(self):
        f = AuditFinding(
            title="Test",
            description="Desc",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        assert f.confidence == 0.8

    def test_locations_default_empty(self):
        f = AuditFinding(
            title="Test",
            description="Desc",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        )
        assert f.locations == []

    def test_locations_populated(self):
        loc = FindingLocation(file_path="src/app.ts", line_start=10, line_end=20)
        f = AuditFinding(
            title="Test",
            description="Desc",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[loc],
        )
        assert len(f.locations) == 1
        assert f.locations[0].file_path == "src/app.ts"
        assert f.locations[0].line_start == 10


class TestCategoryScore:
    def test_score_bounds(self):
        with pytest.raises(ValidationError):
            CategoryScore(name="Security", score=101)

    def test_score_negative(self):
        with pytest.raises(ValidationError):
            CategoryScore(name="Security", score=-1)

    def test_valid_score(self):
        cs = CategoryScore(name="Security", score=85, weight=0.3)
        assert cs.score == 85

    def test_zero_score(self):
        cs = CategoryScore(name="Security", score=0)
        assert cs.score == 0

    def test_max_score(self):
        cs = CategoryScore(name="Security", score=100)
        assert cs.score == 100


class TestArchitectureReviewResult:
    def test_coherence_score_upper_bound(self):
        with pytest.raises(ValidationError):
            ArchitectureReviewResult(structural_coherence_score=101)

    def test_coherence_score_lower_bound(self):
        with pytest.raises(ValidationError):
            ArchitectureReviewResult(structural_coherence_score=-1)

    def test_coherence_score_valid(self):
        r = ArchitectureReviewResult(structural_coherence_score=75)
        assert r.structural_coherence_score == 75

    def test_default_coherence_score(self):
        r = ArchitectureReviewResult()
        assert r.structural_coherence_score == 0


class TestForgeExecutionState:
    def test_defaults(self):
        state = ForgeExecutionState()
        assert state.forge_run_id  # auto-generated
        assert state.total_agent_invocations == 0
        assert state.success is False
        assert state.all_findings == []

    def test_run_id_is_hex(self):
        state = ForgeExecutionState()
        assert len(state.forge_run_id) == 12
        # Should be valid hex
        int(state.forge_run_id, 16)

    def test_unique_run_ids(self):
        s1 = ForgeExecutionState()
        s2 = ForgeExecutionState()
        assert s1.forge_run_id != s2.forge_run_id

    def test_default_mode(self):
        from forge.schemas import ForgeMode
        state = ForgeExecutionState()
        assert state.mode == ForgeMode.FULL

    def test_empty_collections(self):
        state = ForgeExecutionState()
        assert state.security_findings == []
        assert state.quality_findings == []
        assert state.architecture_findings == []
        assert state.completed_fixes == []
        assert state.inner_loop_states == {}
