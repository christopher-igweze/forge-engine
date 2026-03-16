"""FORGE configuration and per-role model routing.

Resolution order: FORGE_DEFAULT_MODELS < models.default < models.<role>
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge.schemas import ForgeMode


# ── Role-to-field mapping ─────────────────────────────────────────────

FORGE_ROLE_TO_MODEL_FIELD: dict[str, str] = {
    "codebase_analyst": "codebase_analyst_model",
    "security_auditor": "security_auditor_model",
    "quality_auditor": "quality_auditor_model",
    "architecture_reviewer": "architecture_reviewer_model",
    "fix_strategist": "fix_strategist_model",
    "triage_classifier": "triage_classifier_model",
    "coder_tier2": "coder_tier2_model",
    "coder_tier3": "coder_tier3_model",
    "coder_fallback": "coder_fallback_model",
    "test_generator": "test_generator_model",
    "code_reviewer": "code_reviewer_model",
    "integration_validator": "integration_validator_model",
    "debt_tracker": "debt_tracker_model",
    # Hive Discovery roles
    "swarm_worker": "swarm_worker_model",
    "synthesizer": "synthesizer_model",
    # Post-discovery intent analysis
    "intent_analyzer": "intent_analyzer_model",
}

# ── Default model assignments per spec ────────────────────────────────

FORGE_DEFAULT_MODELS: dict[str, str] = {
    # All agents use MiniMax M2.5 — 80.2% SWE-bench, $0.30/$1.20 per MTok
    # Analysis agents
    "codebase_analyst_model": "minimax/minimax-m2.5",
    "quality_auditor_model": "minimax/minimax-m2.5",
    "debt_tracker_model": "anthropic/claude-haiku-4.5",
    # Reasoning agents — mid-tier
    "security_auditor_model": "anthropic/claude-haiku-4.5",
    "architecture_reviewer_model": "anthropic/claude-haiku-4.5",
    "fix_strategist_model": "anthropic/claude-haiku-4.5",
    "triage_classifier_model": "anthropic/claude-haiku-4.5",
    "test_generator_model": "anthropic/claude-haiku-4.5",
    "code_reviewer_model": "anthropic/claude-haiku-4.5",
    "integration_validator_model": "anthropic/claude-haiku-4.5",
    # Coding agents — NON-NEGOTIABLE frontier model
    "coder_tier2_model": "anthropic/claude-sonnet-4.6",
    "coder_tier3_model": "anthropic/claude-sonnet-4.6",
    # Fallback: escalate to Kimi K2.5 after 2 failed attempts
    "coder_fallback_model": "moonshotai/kimi-k2.5",
    # Hive Discovery — cheap workers + strong synthesizer
    "swarm_worker_model": "minimax/minimax-m2.5",
    "synthesizer_model": "minimax/minimax-m2.5",
    # Post-discovery intent analysis
    "intent_analyzer_model": "minimax/minimax-m2.5",
}

# ── Provider routing ──────────────────────────────────────────────────

# Analysis/planning agents use openrouter_direct (text-in/JSON-out, no tools)
# Coding agents use openrouter_tools (native function calling via OpenRouter API)

ROLE_TO_PROVIDER: dict[str, str] = {
    "codebase_analyst": "openrouter_direct",
    "security_auditor": "openrouter_direct",
    "quality_auditor": "openrouter_direct",
    "architecture_reviewer": "openrouter_direct",
    "fix_strategist": "openrouter_direct",
    "triage_classifier": "openrouter_direct",
    "coder_tier2": "opencode",
    "coder_tier3": "opencode",
    "coder_fallback": "openrouter_tools",
    "test_generator": "openrouter_direct",
    "code_reviewer": "openrouter_direct",
    "integration_validator": "openrouter_tools",
    "debt_tracker": "openrouter_direct",
    # Hive Discovery
    "swarm_worker": "openrouter_direct",
    "synthesizer": "openrouter_direct",
    # Post-discovery
    "intent_analyzer": "openrouter_direct",
}


class ForgeConfig(BaseModel):
    """Configuration for a FORGE run.

    Mirrors SWE-AF's BuildConfig pattern with FORGE-specific defaults.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["open_code"] = "open_code"
    models: dict[str, str] | None = None  # role -> model overrides

    mode: ForgeMode = ForgeMode.FULL
    max_inner_retries: int = 3  # Inner loop: coder retry on REQUEST_CHANGES
    max_middle_escalations: int = 2  # Middle loop: escalation attempts
    max_outer_replans: int = 1  # Outer loop: re-run Fix Strategist
    agent_timeout_seconds: int = 900  # 15 min per agent

    enable_tier0_autofix: bool = True
    enable_tier1_rules: bool = True
    enable_parallel_audit: bool = True  # Run audit passes concurrently
    enable_learning: bool = True  # Log training data for fine-tuning flywheel

    repo_url: str = ""
    repo_path: str = ""
    enable_github_pr: bool = True
    github_pr_base: str = "main"

    dry_run: bool = False  # scan only, no fixes
    skip_tiers: list[int] = []  # e.g. [0] to process even invalid findings
    focus_categories: list[str] = []  # e.g. ["security"] to only fix security

    # ── Budget / Circuit Breakers ────────────────────────────────────
    max_cost_usd: float = 0.0            # 0 = no limit (user's own API key, their cost)
    max_duration_seconds: float = 0.0     # 0 = no limit
    cost_warning_threshold: float = 0.8  # Log warning at 80% of budget

    # ── Hive Discovery (swarm architecture) ─────────────────────────
    discovery_mode: Literal["classic", "swarm"] = "classic"
    swarm_target_segments: int = 5  # Target number of segments for community detection
    swarm_enable_wave2: bool = True  # Enable Wave 2 (MoA re-analysis)
    swarm_worker_types: list[str] = ["security", "quality", "architecture"]

    # ── Vulnerability Pattern Library ────────────────────────────
    pattern_library_path: str = ""  # Custom path; empty = use built-in library

    # ── Project Context ───────────────────────────────────────────
    project_context: dict = {}  # User-provided project context for scan personalization

    # ── Webhook Event Emission ────────────────────────────────────
    webhook_url: str = ""       # POST endpoint for scan progress events
    webhook_token: str = ""     # HMAC-SHA256 signing secret
    webhook_scan_id: str = ""   # Scan ID included in every event payload

    # ── Regression Check ─────────────────────────────────────────────
    enable_regression_check: bool = True
    regression_test_timeout: int = 180  # seconds for full suite run

    # ── SWE-AF Integration (all AI remediation) ────────────────────────
    sweaf_agentfield_url: str = ""
    sweaf_api_key: str = ""
    sweaf_node_id: str = "swe-planner"
    sweaf_max_coding_iterations: int = 3
    sweaf_max_concurrent_issues: int = 3
    sweaf_runtime: Literal["claude_code", "open_code", "api"] = "api"
    sweaf_timeout_seconds: int = 3600  # 60 min — API runtime is slower than subprocess
    sweaf_fallback_to_forge: bool = True
    sweaf_max_cost_usd: float = 10.0

    # ── Convergence Loop ─────────────────────────────────────────────
    convergence_enabled: bool = True
    convergence_target_score: int = 95
    max_convergence_iterations: int = 3
    convergence_min_improvement: int = 5  # stop if score improves < 5 pts
    convergence_escalate_dropped: bool = True  # re-inject dropped findings

    def resolved_models(self) -> dict[str, str]:
        """Resolve model fields using the standard cascade.

        Resolution: FORGE_DEFAULT_MODELS < models.default < models.<role>
        """
        resolved = dict(FORGE_DEFAULT_MODELS)
        overrides = self.models or {}

        # Apply default override to all fields
        default_model = overrides.get("default")
        if default_model:
            for field in resolved:
                resolved[field] = default_model

        # Apply per-role overrides
        for role, model_id in overrides.items():
            if role == "default":
                continue
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
