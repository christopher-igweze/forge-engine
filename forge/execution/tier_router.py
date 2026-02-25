"""Tier-based dispatch router for FORGE remediation.

Routes findings to the appropriate fix mechanism based on their
assigned tier from the Triage Classifier (Agent 6):

  Tier 0: Auto-skip (invalid / false-positive)
  Tier 1: Deterministic patch (no LLM, uses tier1/ rules engine)
  Tier 2: Scoped AI fix (1-3 files, routed to inner loop with Tier 2 coder)
  Tier 3: Architectural AI fix (5-15 files, routed to inner loop with Tier 3 coder)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig
    from forge.schemas import ForgeExecutionState, TriageResult

logger = logging.getLogger(__name__)


# ── Tier 1 Fix Templates ────────────────────────────────────────────

# Mapping from template ID → deterministic fix function.
# These are simple, validated patches that don't need an LLM.
# Phase 2 ships a small set; Phase 3 expands via a plugin registry.

TIER1_TEMPLATES: dict[str, str] = {
    "replace-hardcoded-secret": "tier1_replace_secret",
    "add-rate-limiter": "tier1_add_rate_limiter",
    "create-env-example": "tier1_create_env_example",
    "add-error-boundary": "tier1_add_error_boundary",
}


def apply_tier0(
    finding: AuditFinding,
    item: RemediationItem,
) -> CoderFixResult:
    """Handle Tier 0 findings — auto-skip with log."""
    logger.info("Tier 0: auto-skipping %s — %s", finding.id, finding.title)
    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.SKIPPED,
        summary=f"Auto-skipped: {finding.title} (Tier 0 — invalid/false-positive)",
    )


def apply_tier1(
    finding: AuditFinding,
    item: RemediationItem,
    repo_path: str,
) -> CoderFixResult:
    """Handle Tier 1 findings — deterministic fix from template.

    For Phase 2, this dispatches to simple rule-based fixers.
    Phase 3 will add a full template engine with AST manipulation.
    """
    template_id = _find_template(finding, item)

    if not template_id:
        logger.warning(
            "Tier 1: no template found for %s — deferring to Tier 2",
            finding.id,
        )
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"No Tier 1 template matched for: {finding.title}",
            error_message="No matching template — will be escalated to Tier 2",
        )

    handler_name = TIER1_TEMPLATES.get(template_id, "")
    logger.info(
        "Tier 1: applying template %s (%s) for %s",
        template_id, handler_name, finding.id,
    )

    try:
        result = _run_tier1_fix(template_id, finding, repo_path)
        return result
    except Exception as e:
        logger.error("Tier 1 fix failed for %s: %s", finding.id, e)
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Tier 1 template {template_id} failed",
            error_message=str(e),
        )


def route_plan_items(
    plan: RemediationPlan,
    findings: list[AuditFinding],
    state: ForgeExecutionState,
    repo_path: str,
    cfg: ForgeConfig,
) -> tuple[list[RemediationItem], list[RemediationItem]]:
    """Split plan items into deterministic (Tier 0-1) and AI (Tier 2-3).

    Tier 0 and Tier 1 are handled synchronously before the async
    inner/middle/outer loops run.

    Returns:
        (handled_items, ai_items) — items that were resolved immediately
        vs items that need the full coder pipeline.
    """
    finding_map: dict[str, AuditFinding] = {f.id: f for f in findings}
    handled: list[RemediationItem] = []
    ai_items: list[RemediationItem] = []

    for item in plan.items:
        finding = finding_map.get(item.finding_id)
        if not finding:
            logger.warning("Finding %s not found — skipping", item.finding_id)
            continue

        if item.tier == RemediationTier.TIER_0:
            result = apply_tier0(finding, item)
            state.completed_fixes.append(result)
            handled.append(item)

        elif item.tier == RemediationTier.TIER_1:
            if not cfg.enable_tier1_rules:
                logger.info("Tier 1 rules disabled — promoting %s to Tier 2", finding.id)
                item.tier = RemediationTier.TIER_2
                ai_items.append(item)
                continue

            result = apply_tier1(finding, item, repo_path)
            if result.outcome in (FixOutcome.COMPLETED, FixOutcome.SKIPPED):
                state.completed_fixes.append(result)
                handled.append(item)
            else:
                # Failed Tier 1 → promote to Tier 2
                logger.info("Tier 1 failed for %s — promoting to Tier 2", finding.id)
                item.tier = RemediationTier.TIER_2
                ai_items.append(item)

        elif item.tier in (RemediationTier.TIER_2, RemediationTier.TIER_3):
            ai_items.append(item)

        else:
            logger.warning("Unknown tier %s for %s — treating as Tier 2", item.tier, finding.id)
            item.tier = RemediationTier.TIER_2
            ai_items.append(item)

    logger.info(
        "Tier router: %d handled (Tier 0/1), %d need AI (Tier 2/3)",
        len(handled), len(ai_items),
    )
    return handled, ai_items


# ── Tier 1 Internal Fix Handlers ─────────────────────────────────────


def _find_template(finding: AuditFinding, item: RemediationItem) -> str:
    """Match a finding to a Tier 1 fix template."""
    # Check if triage classifier already assigned a template
    if hasattr(item, "fix_template_id") and getattr(item, "fix_template_id", ""):
        # RemediationItem doesn't have fix_template_id, but the triage
        # decision did. We check if the approach mentions a template.
        pass

    # Match by finding content keywords
    text = f"{finding.title} {finding.description} {finding.suggested_fix}".lower()

    from forge.prompts.triage_classifier import TIER_1_PATTERNS

    for pattern_info in TIER_1_PATTERNS:
        keywords = pattern_info.get("keywords", [])
        if any(kw.lower() in text for kw in keywords):
            return pattern_info.get("template_id", "")

    return ""


def _run_tier1_fix(
    template_id: str,
    finding: AuditFinding,
    repo_path: str,
) -> CoderFixResult:
    """Execute a Tier 1 deterministic fix.

    Phase 2: Stub implementations that log the action.
    Phase 3: Full AST-aware templates with rollback.
    """
    import os

    if template_id == "replace-hardcoded-secret":
        return _tier1_replace_secret(finding, repo_path)
    elif template_id == "add-rate-limiter":
        return _tier1_add_rate_limiter(finding, repo_path)
    elif template_id == "create-env-example":
        return _tier1_create_env_example(finding, repo_path)
    elif template_id == "add-error-boundary":
        return _tier1_add_error_boundary(finding, repo_path)
    else:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Unknown template: {template_id}",
            error_message=f"No handler for template_id={template_id}",
        )


def _tier1_replace_secret(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Replace hardcoded secrets with environment variable references.

    Phase 2: Identifies the secret location and replaces with env var.
    """
    import os
    import re

    files_changed = []

    for loc in finding.locations:
        file_path = os.path.join(repo_path, loc.file_path)
        if not os.path.isfile(file_path):
            continue

        try:
            with open(file_path, "r") as f:
                content = f.read()

            # Simple pattern: replace hardcoded string assignments
            # This is intentionally conservative — only replaces obvious patterns
            original = content
            # Replace patterns like: API_KEY = "hardcoded_value"
            content = re.sub(
                r'([A-Z_]*(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)[A-Z_]*)\s*=\s*["\']([^"\']{8,})["\']',
                r'\1 = os.environ.get("\1", "")',
                content,
            )

            if content != original:
                # Ensure os import exists
                if "import os" not in content:
                    content = "import os\n" + content
                with open(file_path, "w") as f:
                    f.write(content)
                files_changed.append(loc.file_path)

        except Exception as e:
            logger.error("Tier 1 secret replacement failed in %s: %s", loc.file_path, e)

    if files_changed:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=files_changed,
            summary=f"Replaced hardcoded secrets with env vars in {len(files_changed)} file(s)",
        )

    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary="Could not find replaceable secrets in referenced files",
        error_message="No matching patterns found",
    )


def _tier1_add_rate_limiter(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Add rate limiting to an API route.

    Phase 2: Logs the action — actual implementation needs framework detection.
    Phase 3: Full framework-aware rate limiter injection (Express, FastAPI, etc).
    """
    logger.info("Tier 1 rate limiter: %s (stub — full impl in Phase 3)", finding.id)
    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary="Rate limiter template requires framework detection (Phase 3)",
        error_message="Promoting to Tier 2 for AI-assisted fix",
    )


def _tier1_create_env_example(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Create a .env.example file from existing .env.

    Strips values, keeps keys with placeholder comments.
    """
    import os

    env_path = os.path.join(repo_path, ".env")
    example_path = os.path.join(repo_path, ".env.example")

    if not os.path.isfile(env_path):
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary="No .env file found to create example from",
        )

    if os.path.isfile(example_path):
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.SKIPPED,
            summary=".env.example already exists",
        )

    try:
        lines = []
        with open(env_path, "r") as f:
            for line in f:
                line = line.rstrip()
                if not line or line.startswith("#"):
                    lines.append(line)
                    continue
                if "=" in line:
                    key = line.split("=", 1)[0].strip()
                    lines.append(f"{key}=")
                else:
                    lines.append(line)

        with open(example_path, "w") as f:
            f.write("# Environment variables — copy to .env and fill in values\n")
            f.write("\n".join(lines) + "\n")

        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=[".env.example"],
            summary="Created .env.example from .env (values stripped)",
        )
    except Exception as e:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Failed to create .env.example: {e}",
            error_message=str(e),
        )


def _tier1_add_error_boundary(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Add React ErrorBoundary wrapper.

    Phase 2: Logs the action — actual implementation needs JSX/TSX parsing.
    Phase 3: Full AST-aware ErrorBoundary injection.
    """
    logger.info("Tier 1 error boundary: %s (stub — full impl in Phase 3)", finding.id)
    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary="Error boundary template requires JSX parsing (Phase 3)",
        error_message="Promoting to Tier 2 for AI-assisted fix",
    )
