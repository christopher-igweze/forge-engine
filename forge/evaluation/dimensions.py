"""Dimension scoring for FORGE v3 evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {
    "security": 0.30,
    "reliability": 0.20,
    "maintainability": 0.15,
    "test_quality": 0.15,
    "performance": 0.10,
    "documentation": 0.05,
    "operations": 0.05,
}

SCORE_BANDS = [
    (80, "A", "Production Ready"),
    (60, "B", "Near Ready"),
    (40, "C", "Needs Work"),
    (20, "D", "Major Gaps"),
    (0, "F", "Not Ready"),
]


@dataclass
class DimensionScore:
    """Score for a single dimension."""

    name: str
    score: int  # 0-100
    checks_passed: int
    checks_failed: int
    deductions: int  # Total negative points applied
    check_results: list = field(default_factory=list)  # list[CheckResult]


@dataclass
class DimensionScores:
    security: DimensionScore
    reliability: DimensionScore
    maintainability: DimensionScore
    test_quality: DimensionScore
    performance: DimensionScore
    documentation: DimensionScore
    operations: DimensionScore

    def composite(self, weights: dict[str, float] | None = None) -> int:
        """Weighted average across all dimensions."""
        w = weights or DEFAULT_WEIGHTS
        total = 0.0
        for dim_name, weight in w.items():
            score = getattr(self, dim_name)
            total += score.score * weight
        return round(total)

    def band(self, weights: dict[str, float] | None = None) -> tuple[str, str]:
        """Return (letter, label) based on composite score."""
        comp = self.composite(weights)
        for threshold, letter, label in SCORE_BANDS:
            if comp >= threshold:
                return letter, label
        return "F", "Not Ready"

    def to_dict(self) -> dict:
        """JSON-serializable dict."""
        return {
            name: {
                "score": getattr(self, name).score,
                "checks_passed": getattr(self, name).checks_passed,
                "checks_failed": getattr(self, name).checks_failed,
                "deductions": getattr(self, name).deductions,
            }
            for name in DEFAULT_WEIGHTS
        }


def compute_dimension_score(check_results: list, dimension_name: str) -> DimensionScore:
    """Compute score from a list of CheckResults. Score = max(0, 100 + sum(deductions))."""
    total_deduction = sum(r.deduction for r in check_results)
    passed = sum(1 for r in check_results if r.passed)
    failed = sum(1 for r in check_results if not r.passed)
    score = max(0, min(100, 100 + total_deduction))
    return DimensionScore(
        name=dimension_name,
        score=score,
        checks_passed=passed,
        checks_failed=failed,
        deductions=total_deduction,
        check_results=check_results,
    )


def compute_scores_from_opengrep(
    findings: list,
) -> tuple[DimensionScores, list]:
    """Compute dimension scores from Opengrep findings.

    Args:
        findings: list of dicts with at least 'category', 'severity', 'title' fields.

    Returns:
        (DimensionScores, list of CheckResult-compatible objects)
    """
    from forge.evaluation.checks import (
        CheckResult,
        SEVERITY_DEDUCTIONS as _STANDARD_DEDUCTIONS,
    )

    # Use the shared severity-weighted deduction table so opengrep findings
    # hurt the score the same amount as built-in deterministic checks.
    SEVERITY_DEDUCTIONS = _STANDARD_DEDUCTIONS

    CATEGORY_TO_DIMENSION = {
        "security": "security",
        "quality": "maintainability",
        "reliability": "reliability",
        "performance": "performance",
        "architecture": "maintainability",
    }

    dimension_results: dict[str, list] = {d: [] for d in DEFAULT_WEIGHTS}
    all_check_results: list = []

    for f in findings:
        cat = f.get("category", "security") if isinstance(f, dict) else getattr(f, "category", "security")
        sev = f.get("severity", "medium") if isinstance(f, dict) else getattr(f, "severity", "medium")

        # Normalize enum values
        if hasattr(cat, "value"):
            cat = cat.value
        if hasattr(sev, "value"):
            sev = sev.value

        dimension = CATEGORY_TO_DIMENSION.get(str(cat), "security")
        deduction = SEVERITY_DEDUCTIONS.get(str(sev), -8)

        check_id = f.get("forge_check_id", "") if isinstance(f, dict) else getattr(f, "forge_check_id", "")
        title = f.get("title", "") if isinstance(f, dict) else getattr(f, "title", "")

        cr = CheckResult(
            check_id=check_id or "OG-AUTO",
            name=title[:80] if title else "Opengrep finding",
            passed=False,
            severity=str(sev),
            deduction=deduction,
            details=title,
        )
        all_check_results.append(cr)
        if dimension in dimension_results:
            dimension_results[dimension].append(cr)

    # Build DimensionScores — dimensions with no findings get perfect score
    dim_scores = {}
    for dim_name in DEFAULT_WEIGHTS:
        results = dimension_results.get(dim_name, [])
        dim_scores[dim_name] = compute_dimension_score(results, dim_name)

    scores = DimensionScores(**dim_scores)
    return scores, all_check_results


def _safe_import(module_path: str, func_name: str):
    """Import a check runner, returning a no-op if the module isn't available yet."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, func_name)
    except (ImportError, AttributeError):
        return lambda repo_path: []


def _strip_repo_prefix(results: list, repo_path: str) -> list:
    """Strip repo_path prefix from check result locations for clean display."""
    prefix = repo_path.rstrip("/") + "/"
    for r in results:
        for loc in r.locations:
            f = loc.get("file", "")
            if f.startswith(prefix):
                loc["file"] = f[len(prefix):]
    return results


def _apply_forgeignore(results: list, repo_path: str) -> list:
    """Remove check results suppressed by .forgeignore.

    Suppressed checks are excluded entirely — they don't appear in the
    report and don't affect scores.
    """
    try:
        from forge.execution.forgeignore import ForgeIgnore
        forgeignore = ForgeIgnore.load(repo_path)
        if not forgeignore.rules:
            return results
        kept = []
        for r in results:
            if r.passed:
                kept.append(r)
                continue
            finding_dict = {
                "check_id": r.check_id,
                "title": r.name,
                "severity": r.severity,
                "locations": r.locations,
            }
            is_sup, _ = forgeignore.is_suppressed(finding_dict)
            if not is_sup:
                kept.append(r)
        return kept
    except Exception:
        return results


def run_all_checks(repo_path: str) -> tuple[DimensionScores, list]:
    """Run all dimension checks and return scores + flat list of all CheckResults."""
    run_security_checks = _safe_import("forge.evaluation.checks.security", "run_security_checks")
    run_reliability_checks = _safe_import("forge.evaluation.checks.reliability", "run_reliability_checks")
    run_maintainability_checks = _safe_import("forge.evaluation.checks.maintainability", "run_maintainability_checks")
    run_test_quality_checks = _safe_import("forge.evaluation.checks.test_quality", "run_test_quality_checks")
    run_performance_checks = _safe_import("forge.evaluation.checks.performance", "run_performance_checks")
    run_documentation_checks = _safe_import("forge.evaluation.checks.documentation", "run_documentation_checks")
    run_operations_checks = _safe_import("forge.evaluation.checks.operations", "run_operations_checks")

    sec = _apply_forgeignore(_strip_repo_prefix(run_security_checks(repo_path), repo_path), repo_path)
    rel = _apply_forgeignore(_strip_repo_prefix(run_reliability_checks(repo_path), repo_path), repo_path)
    mnt = _apply_forgeignore(_strip_repo_prefix(run_maintainability_checks(repo_path), repo_path), repo_path)
    tst = _apply_forgeignore(_strip_repo_prefix(run_test_quality_checks(repo_path), repo_path), repo_path)
    prf = _apply_forgeignore(_strip_repo_prefix(run_performance_checks(repo_path), repo_path), repo_path)
    doc = _apply_forgeignore(_strip_repo_prefix(run_documentation_checks(repo_path), repo_path), repo_path)
    ops = _apply_forgeignore(_strip_repo_prefix(run_operations_checks(repo_path), repo_path), repo_path)

    all_results = sec + rel + mnt + tst + prf + doc + ops

    scores = DimensionScores(
        security=compute_dimension_score(sec, "security"),
        reliability=compute_dimension_score(rel, "reliability"),
        maintainability=compute_dimension_score(mnt, "maintainability"),
        test_quality=compute_dimension_score(tst, "test_quality"),
        performance=compute_dimension_score(prf, "performance"),
        documentation=compute_dimension_score(doc, "documentation"),
        operations=compute_dimension_score(ops, "operations"),
    )

    return scores, all_results
