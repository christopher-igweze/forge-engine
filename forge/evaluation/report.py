"""Report generation for FORGE v3 evaluation."""

from __future__ import annotations


def build_json_report(
    scores,  # DimensionScores
    gate_result,  # QualityGateResult
    check_results,  # list[CheckResult]
    compliance,  # dict with asvs, stride, nist
    baseline_delta=None,  # dict or None
    feedback=None,  # dict or None
    project_context=None,  # dict or None
) -> dict:
    """Build the full v3 JSON report."""
    letter, label = scores.band()
    composite = scores.composite()

    failed_checks = []
    for r in check_results:
        if not r.passed:
            failed_checks.append({
                "check_id": r.check_id,
                "name": r.name,
                "severity": r.severity,
                "deduction": r.deduction,
                "details": r.details,
                "locations": r.locations,
                "fix_guidance": r.fix_guidance,
            })

    report = {
        "version": "3.0",
        "scores": {
            "composite": composite,
            "band": letter,
            "label": label,
            "dimensions": scores.to_dict(),
        },
        "quality_gate": {
            "passed": gate_result.passed,
            "profile": gate_result.profile,
            "failures": gate_result.failures,
        },
        "compliance": compliance,
        "deterministic_checks": {
            "total": len(check_results),
            "passed": sum(1 for r in check_results if r.passed),
            "failed": sum(1 for r in check_results if not r.passed),
            "failed_checks": failed_checks,
        },
    }

    if baseline_delta is not None:
        report["baseline_delta"] = baseline_delta
    if feedback is not None:
        report["feedback"] = feedback
    if project_context is not None:
        report["project_context"] = project_context

    return report


def format_cli_report(report: dict) -> str:
    """Format report as CLI text output with visual bars."""
    lines: list[str] = []

    lines.append("FORGE v3 Evaluation Report")
    lines.append("=" * 26)
    lines.append("")

    scores = report["scores"]
    composite = scores["composite"]
    band = scores["band"]
    label = scores["label"]
    lines.append(f"Composite Score: {composite}/100 ({band} -- {label})")

    gate = report["quality_gate"]
    gate_status = "PASSED" if gate["passed"] else "FAILED"
    lines.append(f"Quality Gate:    {gate_status}")
    lines.append("")

    lines.append("Dimensions:")
    dimensions = scores["dimensions"]
    for dim_name, dim_data in dimensions.items():
        score = dim_data["score"]
        failed = dim_data["checks_failed"]
        bar_len = 25
        filled = round(score / 100 * bar_len)
        empty = bar_len - filled
        bar = "\u2588" * filled + "\u2591" * empty
        display_name = dim_name.replace("_", " ").title()
        fail_note = f"  ({failed} checks failed)" if failed > 0 else ""
        lines.append(f"  {display_name:<18} {bar}  {score:>3}{fail_note}")

    lines.append("")

    if gate["failures"]:
        lines.append("Gate Failures:")
        for failure in gate["failures"]:
            lines.append(f"  - {failure}")
        lines.append("")

    checks = report["deterministic_checks"]
    lines.append(
        f"Checks: {checks['passed']}/{checks['total']} passed, "
        f"{checks['failed']} failed"
    )

    if report.get("compliance"):
        comp = report["compliance"]
        if "asvs" in comp:
            asvs = comp["asvs"]
            lines.append(
                f"ASVS Level: {asvs['estimated_level']} "
                f"({asvs['passed']}/{asvs['total_requirements']} requirements)"
            )
        if "nist" in comp:
            nist = comp["nist"]
            lines.append(
                f"NIST SSDF: {nist['covered']}/{nist['total']} practices covered"
            )

    if report.get("baseline_delta"):
        lines.append("")
        delta = report["baseline_delta"]
        lines.append("Baseline Delta:")
        for key, value in delta.items():
            lines.append(f"  {key}: {value}")

    lines.append("")
    return "\n".join(lines)
