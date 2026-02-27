"""Tests for vulnerability pattern LLM context builder."""

import pytest

from forge.patterns.context import (
    build_pattern_context_for_prompt,
    extract_tech_hints_from_codebase_map,
)
from forge.patterns.loader import PatternLibrary
from forge.patterns.schema import (
    DeterministicSignal,
    LLMGuidance,
    SignalType,
    VulnerabilityPattern,
)


def _make_library() -> PatternLibrary:
    """Build a small test library."""
    return PatternLibrary([
        VulnerabilityPattern(
            id="VP-T1",
            name="Test Pattern One",
            slug="test-one",
            category="security",
            severity_default="critical",
            cwe_ids=["CWE-285"],
            llm_guidance=LLMGuidance(
                reasoning_prompt="Look for bad things",
                key_questions=["Is it bad?", "How bad?"],
                technology_variants={
                    "supabase": "Check RLS policies",
                    "firebase": "Check firestore rules",
                    "generic": "Check server-side validation",
                },
            ),
        ),
        VulnerabilityPattern(
            id="VP-T2",
            name="Quality Issue",
            slug="quality-issue",
            category="quality",
            severity_default="medium",
        ),
    ])


class TestBuildPatternContext:
    def test_includes_pattern_name(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "VP-T1" in ctx
        assert "Test Pattern One" in ctx

    def test_includes_severity(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "critical" in ctx

    def test_includes_cwe(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "CWE-285" in ctx

    def test_includes_key_questions(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "Is it bad?" in ctx
        assert "How bad?" in ctx

    def test_includes_reasoning_prompt(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "Look for bad things" in ctx

    def test_filters_by_category(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "VP-T1" in ctx
        assert "VP-T2" not in ctx

    def test_all_categories_when_empty(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="")
        assert "VP-T1" in ctx
        assert "VP-T2" in ctx

    def test_tech_hints_supabase(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(
            lib, category="security", tech_hints=["supabase"],
        )
        assert "Check RLS policies" in ctx
        assert "Check firestore rules" not in ctx

    def test_tech_hints_firebase(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(
            lib, category="security", tech_hints=["firebase"],
        )
        assert "Check firestore rules" in ctx
        assert "Check RLS policies" not in ctx

    def test_generic_fallback_when_no_tech_match(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(
            lib, category="security", tech_hints=[],
        )
        assert "Check server-side validation" in ctx

    def test_no_generic_when_tech_matched(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(
            lib, category="security", tech_hints=["supabase"],
        )
        assert "Check server-side validation" not in ctx

    def test_empty_library_returns_empty(self):
        lib = PatternLibrary()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert ctx == ""

    def test_no_matching_category_returns_empty(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="performance")
        assert ctx == ""

    def test_includes_pattern_id_instruction(self):
        lib = _make_library()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "pattern_id" in ctx
        assert "pattern_slug" in ctx

    def test_default_library_produces_context(self):
        lib = PatternLibrary.load_default()
        ctx = build_pattern_context_for_prompt(lib, category="security")
        assert "VP-001" in ctx
        assert "VP-002" in ctx
        assert "VP-003" in ctx


class TestExtractTechHints:
    def test_supabase_in_packages(self):
        codebase_map = {
            "tech_stack": {"packages": ["@supabase/supabase-js", "react"]},
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert "supabase" in hints

    def test_firebase_in_dependencies(self):
        codebase_map = {
            "dependencies": [{"name": "firebase"}, {"name": "express"}],
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert "firebase" in hints

    def test_pocketbase_in_modules(self):
        codebase_map = {
            "modules": [{"name": "pocketbase-client"}],
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert "pocketbase" in hints

    def test_deduplicates(self):
        codebase_map = {
            "tech_stack": {"packages": ["supabase"]},
            "dependencies": [{"name": "@supabase/supabase-js"}],
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert hints.count("supabase") == 1

    def test_empty_codebase_map(self):
        hints = extract_tech_hints_from_codebase_map({})
        assert hints == []

    def test_string_dependencies(self):
        codebase_map = {
            "dependencies": ["firebase", "express"],
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert "firebase" in hints

    def test_multiple_techs(self):
        codebase_map = {
            "tech_stack": {"packages": ["supabase", "firebase-admin"]},
        }
        hints = extract_tech_hints_from_codebase_map(codebase_map)
        assert "supabase" in hints
        assert "firebase" in hints
