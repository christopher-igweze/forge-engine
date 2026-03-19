"""Quality gate evaluation for FORGE v3."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QualityGate:
    min_security_score: int = 40
    min_reliability_score: int = 30
    min_test_score: int = 20
    max_new_critical: int = 0
    max_new_high: int = 0
    max_new_medium: int | None = None
    min_composite_score: int = 40


@dataclass
class QualityGateResult:
    passed: bool
    failures: list[str]
    profile: str
    scores_summary: dict
    composite_score: int


GATE_PROFILES = {
    "forge-way": QualityGate(),
    "strict": QualityGate(
        min_security_score=60,
        min_reliability_score=40,
        min_test_score=40,
        min_composite_score=60,
    ),
    "startup": QualityGate(
        min_security_score=30,
        min_test_score=0,
        min_composite_score=20,
    ),
}


def evaluate_quality_gate(
    scores,  # DimensionScores
    gate: QualityGate | str = "forge-way",
    baseline_comparison: dict | None = None,
) -> QualityGateResult:
    """Evaluate quality gate. gate can be a QualityGate instance or a profile name string."""
    if isinstance(gate, str):
        gate_obj = GATE_PROFILES.get(gate, GATE_PROFILES["forge-way"])
        profile_name = gate
    else:
        gate_obj = gate
        profile_name = "custom"

    failures: list[str] = []

    if scores.security.score < gate_obj.min_security_score:
        failures.append(
            f"Security score {scores.security.score} < {gate_obj.min_security_score} minimum"
        )
    if scores.reliability.score < gate_obj.min_reliability_score:
        failures.append(
            f"Reliability score {scores.reliability.score} < {gate_obj.min_reliability_score} minimum"
        )
    if scores.test_quality.score < gate_obj.min_test_score:
        failures.append(
            f"Test quality score {scores.test_quality.score} < {gate_obj.min_test_score} minimum"
        )

    composite = scores.composite()
    if composite < gate_obj.min_composite_score:
        failures.append(
            f"Composite score {composite} < {gate_obj.min_composite_score} minimum"
        )

    if baseline_comparison:
        new_crit = baseline_comparison.get("new_critical", 0)
        new_high = baseline_comparison.get("new_high", 0)
        new_med = baseline_comparison.get("new_medium", 0)

        if new_crit > gate_obj.max_new_critical:
            failures.append(
                f"{new_crit} new critical findings (max {gate_obj.max_new_critical})"
            )
        if new_high > gate_obj.max_new_high:
            failures.append(
                f"{new_high} new high findings (max {gate_obj.max_new_high})"
            )
        if gate_obj.max_new_medium is not None and new_med > gate_obj.max_new_medium:
            failures.append(
                f"{new_med} new medium findings (max {gate_obj.max_new_medium})"
            )

    return QualityGateResult(
        passed=len(failures) == 0,
        failures=failures,
        profile=profile_name,
        scores_summary=scores.to_dict(),
        composite_score=composite,
    )
