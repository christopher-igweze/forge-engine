"""Pydantic schemas for all FORGE agent I/O contracts and execution state.

Every agent's input/output is defined here so that the entire pipeline
has a single source of truth for data shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────


class RemediationTier(int, Enum):
    """Complexity tier — drives model selection and routing."""

    TIER_0 = 0  # Invalid / false-positive / auto-skip
    TIER_1 = 1  # Deterministic fix (rules-based, no LLM)
    TIER_2 = 2  # Scoped LLM fix (1-3 files, Sonnet)
    TIER_3 = 3  # Architectural LLM fix (5-15 files, Sonnet)


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(str, Enum):
    SECURITY = "security"
    QUALITY = "quality"
    ARCHITECTURE = "architecture"
    RELIABILITY = "reliability"
    PERFORMANCE = "performance"


class AuditPassType(str, Enum):
    """Sub-passes for parallel audit execution."""

    # Security passes
    AUTH_FLOW = "auth_flow"
    DATA_HANDLING = "data_handling"
    INFRASTRUCTURE = "infrastructure"
    # Quality passes
    ERROR_HANDLING = "error_handling"
    CODE_PATTERNS = "code_patterns"
    PERFORMANCE = "performance"


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    BLOCK = "BLOCK"


class EscalationAction(str, Enum):
    """Middle-loop escalation actions."""

    RECLASSIFY = "RECLASSIFY"  # Move to higher tier
    SPLIT = "SPLIT"  # Decompose into sub-fixes
    DEFER = "DEFER"  # Mark as technical debt
    ESCALATE = "ESCALATE"  # Alert human


class ForgeMode(str, Enum):
    DISCOVERY = "discovery"
    REMEDIATION = "remediation"
    VALIDATION = "validation"
    FULL = "full"


class FixOutcome(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_DEBT = "completed_with_debt"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_ESCALATED = "failed_escalated"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


# ── Agent 1: Codebase Analyst ─────────────────────────────────────────


class FileEntry(BaseModel):
    """A single file in the codebase inventory."""

    path: str
    language: str = ""
    loc: int = 0
    purpose: str = ""


class ModuleEntry(BaseModel):
    """A logical module/directory grouping."""

    name: str
    path: str
    purpose: str = ""
    files: list[str] = Field(default_factory=list)
    loc: int = 0
    language: str = ""


class DependencyEntry(BaseModel):
    """An external dependency."""

    name: str
    version: str = ""
    ecosystem: str = ""  # npm | pip | cargo | go
    dev_only: bool = False


class DataFlowEntry(BaseModel):
    """A data flow between components."""

    source: str
    destination: str
    data_type: str = ""
    is_authenticated: bool = False


class AuthBoundaryEntry(BaseModel):
    """An authentication boundary in the codebase."""

    path: str
    auth_type: str = ""  # jwt | session | api_key | none
    is_protected: bool = False


class EntryPoint(BaseModel):
    """A codebase entry point."""

    path: str
    type: str = ""  # api | cli | web | worker | cron
    is_public: bool = True


class TechStack(BaseModel):
    """Detected technology stack."""

    frontend: str | None = ""
    backend: str | None = ""
    database: str | None = ""
    hosting: str | None = ""
    packages: list[str] = Field(default_factory=list)


class CodebaseMap(BaseModel):
    """Output of Agent 1: Codebase Analyst.

    A structured understanding of the entire codebase that every
    subsequent agent receives as context.
    """

    modules: list[ModuleEntry] = Field(default_factory=list)
    dependencies: list[DependencyEntry] = Field(default_factory=list)
    data_flows: list[DataFlowEntry] = Field(default_factory=list)
    auth_boundaries: list[AuthBoundaryEntry] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    tech_stack: TechStack = Field(default_factory=TechStack)
    files: list[FileEntry] = Field(default_factory=list)
    loc_total: int = 0
    file_count: int = 0
    primary_language: str = ""
    languages: list[str] = Field(default_factory=list)
    architecture_summary: str = ""
    key_patterns: list[str] = Field(default_factory=list)


# ── Agent 2-4: Audit Findings ─────────────────────────────────────────


class FindingLocation(BaseModel):
    """Where in the codebase a finding was detected."""

    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    snippet: str = ""


class AuditFinding(BaseModel):
    """A single finding from any auditor agent (2, 3, or 4).

    This is the universal finding type used throughout the pipeline.
    """

    id: str = Field(default_factory=lambda: f"F-{uuid4().hex[:8]}")
    title: str
    description: str
    category: FindingCategory
    severity: FindingSeverity
    audit_pass: AuditPassType | None = None
    locations: list[FindingLocation] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    cwe_id: str = ""
    owasp_ref: str = ""
    agent: str = ""  # which agent produced this finding
    tier: RemediationTier | None = None  # assigned by Triage Classifier
    dedup_key: str = ""  # used by Fix Strategist for deduplication
    pattern_id: str = ""  # links to VulnerabilityPattern.id (e.g. "VP-001")
    pattern_slug: str = ""  # links to VulnerabilityPattern.slug
    data_flow: str = ""  # source -> transformation -> sink trace
    actionability: str = ""  # must_fix | should_fix | consider | informational
    intent_signal: str = ""  # intentional | ambiguous | unintentional (set by Intent Analyzer)


# ── Agent 2: Security Auditor ─────────────────────────────────────────


class SecurityAuditResult(BaseModel):
    """Output of a single security audit pass."""

    audit_pass: AuditPassType
    findings: list[AuditFinding] = Field(default_factory=list)
    pass_summary: str = ""
    files_analyzed: int = 0


class SecurityAuditAggregate(BaseModel):
    """Aggregated output of Agent 2 across all 3 security passes."""

    findings: list[AuditFinding] = Field(default_factory=list)
    pass_results: list[SecurityAuditResult] = Field(default_factory=list)
    total_findings: int = 0
    critical_count: int = 0
    high_count: int = 0


# ── Agent 3: Quality Auditor ─────────────────────────────────────────


class QualityAuditResult(BaseModel):
    """Output of a single quality audit pass."""

    audit_pass: AuditPassType
    findings: list[AuditFinding] = Field(default_factory=list)
    pass_summary: str = ""


class QualityAuditAggregate(BaseModel):
    """Aggregated output of Agent 3 across all 3 quality passes."""

    findings: list[AuditFinding] = Field(default_factory=list)
    pass_results: list[QualityAuditResult] = Field(default_factory=list)
    total_findings: int = 0


# ── Agent 4: Architecture Reviewer ────────────────────────────────────


class ArchitectureReviewResult(BaseModel):
    """Output of Agent 4: Architecture Reviewer."""

    findings: list[AuditFinding] = Field(default_factory=list)
    structural_coherence_score: int = Field(default=0, ge=0, le=100)
    coupling_assessment: str = ""
    layering_assessment: str = ""
    summary: str = ""


# ── Agent 5: Fix Strategist ──────────────────────────────────────────


class FixDependency(BaseModel):
    """A dependency between two fixes."""

    finding_id: str
    depends_on_finding_id: str
    reason: str = ""


class RemediationItem(BaseModel):
    """A single actionable item in the remediation plan."""

    finding_id: str
    title: str
    tier: RemediationTier
    priority: int = Field(ge=1)  # 1 = highest
    estimated_files: int = 1
    files_to_modify: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)  # finding IDs
    acceptance_criteria: list[str] = Field(default_factory=list)
    approach: str = ""
    group: str = ""  # parallel execution group label


class RemediationPlan(BaseModel):
    """Output of Agent 5: Fix Strategist.

    A prioritized, dependency-ordered plan for all fixes.
    """

    items: list[RemediationItem] = Field(default_factory=list)
    dependencies: list[FixDependency] = Field(default_factory=list)
    execution_levels: list[list[str]] = Field(default_factory=list)  # finding IDs
    deferred_finding_ids: list[str] = Field(default_factory=list)
    dedup_summary: str = ""
    total_items: int = 0
    estimated_invocations: int = 0
    summary: str = ""


# ── Agent 6: Triage Classifier ────────────────────────────────────────


class TriageDecision(BaseModel):
    """Tier assignment for a single finding."""

    finding_id: str
    tier: RemediationTier
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    rationale: str = ""
    fix_template_id: str = ""  # for Tier 1 deterministic fixes
    relevant_files: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """Output of Agent 6: Triage Classifier."""

    decisions: list[TriageDecision] = Field(default_factory=list)
    tier_0_count: int = 0
    tier_1_count: int = 0
    tier_2_count: int = 0
    tier_3_count: int = 0


# ── Agent 7/8: Coder ─────────────────────────────────────────────────


class CoderFixResult(BaseModel):
    """Output of Agent 7 (Tier 2) or Agent 8 (Tier 3): Coder."""

    finding_id: str
    outcome: FixOutcome
    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""
    tests_passed: bool | None = None
    error_message: str = ""
    branch_name: str = ""
    commit_sha: str = ""
    iteration: int = 1


# ── Agent 9: Test Generator ──────────────────────────────────────────


class TestGeneratorResult(BaseModel):
    """Output of Agent 9: Test Generator."""

    finding_id: str
    test_files_created: list[str] = Field(default_factory=list)
    tests_written: int = 0
    tests_passing: int = 0
    coverage_summary: str = ""
    summary: str = ""


# ── Agent 10: Code Reviewer ──────────────────────────────────────────


class ForgeCodeReviewResult(BaseModel):
    """Output of Agent 10: Code Reviewer."""

    finding_id: str
    decision: ReviewDecision
    summary: str = ""
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    regression_risk: str = "LOW"  # LOW | MEDIUM | HIGH

    @field_validator("issues", "suggestions", mode="before")
    @classmethod
    def _normalize_string_lists(cls, v: list) -> list[str]:
        """Accept dicts or strings — LLMs often return structured objects."""
        result = []
        for item in v:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                # Extract the most informative field
                desc = item.get("description", item.get("text", item.get("suggestion", "")))
                cat = item.get("category", "")
                result.append(f"[{cat}] {desc}" if cat and desc else desc or str(item))
            else:
                result.append(str(item))
        return result


# ── Agent 11: Integration Validator ───────────────────────────────────


class IntegrationValidationResult(BaseModel):
    """Output of Agent 11: Integration Validator."""

    passed: bool
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    regressions_detected: list[str] = Field(default_factory=list)
    new_issues_introduced: list[str] = Field(default_factory=list)
    summary: str = ""

    @field_validator("regressions_detected", "new_issues_introduced", mode="before")
    @classmethod
    def _normalize_string_lists(cls, v: list) -> list[str]:
        """Accept dicts or strings — LLMs often return structured objects."""
        return [item if isinstance(item, str) else str(item.get("description", item)) if isinstance(item, dict) else str(item) for item in v]


# ── Agent 12: Debt Tracker & Report Generator ─────────────────────────


class Recommendation(BaseModel):
    """A single actionable recommendation for production readiness."""

    priority: int = Field(default=1, ge=1)
    title: str
    description: str = ""
    impact: str = "medium"  # critical | high | medium | low


class DebtEntry(BaseModel):
    """A single piece of technical debt."""

    title: str
    description: str
    severity: FindingSeverity = FindingSeverity.MEDIUM
    category: FindingCategory = FindingCategory.QUALITY
    source_finding_id: str = ""
    reason_deferred: str = ""


class CategoryScore(BaseModel):
    """Score for a single readiness dimension."""

    name: str
    score: int = Field(ge=0, le=100)
    weight: float = 0.0
    details: str = ""


class ProductionReadinessReport(BaseModel):
    """Output of Agent 12: Debt Tracker & Report Generator.

    The viral acquisition hook — free scan produces this score.
    """

    overall_score: int = Field(default=0, ge=0, le=100)
    category_scores: list[CategoryScore] = Field(default_factory=list)
    findings_total: int = 0
    findings_fixed: int = 0
    findings_deferred: int = 0
    debt_items: list[DebtEntry] = Field(default_factory=list)
    summary: str = ""
    recommendations: list[Recommendation] = Field(default_factory=list)
    investor_summary: str = ""

    @field_validator("recommendations", mode="before")
    @classmethod
    def _normalize_recommendations(cls, v: list) -> list[dict]:
        """Accept plain strings, dicts, or Recommendation objects."""
        normalized = []
        for i, item in enumerate(v):
            if isinstance(item, str):
                normalized.append({"priority": i + 1, "title": item})
            elif isinstance(item, dict):
                # Ensure priority has a default
                if "priority" not in item:
                    item["priority"] = i + 1
                normalized.append(item)
            else:
                # Already a Recommendation instance
                normalized.append(item)
        return normalized


# ── Control Loop State ────────────────────────────────────────────────


class InnerLoopState(BaseModel):
    """Tracks the inner coder retry loop for a single fix."""

    finding_id: str
    iteration: int = 0
    max_iterations: int = 3
    coder_result: CoderFixResult | None = None
    review_result: ForgeCodeReviewResult | None = None
    test_result: TestGeneratorResult | None = None
    review_feedback: str = ""


class EscalationDecision(BaseModel):
    """Decision from the middle loop when inner loop is exhausted."""

    finding_id: str
    action: EscalationAction
    rationale: str = ""
    new_tier: RemediationTier | None = None  # for RECLASSIFY
    split_items: list[RemediationItem] = Field(default_factory=list)


class OuterLoopState(BaseModel):
    """Tracks the outer re-planning loop."""

    iteration: int = 0
    max_iterations: int = 1
    remaining_plan: RemediationPlan | None = None
    completed_fixes: list[CoderFixResult] = Field(default_factory=list)
    deferred_findings: list[str] = Field(default_factory=list)
    escalations: list[EscalationDecision] = Field(default_factory=list)


# ── Full Execution State ──────────────────────────────────────────────


class ForgeExecutionState(BaseModel):
    """Complete state of a FORGE run, enabling checkpoint/resume."""

    forge_run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    mode: ForgeMode = ForgeMode.FULL
    repo_url: str = ""
    repo_path: str = ""
    artifacts_dir: str = ""

    # Discovery outputs
    codebase_map: CodebaseMap | None = None
    security_findings: list[AuditFinding] = Field(default_factory=list)
    quality_findings: list[AuditFinding] = Field(default_factory=list)
    architecture_findings: list[AuditFinding] = Field(default_factory=list)
    all_findings: list[AuditFinding] = Field(default_factory=list)

    # Triage outputs
    triage_result: TriageResult | None = None
    remediation_plan: RemediationPlan | None = None

    # Remediation state
    inner_loop_states: dict[str, InnerLoopState] = Field(default_factory=dict)
    outer_loop: OuterLoopState = Field(default_factory=OuterLoopState)
    completed_fixes: list[CoderFixResult] = Field(default_factory=list)

    # Validation outputs
    integration_result: IntegrationValidationResult | None = None
    readiness_report: ProductionReadinessReport | None = None

    # Metadata
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    total_agent_invocations: int = 0
    estimated_cost_usd: float = 0.0
    success: bool = False


class ForgeResult(BaseModel):
    """Final output of a FORGE run."""

    forge_run_id: str
    success: bool
    mode: ForgeMode
    summary: str
    pr_url: str = ""
    readiness_report: ProductionReadinessReport | None = None
    discovery_report: dict | None = None
    total_findings: int = 0
    findings_fixed: int = 0
    findings_deferred: int = 0
    agent_invocations: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
