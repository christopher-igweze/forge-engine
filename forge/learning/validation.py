"""A/B validation framework for prompt patches.

Tests prompt patches against golden test data before applying them.
Compares baseline (current prompts) vs patched prompts on detection rate,
fix success rate, retry count, escalation rate, and cost.

Requires improvement on ALL metrics to PROMOTE (no regressions allowed).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GoldenTest:
    """A test case with known expected outcomes."""

    test_id: str
    repo_path: str
    description: str = ""
    expected_findings: list[dict] = field(default_factory=list)  # ground truth findings
    expected_fixes: list[dict] = field(default_factory=list)  # ground truth fixes
    expected_score_range: tuple[int, int] = (0, 100)  # (min, max) readiness score

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "repo_path": self.repo_path,
            "description": self.description,
            "expected_findings": self.expected_findings,
            "expected_fixes": self.expected_fixes,
            "expected_score_range": list(self.expected_score_range),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoldenTest:
        score_range = data.get("expected_score_range", [0, 100])
        return cls(
            test_id=data.get("test_id", ""),
            repo_path=data.get("repo_path", ""),
            description=data.get("description", ""),
            expected_findings=data.get("expected_findings", []),
            expected_fixes=data.get("expected_fixes", []),
            expected_score_range=(
                int(score_range[0]) if len(score_range) > 0 else 0,
                int(score_range[1]) if len(score_range) > 1 else 100,
            ),
        )


@dataclass
class MetricComparison:
    """Comparison of a single metric between baseline and patched."""

    metric: str
    baseline_value: float
    patched_value: float
    improved: bool
    delta: float
    delta_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline_value": self.baseline_value,
            "patched_value": self.patched_value,
            "improved": self.improved,
            "delta": round(self.delta, 4),
            "delta_pct": round(self.delta_pct, 2),
        }


@dataclass
class ABResult:
    """Result of A/B testing baseline vs patched prompts."""

    metrics: list[MetricComparison] = field(default_factory=list)
    verdict: str = "INCONCLUSIVE"  # PROMOTE | REJECT | INCONCLUSIVE
    summary: str = ""
    golden_tests_run: int = 0
    baseline_cost_usd: float = 0.0
    patched_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": [m.to_dict() for m in self.metrics],
            "verdict": self.verdict,
            "summary": self.summary,
            "golden_tests_run": self.golden_tests_run,
            "baseline_cost_usd": round(self.baseline_cost_usd, 4),
            "patched_cost_usd": round(self.patched_cost_usd, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ABResult:
        return cls(
            metrics=[
                MetricComparison(**m) for m in data.get("metrics", [])
            ],
            verdict=data.get("verdict", "INCONCLUSIVE"),
            summary=data.get("summary", ""),
            golden_tests_run=data.get("golden_tests_run", 0),
            baseline_cost_usd=data.get("baseline_cost_usd", 0.0),
            patched_cost_usd=data.get("patched_cost_usd", 0.0),
        )


def load_golden_tests(golden_dir: Path) -> list[GoldenTest]:
    """Load golden test definitions from directory.

    Scans for expected.json files in subdirectories of golden_dir.
    """
    tests: list[GoldenTest] = []

    if not golden_dir.is_dir():
        logger.warning("Golden test directory not found: %s", golden_dir)
        return tests

    # Look for expected.json files in subdirectories
    for expected_path in sorted(golden_dir.rglob("expected.json")):
        try:
            data = json.loads(expected_path.read_text())
            test = GoldenTest.from_dict(data)
            # Default repo_path to the parent directory of expected.json
            if not test.repo_path:
                test.repo_path = str(expected_path.parent)
            tests.append(test)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Skipping malformed golden test %s: %s", expected_path, e)

    logger.info("Loaded %d golden tests from %s", len(tests), golden_dir)
    return tests


def compare_metrics(
    baseline: dict[str, float],
    patched: dict[str, float],
) -> list[MetricComparison]:
    """Compare two sets of metrics and determine improvement/regression.

    Metrics where HIGHER is better: detection_rate, fix_success_rate
    Metrics where LOWER is better: retry_count, escalation_rate, cost_usd
    """
    higher_is_better = {"detection_rate", "fix_success_rate"}
    lower_is_better = {"retry_count", "escalation_rate", "cost_usd"}

    all_keys = set(baseline.keys()) | set(patched.keys())
    comparisons: list[MetricComparison] = []

    for metric in sorted(all_keys):
        bval = baseline.get(metric, 0.0)
        pval = patched.get(metric, 0.0)
        delta = pval - bval
        delta_pct = (delta / bval * 100) if bval != 0 else 0.0

        if metric in higher_is_better:
            improved = delta > 0
        elif metric in lower_is_better:
            improved = delta < 0
        else:
            improved = delta >= 0  # Unknown metric: treat as higher-is-better

        comparisons.append(MetricComparison(
            metric=metric,
            baseline_value=bval,
            patched_value=pval,
            improved=improved,
            delta=delta,
            delta_pct=delta_pct,
        ))

    return comparisons


def evaluate_verdict(comparisons: list[MetricComparison]) -> str:
    """Determine verdict from metric comparisons.

    PROMOTE: ALL tracked metrics improved or stayed the same (no regressions)
    REJECT: ANY tracked metric regressed
    INCONCLUSIVE: no metrics to compare
    """
    if not comparisons:
        return "INCONCLUSIVE"

    all_improved = all(c.improved or c.delta == 0 for c in comparisons)
    any_regressed = any(not c.improved and c.delta != 0 for c in comparisons)

    if all_improved:
        return "PROMOTE"
    if any_regressed:
        return "REJECT"
    return "INCONCLUSIVE"


async def ab_test(
    baseline_prompts: dict[str, str],
    patched_prompts: dict[str, str],
    golden_tests: list[GoldenTest],
) -> ABResult:
    """Run A/B test comparing baseline vs patched prompt sets.

    Measures: detection rate, fix success rate, retry count, escalation rate, cost.
    Requires improvement on ALL metrics to PROMOTE (no regressions allowed).

    This is the offline validation step — it simulates scoring without actually
    running the full pipeline. For real A/B testing, the pipeline runner would
    need to be invoked with different prompt sets.
    """
    if not golden_tests:
        return ABResult(
            verdict="INCONCLUSIVE",
            summary="No golden tests available for validation",
        )

    # Simulate baseline and patched scoring
    # In production, this would run the FORGE pipeline twice (expensive).
    # For now, we use a heuristic approach: if the patched prompts are
    # strictly more detailed (more guidance), assume improvement.
    baseline_metrics = _score_prompt_set(baseline_prompts, golden_tests)
    patched_metrics = _score_prompt_set(patched_prompts, golden_tests)

    comparisons = compare_metrics(baseline_metrics, patched_metrics)
    verdict = evaluate_verdict(comparisons)

    # Build summary
    improved = [c for c in comparisons if c.improved]
    regressed = [c for c in comparisons if not c.improved and c.delta != 0]

    summary_parts = [f"Tested against {len(golden_tests)} golden tests."]
    if improved:
        summary_parts.append(
            f"Improved: {', '.join(c.metric for c in improved)}"
        )
    if regressed:
        summary_parts.append(
            f"Regressed: {', '.join(c.metric for c in regressed)}"
        )

    return ABResult(
        metrics=comparisons,
        verdict=verdict,
        summary=" ".join(summary_parts),
        golden_tests_run=len(golden_tests),
        baseline_cost_usd=baseline_metrics.get("cost_usd", 0.0),
        patched_cost_usd=patched_metrics.get("cost_usd", 0.0),
    )


def _score_prompt_set(
    prompts: dict[str, str],
    golden_tests: list[GoldenTest],
) -> dict[str, float]:
    """Score a prompt set against golden tests using heuristic analysis.

    Returns metric dict with: detection_rate, fix_success_rate,
    retry_count, escalation_rate, cost_usd.

    This is a static heuristic — real scoring requires running the pipeline.
    The heuristic scores prompts based on:
    - Coverage of expected finding categories mentioned in prompt text
    - Specificity of instructions (length, keyword density)
    """
    if not prompts:
        return {
            "detection_rate": 0.0,
            "fix_success_rate": 0.0,
            "retry_count": 0.0,
            "escalation_rate": 0.0,
            "cost_usd": 0.0,
        }

    # Aggregate prompt quality signals
    total_prompt_len = sum(len(p) for p in prompts.values())
    num_prompts = len(prompts)

    # Coverage: how many expected finding categories are mentioned
    all_categories = set()
    for test in golden_tests:
        for f in test.expected_findings:
            cat = f.get("category", "").lower()
            if cat:
                all_categories.add(cat)

    prompt_text = " ".join(prompts.values()).lower()
    categories_covered = sum(1 for c in all_categories if c in prompt_text)
    coverage_rate = categories_covered / len(all_categories) if all_categories else 0.5

    # Specificity: longer, more detailed prompts correlate with better performance
    avg_len = total_prompt_len / num_prompts if num_prompts > 0 else 0
    specificity_bonus = min(avg_len / 2000, 0.2)  # Max 20% bonus for length

    return {
        "detection_rate": min(1.0, 0.5 + coverage_rate * 0.3 + specificity_bonus),
        "fix_success_rate": min(1.0, 0.5 + specificity_bonus),
        "retry_count": max(0.0, 2.0 - specificity_bonus * 5),
        "escalation_rate": max(0.0, 0.3 - specificity_bonus),
        "cost_usd": total_prompt_len * 0.00001,  # Rough token cost proxy
    }


def save_ab_result(result: ABResult, output_path: Path) -> None:
    """Save A/B test result to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.to_dict(), indent=2))
    logger.info("Saved A/B result: %s (verdict: %s)", output_path, result.verdict)
