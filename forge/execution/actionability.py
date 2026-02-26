"""Post-processing actionability classifier for audit findings.

Classifies each finding into one of four actionability tiers based on
severity, confidence, and project context. This runs after discovery
as a deterministic post-processing step — no LLM cost.

Tiers:
  must_fix       — Exploitable now, fix before shipping
  should_fix     — Real issue, prioritize this sprint
  consider       — Valid observation, may not be urgent at current stage
  informational  — Noted for awareness, not actionable now
"""

from __future__ import annotations


def classify_actionability(
    finding: dict,
    project_context: dict | None = None,
) -> str:
    """Classify a finding's actionability tier.

    If the LLM already set an actionability value, we respect it unless
    project context overrides it (e.g., known compromise → informational).

    Args:
        finding: A finding dict with at minimum "severity" and "confidence".
        project_context: Optional user-provided project context dict.

    Returns:
        One of: "must_fix", "should_fix", "consider", "informational"
    """
    ctx = project_context or {}
    severity = finding.get("severity", "low")
    confidence = finding.get("confidence", 0.0)
    category = finding.get("category", "")
    known_compromises = ctx.get("known_compromises", [])
    stage = ctx.get("project_stage", "")

    # Check if finding overlaps with a known compromise
    if known_compromises:
        description = finding.get("description", "").lower()
        title = finding.get("title", "").lower()
        for comp in known_compromises:
            comp_lower = comp.lower()
            if comp_lower in description or comp_lower in title:
                return "informational"

    # Critical + high confidence → must_fix always
    if severity == "critical" and confidence >= 0.85:
        return "must_fix"

    # High severity
    if severity == "high" and confidence >= 0.8:
        if stage in ("growth", "enterprise"):
            return "must_fix"
        return "should_fix"

    # Critical/high/medium with decent confidence → should_fix
    if severity in ("critical", "high", "medium") and confidence >= 0.7:
        return "should_fix"

    # Architecture findings in early stages are usually noise
    if category == "architecture" and stage in ("mvp", "early_product"):
        return "informational"

    # Medium severity in early stages → consider
    if severity == "medium" and stage in ("mvp", "early_product"):
        return "consider"

    # Low severity
    if severity == "low":
        return "informational"

    return "consider"


def apply_actionability(
    findings: list[dict],
    project_context: dict | None = None,
    override_llm: bool = False,
) -> list[dict]:
    """Apply actionability classification to a list of findings.

    Args:
        findings: List of finding dicts.
        project_context: Optional user-provided project context.
        override_llm: If True, overwrite LLM-assigned actionability.
            If False (default), only fill in empty actionability fields
            UNLESS project context causes a downgrade (e.g., known compromise).

    Returns:
        The same list with actionability fields populated.
    """
    ctx = project_context or {}

    for finding in findings:
        existing = finding.get("actionability", "")
        classified = classify_actionability(finding, ctx)

        if override_llm or not existing:
            finding["actionability"] = classified
        elif classified == "informational" and _matches_known_compromise(finding, ctx):
            # Known compromise forces downgrade regardless of LLM classification
            finding["actionability"] = "informational"

    return findings


def _matches_known_compromise(finding: dict, ctx: dict) -> bool:
    """Check if finding overlaps with a user's known compromise."""
    compromises = ctx.get("known_compromises", [])
    if not compromises:
        return False
    description = finding.get("description", "").lower()
    title = finding.get("title", "").lower()
    return any(
        c.lower() in description or c.lower() in title
        for c in compromises
    )
