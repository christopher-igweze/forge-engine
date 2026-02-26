"""Tests for actionability classification.

Covers:
- classify_actionability: severity × confidence × stage matrix
- Known compromise detection → informational
- Architecture findings in early stages → informational
- apply_actionability: batch application, LLM override, downgrade logic
"""

from __future__ import annotations

from forge.execution.actionability import (
    apply_actionability,
    classify_actionability,
)


# ── classify_actionability ─────────────────────────────────────────


class TestClassifyActionability:
    def test_critical_high_confidence_is_must_fix(self):
        finding = {"severity": "critical", "confidence": 0.92}
        assert classify_actionability(finding) == "must_fix"

    def test_critical_moderate_confidence_is_should_fix(self):
        finding = {"severity": "critical", "confidence": 0.75}
        assert classify_actionability(finding) == "should_fix"

    def test_high_severity_growth_stage_is_must_fix(self):
        finding = {"severity": "high", "confidence": 0.85}
        ctx = {"project_stage": "growth"}
        assert classify_actionability(finding, ctx) == "must_fix"

    def test_high_severity_enterprise_is_must_fix(self):
        finding = {"severity": "high", "confidence": 0.85}
        ctx = {"project_stage": "enterprise"}
        assert classify_actionability(finding, ctx) == "must_fix"

    def test_high_severity_mvp_is_should_fix(self):
        finding = {"severity": "high", "confidence": 0.85}
        ctx = {"project_stage": "mvp"}
        assert classify_actionability(finding, ctx) == "should_fix"

    def test_high_severity_no_context_is_should_fix(self):
        finding = {"severity": "high", "confidence": 0.85}
        assert classify_actionability(finding) == "should_fix"

    def test_medium_severity_is_should_fix(self):
        finding = {"severity": "medium", "confidence": 0.75}
        assert classify_actionability(finding) == "should_fix"

    def test_medium_severity_mvp_is_consider(self):
        finding = {"severity": "medium", "confidence": 0.6}
        ctx = {"project_stage": "mvp"}
        assert classify_actionability(finding, ctx) == "consider"

    def test_medium_severity_early_product_is_consider(self):
        finding = {"severity": "medium", "confidence": 0.6}
        ctx = {"project_stage": "early_product"}
        assert classify_actionability(finding, ctx) == "consider"

    def test_low_severity_is_informational(self):
        finding = {"severity": "low", "confidence": 0.8}
        assert classify_actionability(finding) == "informational"

    def test_architecture_mvp_is_informational(self):
        finding = {"severity": "medium", "confidence": 0.6, "category": "architecture"}
        ctx = {"project_stage": "mvp"}
        assert classify_actionability(finding, ctx) == "informational"

    def test_architecture_growth_is_not_auto_informational(self):
        finding = {"severity": "medium", "confidence": 0.75, "category": "architecture"}
        ctx = {"project_stage": "growth"}
        assert classify_actionability(finding, ctx) == "should_fix"

    def test_known_compromise_match_in_description(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "description": "No rate limiting on API endpoints",
            "title": "Missing rate limiter",
        }
        ctx = {"known_compromises": ["No rate limiting"]}
        assert classify_actionability(finding, ctx) == "informational"

    def test_known_compromise_match_in_title(self):
        finding = {
            "severity": "critical",
            "confidence": 0.95,
            "description": "Users can access other users' data",
            "title": "Auth is basic and incomplete",
        }
        ctx = {"known_compromises": ["Auth is basic"]}
        assert classify_actionability(finding, ctx) == "informational"

    def test_known_compromise_case_insensitive(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "description": "NO TESTS detected in project",
        }
        ctx = {"known_compromises": ["no tests"]}
        assert classify_actionability(finding, ctx) == "informational"

    def test_known_compromise_no_match(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "description": "SQL injection in login endpoint",
        }
        ctx = {"known_compromises": ["No rate limiting"]}
        assert classify_actionability(finding, ctx) == "should_fix"

    def test_empty_finding_returns_informational(self):
        # Default severity is "low", default confidence is 0.0 → informational
        assert classify_actionability({}) == "informational"

    def test_no_project_context_is_safe(self):
        finding = {"severity": "high", "confidence": 0.85}
        assert classify_actionability(finding, None) == "should_fix"


# ── apply_actionability ────────────────────────────────────────────


class TestApplyActionability:
    def test_fills_empty_actionability(self):
        findings = [
            {"severity": "critical", "confidence": 0.95, "actionability": ""},
            {"severity": "low", "confidence": 0.7, "actionability": ""},
        ]
        result = apply_actionability(findings)
        assert result[0]["actionability"] == "must_fix"
        assert result[1]["actionability"] == "informational"

    def test_preserves_llm_actionability_by_default(self):
        findings = [
            {"severity": "critical", "confidence": 0.95, "actionability": "should_fix"},
        ]
        result = apply_actionability(findings)
        # LLM said should_fix, we don't override upward
        assert result[0]["actionability"] == "should_fix"

    def test_override_llm_when_requested(self):
        findings = [
            {"severity": "critical", "confidence": 0.95, "actionability": "should_fix"},
        ]
        result = apply_actionability(findings, override_llm=True)
        assert result[0]["actionability"] == "must_fix"

    def test_known_compromise_downgrades_llm(self):
        findings = [
            {
                "severity": "high",
                "confidence": 0.9,
                "actionability": "must_fix",
                "description": "No rate limiting detected",
            },
        ]
        ctx = {"known_compromises": ["No rate limiting"]}
        result = apply_actionability(findings, project_context=ctx)
        # Known compromise forces downgrade even without override_llm
        assert result[0]["actionability"] == "informational"

    def test_no_upward_reclassification(self):
        """apply_actionability should not upgrade LLM's classification."""
        findings = [
            {"severity": "high", "confidence": 0.9, "actionability": "must_fix"},
        ]
        ctx = {"project_stage": "mvp"}
        result = apply_actionability(findings, project_context=ctx)
        # Classifier says should_fix for high+mvp, but LLM said must_fix — keep must_fix
        assert result[0]["actionability"] == "must_fix"

    def test_empty_findings_list(self):
        result = apply_actionability([])
        assert result == []

    def test_missing_actionability_key(self):
        findings = [{"severity": "critical", "confidence": 0.95}]
        result = apply_actionability(findings)
        assert result[0]["actionability"] == "must_fix"
