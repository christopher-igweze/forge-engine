"""FORGE v3 Deterministic Evaluation Framework."""

from __future__ import annotations

from forge.evaluation.dimensions import DimensionScores, compute_scores_from_opengrep, run_all_checks
from forge.evaluation.quality_gate import (
    QualityGate,
    QualityGateResult,
    evaluate_quality_gate,
    GATE_PROFILES,
)
from forge.evaluation.compliance import (
    estimate_asvs_level,
    get_stride_mapping,
    get_nist_coverage,
)
from forge.evaluation.feedback import Feedback
from forge.evaluation.report import build_json_report, format_cli_report


def run_evaluation(
    repo_path: str,
    gate_profile: str = "forge-way",
    weights: dict[str, float] | None = None,
    baseline_comparison: dict | None = None,
    opengrep_findings: list | None = None,
) -> dict:
    """Run full deterministic evaluation. Returns JSON-serializable report dict.

    If opengrep_findings is provided, scores are computed from those.
    Otherwise, falls back to built-in regex checks.
    """
    # 1. Run all checks and compute dimension scores
    if opengrep_findings:
        scores, all_results = compute_scores_from_opengrep(opengrep_findings)
    else:
        scores, all_results = run_all_checks(repo_path)

    # 2. Evaluate quality gate
    gate_result = evaluate_quality_gate(
        scores, gate=gate_profile, baseline_comparison=baseline_comparison
    )

    # 3. Compute compliance mappings
    compliance = {
        "asvs": estimate_asvs_level(all_results),
        "stride": get_stride_mapping(all_results),
        "nist": get_nist_coverage(all_results),
    }

    # 4. Load and update feedback
    feedback = Feedback.load(repo_path)
    feedback.record_check_results(all_results)
    feedback.save(repo_path)

    # 5. Build and return JSON report
    return build_json_report(
        scores=scores,
        gate_result=gate_result,
        check_results=all_results,
        compliance=compliance,
        baseline_delta=baseline_comparison,
        feedback=feedback.to_dict(),
    )


__all__ = [
    "run_evaluation",
    "DimensionScores",
    "QualityGate",
    "QualityGateResult",
    "evaluate_quality_gate",
    "GATE_PROFILES",
    "Feedback",
    "build_json_report",
    "format_cli_report",
    "estimate_asvs_level",
    "get_stride_mapping",
    "get_nist_coverage",
    "run_all_checks",
    "compute_scores_from_opengrep",
]
