"""Tests for FORGE configuration and model resolution."""

import pytest

from forge.config import (
    ForgeConfig,
    FORGE_DEFAULT_MODELS,
    FORGE_ROLE_TO_MODEL_FIELD,
    ROLE_TO_PROVIDER,
)


class TestForgeConfig:
    def test_default_models(self):
        cfg = ForgeConfig()
        resolved = cfg.resolved_models()
        assert resolved == FORGE_DEFAULT_MODELS

    def test_default_override_all(self):
        cfg = ForgeConfig(models={"default": "test/model-v1"})
        resolved = cfg.resolved_models()
        for field, model in resolved.items():
            assert model == "test/model-v1"

    def test_role_override(self):
        cfg = ForgeConfig(models={"coder_tier2": "anthropic/claude-opus-4"})
        resolved = cfg.resolved_models()
        assert resolved["coder_tier2_model"] == "anthropic/claude-opus-4"
        # Other roles unchanged
        assert resolved["coder_tier3_model"] == FORGE_DEFAULT_MODELS["coder_tier3_model"]

    def test_role_override_with_default(self):
        cfg = ForgeConfig(models={"default": "test/base", "coder_tier2": "test/premium"})
        resolved = cfg.resolved_models()
        assert resolved["coder_tier2_model"] == "test/premium"
        assert resolved["coder_tier3_model"] == "test/base"

    def test_model_for_role(self):
        cfg = ForgeConfig()
        assert cfg.model_for_role("coder_tier2") == "anthropic/claude-sonnet-4.6"
        assert cfg.model_for_role("codebase_analyst") == "minimax/minimax-m2.5"

    def test_model_for_unknown_role(self):
        cfg = ForgeConfig()
        # Unknown role falls back to "minimax/minimax-m2.5" (default return)
        assert cfg.model_for_role("nonexistent") == "minimax/minimax-m2.5"

    def test_provider_for_role(self):
        cfg = ForgeConfig()
        assert cfg.provider_for_role("coder_tier2") == "opencode"
        assert cfg.provider_for_role("code_reviewer") == "openrouter_direct"

    def test_provider_for_unknown_role(self):
        cfg = ForgeConfig()
        assert cfg.provider_for_role("nonexistent") == "openrouter_direct"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ForgeConfig(nonexistent_field="value")

    def test_skip_tiers_default(self):
        cfg = ForgeConfig()
        assert cfg.skip_tiers == []

    def test_focus_categories_default(self):
        cfg = ForgeConfig()
        assert cfg.focus_categories == []

    def test_all_roles_have_models(self):
        """Every role in FORGE_ROLE_TO_MODEL_FIELD maps to a key in FORGE_DEFAULT_MODELS."""
        for role, field in FORGE_ROLE_TO_MODEL_FIELD.items():
            assert field in FORGE_DEFAULT_MODELS, f"Role {role} field {field} not in defaults"

    def test_all_roles_have_providers(self):
        """Every role in FORGE_ROLE_TO_MODEL_FIELD has a provider entry."""
        for role in FORGE_ROLE_TO_MODEL_FIELD:
            assert role in ROLE_TO_PROVIDER, f"Role {role} missing from ROLE_TO_PROVIDER"

    def test_resolved_models_returns_new_dict(self):
        """resolved_models should return a new dict, not mutate FORGE_DEFAULT_MODELS."""
        cfg = ForgeConfig(models={"default": "test/mutate"})
        resolved = cfg.resolved_models()
        assert resolved != FORGE_DEFAULT_MODELS
        # Verify defaults are NOT mutated
        assert FORGE_DEFAULT_MODELS["coder_tier2_model"] == "anthropic/claude-sonnet-4.6"

    def test_unknown_role_override_ignored(self):
        """Overriding a role not in FORGE_ROLE_TO_MODEL_FIELD has no effect."""
        cfg = ForgeConfig(models={"nonexistent_role": "test/model"})
        resolved = cfg.resolved_models()
        assert resolved == FORGE_DEFAULT_MODELS

    def test_default_config_values(self):
        cfg = ForgeConfig()
        assert cfg.max_inner_retries == 3
        assert cfg.max_middle_escalations == 2
        assert cfg.max_outer_replans == 1
        assert cfg.agent_timeout_seconds == 900
        assert cfg.enable_tier0_autofix is True
        assert cfg.enable_tier1_rules is True
        assert cfg.enable_parallel_audit is True
        assert cfg.enable_learning is True
        assert cfg.dry_run is False

    def test_pattern_library_path_default(self):
        cfg = ForgeConfig()
        assert cfg.pattern_library_path == ""

    def test_pattern_library_path_custom(self):
        cfg = ForgeConfig(pattern_library_path="/custom/patterns")
        assert cfg.pattern_library_path == "/custom/patterns"
