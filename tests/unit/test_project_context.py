"""Tests for the project context string builder.

Covers:
- Empty/None input returns empty string
- Each field renders correctly when present
- Stage labels map to human-readable descriptions
- Known compromises trigger informational classification instruction
- Beloved features trigger minimal-impact fix instruction
- Sensitive data types trigger severity escalation instruction
- Scale guidance fires for MVP/early_product and small teams
- Scale guidance does NOT fire for growth/enterprise stages
- Original prompt is truncated at 500 chars
- XML wrapper tags present
"""

from __future__ import annotations

import pytest

from forge.prompts.project_context import build_project_context_string


class TestBuildProjectContextString:
    def test_empty_dict_returns_empty(self):
        assert build_project_context_string({}) == ""

    def test_none_returns_empty(self):
        assert build_project_context_string(None) == ""

    def test_project_stage_mvp(self):
        result = build_project_context_string({"project_stage": "mvp"})
        assert "MVP / Prototype" in result
        assert "early stage" in result

    def test_project_stage_growth(self):
        result = build_project_context_string({"project_stage": "growth"})
        assert "Growth Stage" in result
        assert "scaling concerns" in result

    def test_project_stage_enterprise(self):
        result = build_project_context_string({"project_stage": "enterprise"})
        assert "Enterprise" in result
        assert "compliance" in result

    def test_unknown_stage_uses_raw_value(self):
        result = build_project_context_string({"project_stage": "pre_seed"})
        assert "pre_seed" in result

    def test_team_size_solo(self):
        result = build_project_context_string({"team_size": 1})
        assert "1 developer" in result
        # Should NOT say "developers" (plural)
        assert "1 developers" not in result

    def test_team_size_plural(self):
        result = build_project_context_string({"team_size": 5})
        assert "5 developers" in result

    def test_team_size_zero_omitted(self):
        result = build_project_context_string({"team_size": 0, "project_stage": "mvp"})
        assert "Team Size" not in result

    def test_vision_summary(self):
        result = build_project_context_string({"vision_summary": "AI-powered fitness tracker"})
        assert "AI-powered fitness tracker" in result
        assert "**Vision:**" in result

    def test_target_launch(self):
        result = build_project_context_string({"target_launch": "2 weeks"})
        assert "2 weeks" in result
        assert "**Target Launch:**" in result

    def test_known_compromises(self):
        result = build_project_context_string({
            "known_compromises": ["Auth is basic", "No tests yet"]
        })
        assert "- Auth is basic" in result
        assert "- No tests yet" in result
        assert "informational" in result
        assert "developer already knows" in result

    def test_beloved_features(self):
        result = build_project_context_string({
            "beloved_features": ["Real-time tracking", "Edge functions"]
        })
        assert "Real-time tracking" in result
        assert "Edge functions" in result
        assert "minimal-impact fixes" in result
        assert "preserve their design intent" in result

    def test_original_prompt(self):
        result = build_project_context_string({
            "original_prompt": "Build a fitness app with Supabase auth"
        })
        assert "Build a fitness app" in result
        assert "> Build a fitness app" in result

    def test_original_prompt_truncated_at_500(self):
        long_prompt = "x" * 600
        result = build_project_context_string({"original_prompt": long_prompt})
        # The prompt should be truncated, not the full 600 chars
        assert "x" * 500 in result
        assert "x" * 501 not in result

    def test_sensitive_data_types(self):
        result = build_project_context_string({
            "sensitive_data_types": ["payments", "pii", "health"]
        })
        assert "payments" in result
        assert "pii" in result
        assert "health" in result
        assert "Escalate severity" in result

    def test_scale_guidance_mvp(self):
        result = build_project_context_string({"project_stage": "mvp"})
        assert "Scale Guidance" in result
        assert "Do NOT recommend architectural patterns" in result

    def test_scale_guidance_early_product(self):
        result = build_project_context_string({"project_stage": "early_product"})
        assert "Scale Guidance" in result

    def test_scale_guidance_small_team(self):
        result = build_project_context_string({"team_size": 2})
        assert "Scale Guidance" in result

    def test_no_scale_guidance_growth(self):
        result = build_project_context_string({
            "project_stage": "growth",
            "team_size": 10,
        })
        assert "Scale Guidance" not in result

    def test_no_scale_guidance_enterprise(self):
        result = build_project_context_string({
            "project_stage": "enterprise",
            "team_size": 50,
        })
        assert "Scale Guidance" not in result

    def test_xml_wrapper_tags(self):
        result = build_project_context_string({"project_stage": "mvp"})
        assert result.startswith("<project_context>")
        assert result.endswith("</project_context>")

    def test_full_context(self):
        """All fields populated — smoke test."""
        ctx = {
            "project_stage": "early_product",
            "team_size": 3,
            "vision_summary": "AI audit platform",
            "target_launch": "3 months",
            "known_compromises": ["No rate limiting yet"],
            "beloved_features": ["Real-time scan status"],
            "original_prompt": "Build a security scanner",
            "sensitive_data_types": ["auth_secrets"],
        }
        result = build_project_context_string(ctx)
        assert "Early Product" in result
        assert "3 developers" in result
        assert "AI audit platform" in result
        assert "3 months" in result
        assert "No rate limiting yet" in result
        assert "Real-time scan status" in result
        assert "Build a security scanner" in result
        assert "auth_secrets" in result
        assert "<project_context>" in result
        assert "</project_context>" in result
