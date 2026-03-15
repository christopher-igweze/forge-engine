"""Tier-based dispatch router for FORGE remediation.

Routes findings to the appropriate fix mechanism based on their
assigned tier from the Triage Classifier (Agent 6):

  Tier 0: Auto-skip (invalid / false-positive)
  Tier 1: Deterministic patch (no LLM, uses tier1/ rules engine)
  Tier 2 + Tier 3: ALL AI items routed to SWE-AF DAG executor
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

# Re-export Tier 1 handlers and helpers for backward compatibility.
# Tests and golden suite import private functions from this module.
from forge.execution.tier1_handlers import (  # noqa: F401
    TIER1_TEMPLATES,
    _find_template,
    _run_tier1_fix,
    _tier1_add_error_boundary,
    _tier1_add_rate_limiter,
    _tier1_create_env_example,
    _tier1_replace_secret,
)
from forge.execution.tier1_helpers import (  # noqa: F401
    _ERROR_BOUNDARY_JSX,
    _ERROR_BOUNDARY_TSX,
    _add_express_rate_limiter,
    _add_fastapi_rate_limiter,
    _add_flask_rate_limiter,
    _detect_framework,
    _find_react_src,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig
    from forge.schemas import ForgeExecutionState

logger = logging.getLogger(__name__)


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
    """Split plan items into deterministic and AI (SWE-AF) buckets.

    Tier 0 and Tier 1 are handled synchronously before the async
    SWE-AF executor runs.

    Returns:
        (handled_items, sweaf_items) — items resolved immediately via
        deterministic fixes, and ALL AI items (Tier 2 + Tier 3) for
        SWE-AF dispatch.
    """
    finding_map: dict[str, AuditFinding] = {f.id: f for f in findings}
    handled: list[RemediationItem] = []
    sweaf_items: list[RemediationItem] = []

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
                logger.info("Tier 1 rules disabled — promoting %s to SWE-AF", finding.id)
                item.tier = RemediationTier.TIER_2
                sweaf_items.append(item)
                continue

            result = apply_tier1(finding, item, repo_path)
            if result.outcome in (FixOutcome.COMPLETED, FixOutcome.SKIPPED):
                state.completed_fixes.append(result)
                handled.append(item)
            else:
                # Failed Tier 1 → promote to SWE-AF
                logger.info("Tier 1 failed for %s — promoting to SWE-AF", finding.id)
                item.tier = RemediationTier.TIER_2
                sweaf_items.append(item)

        elif item.tier in (RemediationTier.TIER_2, RemediationTier.TIER_3):
            sweaf_items.append(item)

        else:
            logger.warning("Unknown tier %s for %s — routing to SWE-AF", item.tier, finding.id)
            item.tier = RemediationTier.TIER_2
            sweaf_items.append(item)

    logger.info(
        "Tier router: %d handled (Tier 0/1), %d AI items → SWE-AF",
        len(handled), len(sweaf_items),
    )
    return handled, sweaf_items
