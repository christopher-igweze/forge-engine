"""FORGE configuration and per-role model routing.

Resolution order: FORGE_DEFAULT_MODELS < models.default < models.<role>
"""

from __future__ import annotations

from typing import Literal

import re

from pydantic import BaseModel, ConfigDict, field_validator

from forge.schemas import ForgeMode


# ── Role-to-field mapping ─────────────────────────────────────────────

FORGE_ROLE_TO_MODEL_FIELD: dict[str, str] = {
    "codebase_analyst": "codebase_analyst_model",
    "security_auditor": "security_auditor_model",
    "fix_strategist": "fix_strategist_model",
}

# ── Default model assignments per spec ────────────────────────────────

FORGE_DEFAULT_MODELS: dict[str, str] = {
    # Analysis agents — cheap, high-throughput
    "codebase_analyst_model": "minimax/minimax-m2.5",
    # Reasoning agents — mid-tier
    "security_auditor_model": "anthropic/claude-haiku-4.5",
    "fix_strategist_model": "anthropic/claude-haiku-4.5",
}

# ── Provider routing ──────────────────────────────────────────────────

# All discovery agents use openrouter_direct (text-in/JSON-out, no tools)

ROLE_TO_PROVIDER: dict[str, str] = {
    "codebase_analyst": "openrouter_direct",
    "security_auditor": "openrouter_direct",
    "fix_strategist": "openrouter_direct",
}


class ForgeConfig(BaseModel):
    """Configuration for a FORGE run."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["open_code"] = "open_code"
    models: dict[str, str] | None = None  # role -> model overrides

    mode: ForgeMode = ForgeMode.FULL
    agent_timeout_seconds: int = 900  # 15 min per agent

    enable_parallel_audit: bool = True  # Run audit passes concurrently

    repo_url: str = ""
    repo_path: str = ""

    # ── Budget / Circuit Breakers ────────────────────────────────────
    max_cost_usd: float = 0.0            # 0 = no limit (user's own API key, their cost)
    max_duration_seconds: float = 0.0     # 0 = no limit
    cost_warning_threshold: float = 0.8  # Log warning at 80% of budget

    # ── Vulnerability Pattern Library ────────────────────────────
    pattern_library_path: str = ""  # Custom path; empty = use built-in library

    # ── Project Context ───────────────────────────────────────────
    project_context: dict = {}  # User-provided project context for scan personalization

    # ── Webhook Event Emission ────────────────────────────────────
    webhook_url: str = ""       # POST endpoint for scan progress events
    webhook_token: str = ""     # HMAC-SHA256 signing secret — load from env vars, not config files
    webhook_scan_id: str = ""   # Scan ID included in every event payload

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str) -> str:
        if not v:
            return v  # Empty is OK (webhook disabled)
        from urllib.parse import urlparse
        parsed = urlparse(v)
        # Allow HTTP only for localhost (development)
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(f"webhook_url must use HTTPS for non-localhost: {v}")
        if parsed.scheme not in ("https", "http"):
            raise ValueError(f"webhook_url must be HTTP(S): {v}")
        if not parsed.hostname:
            raise ValueError(f"webhook_url must have a hostname: {v}")
        return v

    # ── Delta Mode ─────────────────────────────────────────────────────
    delta_mode: bool = False  # Only scan files changed since last scan

    # ── Quality Gate Thresholds ───────────────────────────────────────
    quality_gate_max_critical: int = 0       # Max new critical findings (0 = none allowed)
    quality_gate_max_high: int = 0           # Max new high findings (0 = none allowed)
    quality_gate_max_medium: int | None = None  # None = no limit on new medium findings

    # ── Evaluation Framework (v3) ──────────────────────────────────────
    evaluation_weights: dict[str, float] | None = None  # Dimension weight overrides
    quality_gate_profile: str = "forge-way"  # "forge-way" | "strict" | "startup" | custom JSON

    # ── Opengrep Integration ──────────────────────────────────────────
    opengrep_enabled: bool = True          # Use Opengrep for deterministic scanning
    opengrep_community_rules: bool = True  # Include community rules alongside FORGE rules
    opengrep_timeout: int = 300            # Max seconds for Opengrep scan
    opengrep_rules_dir: str = ""           # Custom rules dir (empty = use built-in forge/rules/)

    # ── Data Sharing ───────────────────────────────────────────────
    share_forgeignore: bool = True  # Share anonymized .forgeignore suppression data

    def resolved_models(self) -> dict[str, str]:
        """Resolve model fields using the standard cascade.

        Resolution: FORGE_DEFAULT_MODELS < models.default < models.<role>
        """
        resolved = dict(FORGE_DEFAULT_MODELS)
        overrides = self.models or {}

        # Apply default override to all fields
        default_model = overrides.get("default")
        if default_model:
            _validate_model_id(default_model)
            for field in resolved:
                resolved[field] = default_model

        # Apply per-role overrides
        for role, model_id in overrides.items():
            if role == "default":
                continue
            _validate_model_id(model_id)
            field = FORGE_ROLE_TO_MODEL_FIELD.get(role)
            if field and field in resolved:
                resolved[field] = model_id

        return resolved

    def model_for_role(self, role: str) -> str:
        """Get the resolved model ID for a specific agent role."""
        field = FORGE_ROLE_TO_MODEL_FIELD.get(role, "")
        return self.resolved_models().get(field, "minimax/minimax-m2.5")

    def provider_for_role(self, role: str) -> str:
        """Get the provider name for a specific agent role."""
        return ROLE_TO_PROVIDER.get(role, "openrouter_direct")


def _validate_model_id(model_id: str) -> None:
    """Validate model ID format: provider/model-name."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.:-]+", model_id):
        raise ValueError(f"Invalid model ID format: '{model_id}'. Expected: provider/model-name")
