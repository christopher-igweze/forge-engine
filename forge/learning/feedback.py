"""Fix outcome feedback: training_data.jsonl -> agent prompt enrichment.

Reads fix outcome data logged by ForgeTelemetry, aggregates success/failure
rates per category and tier, selects best-performing fix examples, and
generates structured guidance that can be injected into coder agent prompts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CategoryStats:
    """Fix outcome statistics for a single finding category."""

    category: str
    total_fixes: int = 0
    successes: int = 0
    failures: int = 0
    avg_retry_count: float = 0.0
    escalation_rate: float = 0.0
    models_used: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.successes / self.total_fixes if self.total_fixes else 0.0


@dataclass
class TierStats:
    """Fix outcome statistics for a single remediation tier."""

    tier: int
    total_fixes: int = 0
    successes: int = 0
    failures: int = 0
    avg_retry_count: float = 0.0
    escalation_rate: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.total_fixes if self.total_fixes else 0.0


@dataclass
class FixOutcomeStats:
    """Aggregated fix outcome statistics across all training data."""

    total_entries: int = 0
    by_category: dict[str, CategoryStats] = field(default_factory=dict)
    by_tier: dict[int, TierStats] = field(default_factory=dict)


@dataclass
class AgentGuidance:
    """Structured guidance derived from fix outcome data."""

    lessons: list[str] = field(default_factory=list)
    few_shot_examples: list[dict] = field(default_factory=list)
    stats_summary: dict = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """Format as a text block suitable for injection into agent prompts."""
        lines: list[str] = []
        if self.lessons:
            lines.append("LESSONS_LEARNED:")
            for lesson in self.lessons:
                lines.append(f"- {lesson}")
            lines.append("")

        if self.few_shot_examples:
            lines.append("SUCCESSFUL_EXAMPLES:")
            for i, ex in enumerate(self.few_shot_examples, 1):
                lines.append(f"{i}. Finding: {ex.get('title', 'Unknown')}")
                lines.append(f"   Category: {ex.get('category', 'Unknown')}")
                if ex.get("fix_summary"):
                    lines.append(f"   Fix: {ex['fix_summary']}")
                lines.append(f"   Outcome: {ex.get('outcome', 'success')}")
            lines.append("")

        return "\n".join(lines)


def _load_training_data(artifacts_dir: Path) -> list[dict]:
    """Load training data entries from telemetry/training_data.jsonl."""
    path = artifacts_dir / "telemetry" / "training_data.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in training_data.jsonl")
    return entries


def aggregate_fix_outcomes(artifacts_dir: Path) -> FixOutcomeStats:
    """Read training_data.jsonl and compute per-category/tier statistics."""
    entries = _load_training_data(artifacts_dir)
    if not entries:
        return FixOutcomeStats()

    stats = FixOutcomeStats(total_entries=len(entries))

    # Group by category
    cat_entries: dict[str, list[dict]] = {}
    tier_entries: dict[int, list[dict]] = {}

    for e in entries:
        cat = e.get("finding_category", "unknown").upper()
        tier = e.get("tier", 0)

        cat_entries.setdefault(cat, []).append(e)
        tier_entries.setdefault(tier, []).append(e)

    # Build category stats
    for cat, group in cat_entries.items():
        successes = sum(
            1 for e in group if e.get("fix_outcome", "") in ("completed", "completed_with_debt")
        )
        failures = len(group) - successes
        retry_counts = [e.get("retry_count", 0) for e in group]
        escalated = sum(1 for e in group if e.get("escalated", False))
        models = {}
        for e in group:
            m = e.get("model_used", "unknown")
            models[m] = models.get(m, 0) + 1

        stats.by_category[cat] = CategoryStats(
            category=cat,
            total_fixes=len(group),
            successes=successes,
            failures=failures,
            avg_retry_count=sum(retry_counts) / len(retry_counts) if retry_counts else 0.0,
            escalation_rate=escalated / len(group) if group else 0.0,
            models_used=models,
        )

    # Build tier stats
    for tier, group in tier_entries.items():
        successes = sum(
            1 for e in group if e.get("fix_outcome", "") in ("completed", "completed_with_debt")
        )
        failures = len(group) - successes
        retry_counts = [e.get("retry_count", 0) for e in group]
        escalated = sum(1 for e in group if e.get("escalated", False))

        stats.by_tier[tier] = TierStats(
            tier=tier,
            total_fixes=len(group),
            successes=successes,
            failures=failures,
            avg_retry_count=sum(retry_counts) / len(retry_counts) if retry_counts else 0.0,
            escalation_rate=escalated / len(group) if group else 0.0,
        )

    return stats


def select_few_shot_examples(
    artifacts_dir: Path,
    category: str | None = None,
    max_examples: int = 3,
) -> list[dict]:
    """Select best-performing fix examples for prompt injection.

    Picks successful fixes with the lowest retry count and no escalation.
    Optionally filters by category.
    """
    entries = _load_training_data(artifacts_dir)
    if not entries:
        return []

    # Filter to successes only
    successes = [
        e for e in entries
        if e.get("fix_outcome", "") in ("completed", "completed_with_debt")
        and not e.get("escalated", False)
    ]

    if category:
        successes = [
            e for e in successes
            if e.get("finding_category", "").upper() == category.upper()
        ]

    # Sort by retry_count ascending (best fixes first)
    successes.sort(key=lambda e: e.get("retry_count", 0))

    examples = []
    for e in successes[:max_examples]:
        examples.append({
            "title": e.get("finding_title", ""),
            "category": e.get("finding_category", ""),
            "severity": e.get("finding_severity", ""),
            "tier": e.get("tier", 0),
            "fix_summary": e.get("fix_summary", ""),
            "files_changed": e.get("files_changed", []),
            "outcome": e.get("fix_outcome", ""),
            "retry_count": e.get("retry_count", 0),
        })

    return examples


def generate_agent_guidance(artifacts_dir: Path) -> AgentGuidance:
    """Combine stats and few-shot examples into structured agent guidance.

    Saves output to forge/learning/guidance/ as both JSON and markdown.
    """
    stats = aggregate_fix_outcomes(artifacts_dir)
    if stats.total_entries == 0:
        logger.info("No training data found -- no guidance to generate")
        return AgentGuidance()

    # Generate lessons from category stats
    lessons: list[str] = []
    for cat, cs in sorted(stats.by_category.items()):
        if cs.total_fixes >= 2:
            rate_pct = cs.success_rate * 100
            lessons.append(
                f"For {cat} findings: fix success rate {rate_pct:.0f}% "
                f"(avg {cs.avg_retry_count:.1f} retries, "
                f"{cs.escalation_rate * 100:.0f}% escalation rate)"
            )

    for tier, ts in sorted(stats.by_tier.items()):
        if ts.total_fixes >= 2 and ts.escalation_rate > 0.3:
            lessons.append(
                f"Tier {tier} findings escalate {ts.escalation_rate * 100:.0f}% "
                f"of the time — consider splitting complex fixes"
            )

    # Select few-shot examples (best across all categories)
    few_shots = select_few_shot_examples(artifacts_dir, max_examples=5)

    guidance = AgentGuidance(
        lessons=lessons,
        few_shot_examples=few_shots,
        stats_summary={
            "total_training_entries": stats.total_entries,
            "by_category": {
                cat: {
                    "total": cs.total_fixes,
                    "success_rate": round(cs.success_rate, 3),
                    "avg_retries": round(cs.avg_retry_count, 2),
                    "escalation_rate": round(cs.escalation_rate, 3),
                }
                for cat, cs in stats.by_category.items()
            },
            "by_tier": {
                str(tier): {
                    "total": ts.total_fixes,
                    "success_rate": round(ts.success_rate, 3),
                    "avg_retries": round(ts.avg_retry_count, 2),
                    "escalation_rate": round(ts.escalation_rate, 3),
                }
                for tier, ts in stats.by_tier.items()
            },
        },
    )

    # Save outputs
    guidance_dir = Path(__file__).parent / "guidance"
    guidance_dir.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = guidance_dir / "agent_guidance.json"
    json_path.write_text(json.dumps({
        "lessons": guidance.lessons,
        "few_shot_examples": guidance.few_shot_examples,
        "stats_summary": guidance.stats_summary,
    }, indent=2))

    # Human-readable markdown
    md_path = guidance_dir / "agent_guidance.md"
    md_lines = ["# FORGE Agent Guidance (Auto-Generated)", ""]
    md_lines.append(guidance.to_prompt_block())
    md_lines.append("## Statistics Summary")
    md_lines.append(f"- Total training entries: {stats.total_entries}")
    for cat, cs in sorted(stats.by_category.items()):
        md_lines.append(
            f"- {cat}: {cs.successes}/{cs.total_fixes} success "
            f"({cs.success_rate * 100:.0f}%), "
            f"avg retries {cs.avg_retry_count:.1f}"
        )
    md_lines.append("")

    md_path.write_text("\n".join(md_lines))

    logger.info(
        "Agent guidance generated: %d lessons, %d examples",
        len(guidance.lessons),
        len(guidance.few_shot_examples),
    )
    return guidance
