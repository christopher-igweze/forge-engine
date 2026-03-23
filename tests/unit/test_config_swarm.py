"""Tests for swarm-related configuration in ForgeConfig."""

import pytest
from pydantic import ValidationError

from forge.config import (
    FORGE_DEFAULT_MODELS,
    FORGE_ROLE_TO_MODEL_FIELD,
    ROLE_TO_PROVIDER,
    ForgeConfig,
)


class TestDiscoveryModeDefault:
    """discovery_mode defaults to 'classic'."""

    def test_default_is_classic(self):
        cfg = ForgeConfig()
        assert cfg.discovery_mode == "classic"


class TestDiscoveryModeSwarm:
    """discovery_mode='swarm' is accepted."""

    def test_swarm_accepted(self):
        cfg = ForgeConfig(discovery_mode="swarm")
        assert cfg.discovery_mode == "swarm"


class TestDiscoveryModeInvalid:
    """Anything other than 'classic' or 'swarm' raises validation error."""

    def test_invalid_mode_raises(self):
        with pytest.raises(ValidationError):
            ForgeConfig(discovery_mode="hybrid")

    def test_empty_string_raises(self):
        with pytest.raises(ValidationError):
            ForgeConfig(discovery_mode="")

    def test_none_raises(self):
        with pytest.raises(ValidationError):
            ForgeConfig(discovery_mode=None)


class TestSwarmSettingsDefaults:
    """Swarm settings have correct defaults."""

    def test_target_segments_default(self):
        cfg = ForgeConfig()
        assert cfg.swarm_target_segments == 5

    def test_enable_wave2_default(self):
        cfg = ForgeConfig()
        assert cfg.swarm_enable_wave2 is True

    def test_worker_types_default(self):
        cfg = ForgeConfig()
        assert cfg.swarm_worker_types == ["security", "quality", "architecture"]


class TestSwarmSettingsCustom:
    """Custom swarm settings can be overridden."""

    def test_override_target_segments(self):
        cfg = ForgeConfig(swarm_target_segments=10)
        assert cfg.swarm_target_segments == 10

    def test_disable_wave2(self):
        cfg = ForgeConfig(swarm_enable_wave2=False)
        assert cfg.swarm_enable_wave2 is False

    def test_custom_worker_types(self):
        cfg = ForgeConfig(swarm_worker_types=["security", "performance"])
        assert cfg.swarm_worker_types == ["security", "performance"]

    def test_all_overrides_combined(self):
        cfg = ForgeConfig(
            discovery_mode="swarm",
            swarm_target_segments=8,
            swarm_enable_wave2=False,
            swarm_worker_types=["quality"],
        )
        assert cfg.discovery_mode == "swarm"
        assert cfg.swarm_target_segments == 8
        assert cfg.swarm_enable_wave2 is False
        assert cfg.swarm_worker_types == ["quality"]


class TestSwarmModelResolution:
    """swarm_worker and synthesizer roles resolve to correct defaults."""

    def test_swarm_worker_default_model(self):
        cfg = ForgeConfig()
        model = cfg.model_for_role("swarm_worker")
        assert model == "minimax/minimax-m2.5"

    def test_synthesizer_default_model(self):
        cfg = ForgeConfig()
        model = cfg.model_for_role("synthesizer")
        assert model == "minimax/minimax-m2.5"

    def test_swarm_roles_in_role_mapping(self):
        assert "swarm_worker" in FORGE_ROLE_TO_MODEL_FIELD
        assert "synthesizer" in FORGE_ROLE_TO_MODEL_FIELD

    def test_swarm_roles_in_default_models(self):
        assert "swarm_worker_model" in FORGE_DEFAULT_MODELS
        assert "synthesizer_model" in FORGE_DEFAULT_MODELS


class TestSwarmProviderRouting:
    """swarm_worker and synthesizer map to openrouter_direct."""

    def test_swarm_worker_provider(self):
        cfg = ForgeConfig()
        assert cfg.provider_for_role("swarm_worker") == "openrouter_direct"

    def test_synthesizer_provider(self):
        cfg = ForgeConfig()
        assert cfg.provider_for_role("synthesizer") == "openrouter_direct"

    def test_swarm_roles_in_provider_map(self):
        assert "swarm_worker" in ROLE_TO_PROVIDER
        assert "synthesizer" in ROLE_TO_PROVIDER
        assert ROLE_TO_PROVIDER["swarm_worker"] == "openrouter_direct"
        assert ROLE_TO_PROVIDER["synthesizer"] == "openrouter_direct"


class TestExtraFieldsRejected:
    """Pydantic strict mode rejects unknown fields."""

    def test_extra_field_raises(self):
        with pytest.raises(ValidationError):
            ForgeConfig(bogus_swarm_field="nope")

    def test_extra_field_near_swarm_raises(self):
        with pytest.raises(ValidationError):
            ForgeConfig(swarm_bogus_option=True)


class TestSwarmModelOverride:
    """models={'swarm_worker': 'custom/model'} overrides swarm_worker_model."""

    def test_override_swarm_worker_model(self):
        cfg = ForgeConfig(models={"swarm_worker": "custom/fast-model"})
        resolved = cfg.resolved_models()
        assert resolved["swarm_worker_model"] == "custom/fast-model"
        # Synthesizer should remain default
        assert resolved["synthesizer_model"] == FORGE_DEFAULT_MODELS["synthesizer_model"]

    def test_override_synthesizer_model(self):
        cfg = ForgeConfig(models={"synthesizer": "anthropic/claude-opus-4"})
        resolved = cfg.resolved_models()
        assert resolved["synthesizer_model"] == "anthropic/claude-opus-4"
        # Worker should remain default
        assert resolved["swarm_worker_model"] == FORGE_DEFAULT_MODELS["swarm_worker_model"]

    def test_model_for_role_with_override(self):
        cfg = ForgeConfig(models={"swarm_worker": "custom/model-v2"})
        assert cfg.model_for_role("swarm_worker") == "custom/model-v2"

    def test_default_override_applies_to_swarm(self):
        cfg = ForgeConfig(models={"default": "test/universal"})
        resolved = cfg.resolved_models()
        assert resolved["swarm_worker_model"] == "test/universal"
        assert resolved["synthesizer_model"] == "test/universal"

    def test_role_override_beats_default_for_swarm(self):
        cfg = ForgeConfig(models={
            "default": "test/base",
            "swarm_worker": "test/premium",
        })
        resolved = cfg.resolved_models()
        assert resolved["swarm_worker_model"] == "test/premium"
        assert resolved["synthesizer_model"] == "test/base"
