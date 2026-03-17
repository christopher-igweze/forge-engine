"""Estimate production readiness score from discovery findings."""
from __future__ import annotations

# Per-finding severity deductions
SEVERITY_DEDUCTIONS = {
    "critical": -15,
    "high": -8,
    "medium": -3,
    "low": -1,
}

# Max total deduction per category to prevent one category tanking the score
MAX_CATEGORY_DEDUCTION = -25


def estimate_readiness_score(findings: list[dict]) -> int:
    """Estimate a production readiness score (0-100) from discovery findings.

    This is a rough estimate for discovery-only mode. The full pipeline
    computes a more accurate score after remediation and validation.

    Algorithm:
    - Start at 100
    - Deduct per finding based on severity
    - Cap deductions per category at MAX_CATEGORY_DEDUCTION
    - Clamp result to 0-100
    """
    category_deductions: dict[str, int] = {}

    for finding in findings:
        severity = finding.get("severity", "medium")
        category = finding.get("category", "uncategorized")
        deduction = SEVERITY_DEDUCTIONS.get(severity, -1)

        current = category_deductions.get(category, 0)
        # Apply category cap
        new_total = max(MAX_CATEGORY_DEDUCTION, current + deduction)
        category_deductions[category] = new_total

    total_deduction = sum(category_deductions.values())
    score = max(0, min(100, 100 + total_deduction))

    return score


def readiness_breakdown(findings: list[dict]) -> dict:
    """Get per-category readiness breakdown."""
    categories: dict[str, dict] = {}

    for finding in findings:
        category = finding.get("category", "uncategorized")
        severity = finding.get("severity", "medium")

        if category not in categories:
            categories[category] = {"findings": 0, "critical": 0, "high": 0, "medium": 0, "low": 0}
        categories[category]["findings"] += 1
        categories[category][severity] = categories[category].get(severity, 0) + 1

    return {
        "overall_score": estimate_readiness_score(findings),
        "categories": categories,
        "total_findings": len(findings),
    }
