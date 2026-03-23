"""Learning loop status report — CLI-friendly summary of FORGE learning data.

Aggregates findings history, training data, telemetry cost, and proposed
patterns into a single report for monitoring the learning loop health.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from forge.learning.feedback import aggregate_fix_outcomes
from forge.patterns.learner import load_findings_history

logger = logging.getLogger(__name__)


@dataclass
class LearningReport:
    """Structured learning loop status report."""

    total_scans: int = 0
    total_findings: int = 0
    total_fixes_attempted: int = 0
    fix_success_rate_by_category: dict[str, float] = field(default_factory=dict)
    fix_success_rate_by_tier: dict[int, float] = field(default_factory=dict)
    avg_retries_by_tier: dict[int, float] = field(default_factory=dict)
    escalation_rate_by_tier: dict[int, float] = field(default_factory=dict)
    top_patterns: list[dict] = field(default_factory=list)
    proposed_patterns_count: int = 0
    total_cost_usd: float = 0.0
    total_invocations: int = 0

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON output."""
        return {
            "total_scans": self.total_scans,
            "total_findings": self.total_findings,
            "total_fixes_attempted": self.total_fixes_attempted,
            "fix_success_rate_by_category": self.fix_success_rate_by_category,
            "fix_success_rate_by_tier": {
                str(k): v for k, v in self.fix_success_rate_by_tier.items()
            },
            "avg_retries_by_tier": {
                str(k): v for k, v in self.avg_retries_by_tier.items()
            },
            "escalation_rate_by_tier": {
                str(k): v for k, v in self.escalation_rate_by_tier.items()
            },
            "top_patterns": self.top_patterns,
            "proposed_patterns_count": self.proposed_patterns_count,
            "total_cost_usd": self.total_cost_usd,
            "total_invocations": self.total_invocations,
        }

    def to_terminal(self) -> str:
        """Format as a human-readable terminal string."""
        lines = [
            "=" * 60,
            "  FORGE Learning Loop Status Report",
            "=" * 60,
            "",
            f"  Scans processed:         {self.total_scans}",
            f"  Total findings:          {self.total_findings}",
            f"  Fixes attempted:         {self.total_fixes_attempted}",
            f"  Proposed patterns:       {self.proposed_patterns_count}",
            f"  Total cost:              ${self.total_cost_usd:.4f}",
            f"  Total LLM invocations:   {self.total_invocations}",
            "",
        ]

        if self.fix_success_rate_by_category:
            lines.append("  Fix Success Rate by Category:")
            for cat, rate in sorted(self.fix_success_rate_by_category.items()):
                bar = _progress_bar(rate)
                lines.append(f"    {cat:<20s} {bar} {rate * 100:.0f}%")
            lines.append("")

        if self.fix_success_rate_by_tier:
            lines.append("  Fix Success Rate by Tier:")
            for tier in sorted(self.fix_success_rate_by_tier):
                rate = self.fix_success_rate_by_tier[tier]
                retries = self.avg_retries_by_tier.get(tier, 0)
                esc = self.escalation_rate_by_tier.get(tier, 0)
                bar = _progress_bar(rate)
                lines.append(
                    f"    Tier {tier}  {bar} {rate * 100:.0f}%  "
                    f"(avg retries: {retries:.1f}, escalation: {esc * 100:.0f}%)"
                )
            lines.append("")

        if self.top_patterns:
            lines.append("  Top Patterns by Prevalence:")
            for p in self.top_patterns[:10]:
                lines.append(
                    f"    {p.get('pattern_id', '?'):<8s} "
                    f"{p.get('name', 'Unknown'):<40s} "
                    f"x{p.get('times_detected', 0)}"
                )
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


def _progress_bar(ratio: float, width: int = 20) -> str:
    """Render a simple ASCII progress bar."""
    filled = int(ratio * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _count_distinct_scans(findings: list[dict]) -> int:
    """Count distinct scans from findings history timestamps.

    Uses timestamps truncated to the minute as a proxy for distinct runs.
    """
    return len({f.get("timestamp", "")[:16] for f in findings if f.get("timestamp")})


def _load_cost_summary(artifacts_dir: Path) -> dict:
    """Load telemetry cost summary if available."""
    path = artifacts_dir / "telemetry" / "cost_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _count_proposed_patterns(library_dir: Path) -> int:
    """Count YAML files in the proposed patterns directory."""
    proposed_dir = library_dir / "proposed"
    if not proposed_dir.is_dir():
        return 0
    return len(list(proposed_dir.glob("VP-*.yaml")))


def _load_top_patterns(library_dir: Path, limit: int = 10) -> list[dict]:
    """Load patterns sorted by times_detected descending."""
    import yaml as _yaml

    patterns = []
    for subdir in ("curated", "proposed"):
        pattern_dir = library_dir / subdir
        if not pattern_dir.is_dir():
            continue
        for yaml_path in pattern_dir.glob("VP-*.yaml"):
            try:
                data = _yaml.safe_load(yaml_path.read_text())
                if data and isinstance(data, dict):
                    patterns.append({
                        "pattern_id": data.get("id", yaml_path.stem),
                        "name": data.get("name", "Unknown"),
                        "category": data.get("category", ""),
                        "source": data.get("source", ""),
                        "times_detected": data.get("times_detected", 0),
                    })
            except Exception:
                continue

    patterns.sort(key=lambda p: p.get("times_detected", 0), reverse=True)
    return patterns[:limit]


def generate_learning_report(
    artifacts_dir: Path,
    library_dir: Path | None = None,
) -> LearningReport:
    """Generate a comprehensive learning loop status report."""
    if library_dir is None:
        library_dir = Path(__file__).parent.parent / "patterns" / "library"

    # Findings history
    findings = load_findings_history(artifacts_dir)
    total_scans = _count_distinct_scans(findings)

    # Fix outcome stats
    fix_stats = aggregate_fix_outcomes(artifacts_dir)

    # Cost summary
    cost_summary = _load_cost_summary(artifacts_dir)

    # Patterns
    proposed_count = _count_proposed_patterns(library_dir)
    top_patterns = _load_top_patterns(library_dir)

    report = LearningReport(
        total_scans=total_scans,
        total_findings=len(findings),
        total_fixes_attempted=fix_stats.total_entries,
        fix_success_rate_by_category={
            cat: round(cs.success_rate, 3)
            for cat, cs in fix_stats.by_category.items()
        },
        fix_success_rate_by_tier={
            tier: round(ts.success_rate, 3)
            for tier, ts in fix_stats.by_tier.items()
        },
        avg_retries_by_tier={
            tier: round(ts.avg_retry_count, 2)
            for tier, ts in fix_stats.by_tier.items()
        },
        escalation_rate_by_tier={
            tier: round(ts.escalation_rate, 3)
            for tier, ts in fix_stats.by_tier.items()
        },
        top_patterns=top_patterns,
        proposed_patterns_count=proposed_count,
        total_cost_usd=cost_summary.get("total_cost_usd", 0.0),
        total_invocations=cost_summary.get("total_invocations", 0),
    )

    return report


if __name__ == "__main__":
    import sys

    artifacts = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".artifacts")
    lib_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    report = generate_learning_report(artifacts, lib_dir)

    # Print terminal-formatted report
    print(report.to_terminal())

    # Also save JSON
    json_path = artifacts / "learning_report.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report.to_dict(), indent=2))
    print(f"\nJSON report saved to: {json_path}")
