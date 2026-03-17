"""Post-discovery severity calibration.

Applies risk-based severity adjustments after discovery agents complete.
Ensures architectural opinions don't inflate severity and OWASP Top 10
issues get appropriate attention.
"""
from __future__ import annotations

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITY_REVERSE = {v: k for k, v in SEVERITY_ORDER.items()}

# Architecture findings capped at MEDIUM — they're structural opinions,
# not exploitable vulnerabilities.
SEVERITY_CAPS: dict[str, str] = {
    "architecture": "medium",
}

# OWASP Top 10 2021 CWE IDs that warrant minimum HIGH severity
OWASP_TOP_10_CWES: frozenset[str] = frozenset({
    # A01: Broken Access Control
    "CWE-200", "CWE-201", "CWE-352", "CWE-639", "CWE-862", "CWE-863",
    # A02: Cryptographic Failures
    "CWE-259", "CWE-261", "CWE-312", "CWE-326", "CWE-327", "CWE-328",
    # A03: Injection
    "CWE-20", "CWE-74", "CWE-75", "CWE-77", "CWE-78", "CWE-79", "CWE-89",
    "CWE-94", "CWE-917",
    # A04: Insecure Design
    "CWE-209", "CWE-256", "CWE-501", "CWE-522",
    # A05: Security Misconfiguration
    "CWE-2", "CWE-11", "CWE-13", "CWE-15", "CWE-16", "CWE-260",
    # A06: Vulnerable Components — handled by dependency scanners
    # A07: Auth Failures
    "CWE-287", "CWE-297", "CWE-384", "CWE-613", "CWE-620", "CWE-640",
    # A08: Software/Data Integrity
    "CWE-345", "CWE-502", "CWE-565", "CWE-784", "CWE-829",
    # A09: Logging Failures
    "CWE-117", "CWE-223", "CWE-532", "CWE-778",
    # A10: SSRF
    "CWE-918",
})

# Confidence threshold below which severity is downgraded
CONFIDENCE_DOWNGRADE_THRESHOLD = 0.6


def calibrate_severity(finding: dict) -> str:
    """Apply risk-based severity adjustments to a finding.

    Rules applied in order:
    1. Cap architecture findings at MEDIUM
    2. Boost OWASP Top 10 CWEs to minimum HIGH
    3. Downgrade findings with low confidence scores

    Returns the adjusted severity string.
    """
    severity = finding.get("severity", "medium")
    if severity not in SEVERITY_ORDER:
        severity = "medium"

    category = finding.get("category", "")

    # Rule 1: Cap by category
    if category in SEVERITY_CAPS:
        cap = SEVERITY_CAPS[category]
        if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(cap, 0):
            severity = cap

    # Rule 2: Boost OWASP Top 10
    cwe_id = finding.get("cwe_id", "")
    if cwe_id and cwe_id in OWASP_TOP_10_CWES:
        if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get("high", 0):
            severity = "high"

    # Rule 3: Downgrade low-confidence findings
    confidence = finding.get("confidence", 1.0)
    if isinstance(confidence, (int, float)) and confidence < CONFIDENCE_DOWNGRADE_THRESHOLD:
        current = SEVERITY_ORDER.get(severity, 0)
        if current > 0:
            severity = SEVERITY_REVERSE[current - 1]

    return severity


def calibrate_findings(findings: list[dict]) -> list[dict]:
    """Apply severity calibration to all findings in-place.

    Modifies each finding's severity and adds original_severity field.
    Returns the same list for chaining.
    """
    for finding in findings:
        original = finding.get("severity", "medium")
        calibrated = calibrate_severity(finding)
        if calibrated != original:
            finding["original_severity"] = original
            finding["severity"] = calibrated
    return findings
