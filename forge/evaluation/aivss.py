"""OWASP AIVSS (AI Vulnerability Scoring System) implementation.

Scores agentic AI projects on a 0-10 scale using three components:
- CVSS Base metrics (traditional vulnerability scoring)
- AARS (Agentic AI Risk Score) — 10 amplification factors
- Impact metrics (C/I/A/Safety)

Reference: https://aivss.owasp.org/
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Base Metric Values ────────────────────────────────────────────
ATTACK_VECTOR = {"network": 0.85, "adjacent": 0.62, "local": 0.55, "physical": 0.20}
ATTACK_COMPLEXITY = {"low": 0.77, "high": 0.44}
PRIVILEGES_REQUIRED = {"none": 0.85, "low": 0.62, "high": 0.27}
USER_INTERACTION = {"none": 0.85, "required": 0.62}
SCOPE = {"unchanged": 1.00, "changed": 1.50}

# ── AI-Specific Metric Values ────────────────────────────────────
# Each 0.2 (critical) to 1.0 (no vuln)
AI_METRIC_RANGE = {"none": 1.0, "low": 0.8, "medium": 0.5, "high": 0.3, "critical": 0.2}

# ── Severity Bands ────────────────────────────────────────────────
SEVERITY_BANDS = [
    (9.0, "Critical"),
    (7.0, "High"),
    (4.0, "Medium"),
    (0.1, "Low"),
    (0.0, "None"),
]

# ── AARS Factor Names ────────────────────────────────────────────
AARS_FACTORS = [
    "execution_autonomy",
    "tool_control_surface",
    "natural_language_interface",
    "contextual_awareness",
    "behavioral_non_determinism",
    "opacity_reflexivity",
    "persistent_state",
    "dynamic_identity",
    "multi_agent_interactions",
    "self_modification",
]

AARS_LABELS = {
    "execution_autonomy": {0.0: "Human approves all", 0.5: "Human-in-the-loop", 1.0: "Fully autonomous"},
    "tool_control_surface": {0.0: "No external tools", 0.5: "Limited/sandboxed", 1.0: "Unrestricted tools"},
    "natural_language_interface": {0.0: "No NL input", 0.5: "Validated NL input", 1.0: "Raw NL, no sanitization"},
    "contextual_awareness": {0.0: "No env context", 0.5: "Limited context", 1.0: "Full env access"},
    "behavioral_non_determinism": {0.0: "Deterministic", 0.5: "Mostly deterministic", 1.0: "Highly non-deterministic"},
    "opacity_reflexivity": {0.0: "Full reasoning trace", 0.5: "Partial logging", 1.0: "Black box"},
    "persistent_state": {0.0: "Stateless", 0.5: "Session-only", 1.0: "Cross-session persistent"},
    "dynamic_identity": {0.0: "Fixed identity", 0.5: "Role-based", 1.0: "Arbitrary identities"},
    "multi_agent_interactions": {0.0: "Single agent", 0.5: "Supervised multi-agent", 1.0: "Unsupervised multi-agent"},
    "self_modification": {0.0: "No self-modification", 0.5: "Config self-tuning", 1.0: "Code/prompt modification"},
}


@dataclass
class AIVSSInput:
    """All input parameters for AIVSS scoring."""
    # Base metrics (use string keys from the dicts above, or float values directly)
    attack_vector: float = 0.85       # Default: network
    attack_complexity: float = 0.77   # Default: low
    privileges_required: float = 0.85  # Default: none
    user_interaction: float = 0.85    # Default: none
    scope: float = 1.00              # Default: unchanged

    # AI-specific metrics (1.0 = no vuln, 0.2 = critical)
    model_robustness: float = 0.8
    data_sensitivity: float = 0.8
    ethical_impact: float = 0.8
    decision_criticality: float = 0.8
    adaptability: float = 0.8

    # AARS factors (each 0.0, 0.5, or 1.0)
    execution_autonomy: float = 0.0
    tool_control_surface: float = 0.0
    natural_language_interface: float = 0.0
    contextual_awareness: float = 0.0
    behavioral_non_determinism: float = 0.0
    opacity_reflexivity: float = 0.0
    persistent_state: float = 0.0
    dynamic_identity: float = 0.0
    multi_agent_interactions: float = 0.0
    self_modification: float = 0.0

    # Impact metrics (each 0.0 to 1.0)
    confidentiality_impact: float = 0.5
    integrity_impact: float = 0.5
    availability_impact: float = 0.3
    safety_impact: float = 0.0

    # Threat multiplier
    threat_multiplier: float = 1.0


@dataclass
class AIVSSResult:
    """Scored AIVSS result."""
    score: float                    # 0-10 final score
    severity: str                   # None/Low/Medium/High/Critical
    base_score: float               # CVSS base component
    ai_metrics_score: float         # AI-specific metrics component
    aars_score: float               # AARS normalized to 0-10
    impact_score: float             # Impact component (0-10)
    formula_used: str               # "weighted" or "simple"
    factor_breakdown: dict = field(default_factory=dict)  # factor_name -> {value, label}


def _severity_from_score(score: float) -> str:
    """Map score to severity band."""
    for threshold, label in SEVERITY_BANDS:
        if score >= threshold:
            return label
    return "None"


def calculate_aivss(input: AIVSSInput, formula: str = "weighted") -> AIVSSResult:
    """Calculate AIVSS score.

    Two formulas available:
    - "weighted": AIVSS = (0.25 x Base) + (0.45 x AI_Normalized) + (0.30 x Impact)
    - "simple": AIVSS = ((CVSS_Base + AARS) / 2) x Threat_Multiplier

    For non-agentic codebases (all AARS = 0), the score reflects only base + impact.
    """
    # 1. Base score
    base_raw = (
        input.attack_vector
        * input.attack_complexity
        * input.privileges_required
        * input.user_interaction
        * input.scope
    )
    base_score = min(10.0, base_raw)

    # 2. AI-specific metrics
    ai_raw = (
        input.model_robustness
        * input.data_sensitivity
        * input.ethical_impact
        * input.decision_criticality
        * input.adaptability
    )
    # Invert: lower product = higher risk. Normalize to 0-10 where 10 = highest risk.
    ai_metrics_score = (1.0 - ai_raw) * 10.0

    # 3. AARS score
    aars_values = [getattr(input, factor) for factor in AARS_FACTORS]
    aars_sum = sum(aars_values)
    aars_score = (aars_sum / len(AARS_FACTORS)) * 10.0  # Normalized to 0-10

    # 4. Impact score
    impact_avg = (
        input.confidentiality_impact
        + input.integrity_impact
        + input.availability_impact
        + input.safety_impact
    ) / 4.0
    impact_score = impact_avg * 10.0  # Normalized to 0-10

    # 5. Final score
    if formula == "weighted":
        # Combine AI metrics + AARS into a single AI_Normalized
        ai_normalized = (ai_metrics_score + aars_score) / 2.0
        score = (0.25 * base_score) + (0.45 * ai_normalized) + (0.30 * impact_score)
    else:  # simple
        score = ((base_score + aars_score) / 2.0) * input.threat_multiplier

    score = max(0.0, min(10.0, round(score, 1)))

    # Build factor breakdown
    factor_breakdown = {}
    for factor in AARS_FACTORS:
        val = getattr(input, factor)
        labels = AARS_LABELS.get(factor, {})
        label = labels.get(val, f"Score: {val}")
        factor_breakdown[factor] = {"value": val, "label": label}

    return AIVSSResult(
        score=score,
        severity=_severity_from_score(score),
        base_score=round(base_score, 2),
        ai_metrics_score=round(ai_metrics_score, 2),
        aars_score=round(aars_score, 2),
        impact_score=round(impact_score, 2),
        formula_used=formula,
        factor_breakdown=factor_breakdown,
    )


def build_aivss_input(
    aars_factors: dict[str, float], findings: list | None = None
) -> AIVSSInput:
    """Build AIVSSInput from detected AARS factors and optional findings.

    Base metrics are set to reasonable defaults for web applications.
    AI metrics are estimated from AARS factors.
    Impact metrics are estimated from findings if available.
    """
    input_kwargs: dict = {}

    # Set AARS factors from detection
    for factor in AARS_FACTORS:
        if factor in aars_factors:
            input_kwargs[factor] = aars_factors[factor]

    # Estimate impact from findings if available
    if findings:
        sev_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for f in findings:
            sev = (
                f.get("severity", "medium")
                if isinstance(f, dict)
                else getattr(f, "severity", "medium")
            )
            if hasattr(sev, "value"):
                sev = sev.value
            sev_counts[sev] = sev_counts.get(sev, 0) + 1

        total = sum(sev_counts.values()) or 1
        weighted = (
            sev_counts["critical"] * 1.0
            + sev_counts["high"] * 0.7
            + sev_counts["medium"] * 0.4
            + sev_counts["low"] * 0.1
        ) / total
        input_kwargs["confidentiality_impact"] = min(1.0, weighted)
        input_kwargs["integrity_impact"] = min(1.0, weighted * 0.8)
        input_kwargs["availability_impact"] = min(1.0, weighted * 0.5)

    # Estimate AI metrics from AARS presence
    has_agent = any(aars_factors.get(f, 0) > 0 for f in AARS_FACTORS)
    if not has_agent:
        input_kwargs["model_robustness"] = 1.0
        input_kwargs["data_sensitivity"] = 1.0
        input_kwargs["ethical_impact"] = 1.0
        input_kwargs["decision_criticality"] = 1.0
        input_kwargs["adaptability"] = 1.0

    return AIVSSInput(**input_kwargs)


def format_aivss_report(result: AIVSSResult) -> str:
    """Format AIVSS result for CLI output."""
    lines = []
    lines.append(f"AIVSS Score: {result.score}/10 ({result.severity})")
    lines.append("")
    lines.append(
        f"Base: {result.base_score}  |  AI: {result.ai_metrics_score}"
        f"  |  AARS: {result.aars_score}  |  Impact: {result.impact_score}"
    )
    lines.append("")
    lines.append("AARS Factors:")

    bar_width = 15
    for factor in AARS_FACTORS:
        info = result.factor_breakdown.get(factor, {"value": 0.0, "label": "Unknown"})
        val = info["value"]
        label = info["label"]
        filled = round(val * bar_width)
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
        name = factor.replace("_", " ").title()
        lines.append(f"  {name:30s} {bar} {val:.1f}  {label}")

    return "\n".join(lines)
