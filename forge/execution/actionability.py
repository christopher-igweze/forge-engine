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

from typing import Any


def _get_field(finding: Any, key: str, default: Any = "") -> Any:
    """Get a field from a finding dict or Pydantic model."""
    if isinstance(finding, dict):
        return finding.get(key, default)
    val = getattr(finding, key, default)
    # Pydantic enums: extract .value for string comparison
    if hasattr(val, "value"):
        return val.value
    return val


def _set_field(finding: Any, key: str, value: Any) -> None:
    """Set a field on a finding dict or Pydantic model."""
    if isinstance(finding, dict):
        finding[key] = value
    else:
        setattr(finding, key, value)


def classify_actionability(
    finding: Any,
    project_context: dict | None = None,
) -> str:
    """Classify a finding's actionability tier.

    If the LLM already set an actionability value, we respect it unless
    project context overrides it (e.g., known compromise → informational).

    Args:
        finding: A finding dict or AuditFinding with at minimum
            "severity" and "confidence".
        project_context: Optional user-provided project context dict.

    Returns:
        One of: "must_fix", "should_fix", "consider", "informational"
    """
    ctx = project_context or {}
    severity = _get_field(finding, "severity", "low")
    confidence = _get_field(finding, "confidence", 0.0)
    category = _get_field(finding, "category", "")
    known_compromises = ctx.get("known_compromises", [])
    stage = ctx.get("project_stage", "")

    # Check if finding overlaps with a known compromise
    if known_compromises:
        description = _get_field(finding, "description", "").lower()
        title = _get_field(finding, "title", "").lower()
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
    findings: list,
    project_context: dict | None = None,
    override_llm: bool = False,
) -> list:
    """Apply actionability classification to a list of findings.

    Args:
        findings: List of finding dicts or AuditFinding objects.
        project_context: Optional user-provided project context.
        override_llm: If True, overwrite LLM-assigned actionability.
            If False (default), only fill in empty actionability fields
            UNLESS project context causes a downgrade (e.g., known compromise).

    Returns:
        The same list with actionability fields populated.
    """
    ctx = project_context or {}

    for finding in findings:
        existing = _get_field(finding, "actionability", "")
        classified = classify_actionability(finding, ctx)

        if override_llm or not existing:
            _set_field(finding, "actionability", classified)
        elif classified == "informational" and _matches_known_compromise(finding, ctx):
            # Known compromise forces downgrade regardless of LLM classification
            _set_field(finding, "actionability", "informational")

    return findings


def _matches_known_compromise(finding: Any, ctx: dict) -> bool:
    """Check if finding overlaps with a user's known compromise."""
    compromises = ctx.get("known_compromises", [])
    if not compromises:
        return False
    description = _get_field(finding, "description", "").lower()
    title = _get_field(finding, "title", "").lower()
    return any(
        c.lower() in description or c.lower() in title
        for c in compromises
    )
