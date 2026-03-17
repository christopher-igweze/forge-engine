"""Quality gate: pass/fail evaluation against finding thresholds."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QualityGateThreshold:
    """Thresholds for quality gate evaluation."""

    max_new_critical: int = 0
    max_new_high: int = 0
    max_new_medium: int | None = None  # None = no limit


@dataclass
class QualityGateResult:
    """Result of quality gate evaluation."""

    passed: bool
    reason: str
    new_critical: int = 0
    new_high: int = 0
    new_medium: int = 0
    total_new: int = 0


def evaluate_gate(
    new_findings: list[dict],
    threshold: QualityGateThreshold | None = None,
) -> QualityGateResult:
    """Evaluate quality gate against new findings from baseline comparison.

    Only evaluates NEW findings (not recurring). This prevents existing
    tech debt from blocking merges while ensuring no new issues are introduced.
    """
    if threshold is None:
        threshold = QualityGateThreshold()

    critical = sum(1 for f in new_findings if f.get("severity") == "critical")
    high = sum(1 for f in new_findings if f.get("severity") == "high")
    medium = sum(1 for f in new_findings if f.get("severity") == "medium")

    reasons = []
    if critical > threshold.max_new_critical:
        reasons.append(f"{critical} new critical (max {threshold.max_new_critical})")
    if high > threshold.max_new_high:
        reasons.append(f"{high} new high (max {threshold.max_new_high})")
    if threshold.max_new_medium is not None and medium > threshold.max_new_medium:
        reasons.append(f"{medium} new medium (max {threshold.max_new_medium})")

    passed = len(reasons) == 0
    reason = "Quality gate passed" if passed else f"Quality gate failed: {'; '.join(reasons)}"

    return QualityGateResult(
        passed=passed,
        reason=reason,
        new_critical=critical,
        new_high=high,
        new_medium=medium,
        total_new=len(new_findings),
    )
