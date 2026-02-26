"""Tests for vulnerability pattern schema models."""

import pytest

from forge.patterns.schema import (
    DeterministicSignal,
    LLMGuidance,
    PatternSource,
    PatternTier,
    SignalType,
    VulnerabilityPattern,
)


class TestPatternTierEnum:
    def test_values(self):
        assert PatternTier.DETERMINISTIC == "deterministic"
        assert PatternTier.HYBRID == "hybrid"
        assert PatternTier.LLM_ONLY == "llm_only"

    def test_from_string(self):
        assert PatternTier("hybrid") == PatternTier.HYBRID


class TestSignalTypeEnum:
    def test_values(self):
        assert SignalType.REGEX == "regex"
        assert SignalType.AST == "ast"
        assert SignalType.DEPENDENCY == "dependency"
        assert SignalType.FILE_PRESENCE == "file_presence"
        assert SignalType.SCHEMA_COLUMN == "schema_column"


class TestPatternSourceEnum:
    def test_values(self):
        assert PatternSource.CURATED == "curated"
        assert PatternSource.SCAN_DERIVED == "scan_derived"


class TestDeterministicSignal:
    def test_defaults(self):
        sig = DeterministicSignal(signal_type=SignalType.REGEX)
        assert sig.weight == 1.0
        assert sig.is_positive is True
        assert sig.patterns == []
        assert sig.file_globs == []
        assert sig.package_names == []
        assert sig.description == ""

    def test_full_construction(self):
        sig = DeterministicSignal(
            signal_type=SignalType.DEPENDENCY,
            description="BaaS SDK detected",
            package_names=["supabase", "firebase"],
            weight=0.3,
            is_positive=True,
        )
        assert sig.signal_type == SignalType.DEPENDENCY
        assert len(sig.package_names) == 2
        assert sig.weight == 0.3

    def test_inverted_signal(self):
        sig = DeterministicSignal(
            signal_type=SignalType.FILE_PRESENCE,
            patterns=["supabase/functions/"],
            weight=0.2,
            is_positive=False,
        )
        assert sig.is_positive is False


class TestLLMGuidance:
    def test_defaults(self):
        g = LLMGuidance()
        assert g.reasoning_prompt == ""
        assert g.examples == []
        assert g.counter_examples == []
        assert g.key_questions == []
        assert g.technology_variants == {}

    def test_full_construction(self):
        g = LLMGuidance(
            reasoning_prompt="Check for X",
            key_questions=["Q1", "Q2"],
            technology_variants={"supabase": "Check RLS"},
            examples=[{"code": "bad()", "is_vulnerable": True}],
            counter_examples=[{"code": "good()", "is_vulnerable": False}],
        )
        assert len(g.key_questions) == 2
        assert "supabase" in g.technology_variants
        assert len(g.examples) == 1


class TestVulnerabilityPattern:
    def test_minimal_construction(self):
        p = VulnerabilityPattern(
            id="VP-TEST",
            name="Test Pattern",
            slug="test-pattern",
        )
        assert p.id == "VP-TEST"
        assert p.category == "security"
        assert p.severity_default == "critical"
        assert p.tier == PatternTier.HYBRID
        assert p.source == PatternSource.CURATED
        assert p.deterministic_threshold == 0.7
        assert p.times_detected == 0
        assert p.signals == []
        assert p.cwe_ids == []

    def test_full_construction(self):
        p = VulnerabilityPattern(
            id="VP-001",
            name="Client-writable server-authority columns",
            slug="client-writable-server-authority",
            description="BaaS platforms allow direct writes...",
            category="security",
            severity_default="critical",
            cwe_ids=["CWE-285", "CWE-639"],
            owasp_refs=["A01:2021"],
            tier=PatternTier.HYBRID,
            signals=[
                DeterministicSignal(
                    signal_type=SignalType.DEPENDENCY,
                    package_names=["supabase"],
                    weight=0.3,
                ),
                DeterministicSignal(
                    signal_type=SignalType.REGEX,
                    patterns=[r"\.update\("],
                    weight=0.4,
                ),
            ],
            deterministic_threshold=0.7,
            llm_guidance=LLMGuidance(
                reasoning_prompt="Check for direct column writes",
                key_questions=["Can client write role columns?"],
            ),
            fix_strategy="Move writes to server-side functions",
            fix_examples={"supabase": "Use edge functions"},
            times_detected=5,
            times_confirmed=3,
            false_positive_rate=0.1,
        )
        assert len(p.signals) == 2
        assert p.signals[0].weight == 0.3
        assert p.signals[1].weight == 0.4
        assert p.llm_guidance.reasoning_prompt.startswith("Check")
        assert p.times_detected == 5
        assert p.false_positive_rate == 0.1

    def test_round_trip_dict(self):
        data = {
            "id": "VP-TEST",
            "name": "Test",
            "slug": "test",
            "category": "security",
            "severity_default": "high",
            "cwe_ids": ["CWE-100"],
            "tier": "deterministic",
            "signals": [
                {
                    "signal_type": "regex",
                    "patterns": ["bad_pattern"],
                    "weight": 0.5,
                }
            ],
        }
        p = VulnerabilityPattern(**data)
        assert p.tier == PatternTier.DETERMINISTIC
        assert p.signals[0].signal_type == SignalType.REGEX

        d = p.model_dump()
        assert d["id"] == "VP-TEST"
        assert d["signals"][0]["weight"] == 0.5

    def test_prevalence_defaults(self):
        p = VulnerabilityPattern(id="VP-X", name="X", slug="x")
        assert p.times_detected == 0
        assert p.times_confirmed == 0
        assert p.false_positive_rate == 0.0

    def test_source_default_is_curated(self):
        p = VulnerabilityPattern(id="VP-X", name="X", slug="x")
        assert p.source == PatternSource.CURATED

    def test_scan_derived_source(self):
        p = VulnerabilityPattern(
            id="VP-X",
            name="X",
            slug="x",
            source=PatternSource.SCAN_DERIVED,
            source_url="https://example.com/vuln",
        )
        assert p.source == PatternSource.SCAN_DERIVED
        assert p.source_url == "https://example.com/vuln"
