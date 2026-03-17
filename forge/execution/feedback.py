"""Per-agent feedback tracking for false positive rate monitoring."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FEEDBACK_FILENAME = "feedback.json"
FP_RATE_WARNING_THRESHOLD = 0.30


@dataclass
class AgentFeedback:
    total_findings: int = 0
    total_suppressed: int = 0
    last_updated: str = ""

    @property
    def fp_rate(self) -> float:
        if self.total_findings == 0:
            return 0.0
        return self.total_suppressed / self.total_findings


class FeedbackTracker:
    """Tracks per-agent finding and suppression stats across scans."""

    def __init__(self):
        self.agents: dict[str, AgentFeedback] = {}

    @classmethod
    def load(cls, artifacts_dir: str) -> FeedbackTracker:
        tracker = cls()
        path = Path(artifacts_dir) / FEEDBACK_FILENAME
        if not path.exists():
            return tracker
        try:
            data = json.loads(path.read_text())
            for agent_name, stats in data.get("agents", {}).items():
                tracker.agents[agent_name] = AgentFeedback(
                    total_findings=stats.get("total_findings", 0),
                    total_suppressed=stats.get("total_suppressed", 0),
                    last_updated=stats.get("last_updated", ""),
                )
        except Exception:
            logger.warning("Failed to load feedback from %s", path)
        return tracker

    def save(self, artifacts_dir: str) -> None:
        path = Path(artifacts_dir) / FEEDBACK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": {
                name: {
                    "total_findings": fb.total_findings,
                    "total_suppressed": fb.total_suppressed,
                    "fp_rate": round(fb.fp_rate, 3),
                    "last_updated": fb.last_updated,
                }
                for name, fb in self.agents.items()
            }
        }
        path.write_text(json.dumps(data, indent=2))

    def update_from_scan(
        self,
        all_findings: list[dict],
        suppressed_findings: list[dict],
    ) -> dict[str, float]:
        """Update stats from current scan. Returns per-agent FP rates."""
        now = datetime.now(timezone.utc).isoformat()

        # Count findings per agent
        agent_total: dict[str, int] = {}
        for f in all_findings + suppressed_findings:
            agent = f.get("agent", "unknown")
            agent_total[agent] = agent_total.get(agent, 0) + 1

        # Count suppressed per agent
        agent_suppressed: dict[str, int] = {}
        for f in suppressed_findings:
            agent = f.get("agent", "unknown")
            agent_suppressed[agent] = agent_suppressed.get(agent, 0) + 1

        # Update cumulative stats
        for agent_name in agent_total:
            if agent_name not in self.agents:
                self.agents[agent_name] = AgentFeedback()
            fb = self.agents[agent_name]
            fb.total_findings += agent_total[agent_name]
            fb.total_suppressed += agent_suppressed.get(agent_name, 0)
            fb.last_updated = now

        # Check thresholds and warn
        fp_rates = {}
        for name, fb in self.agents.items():
            rate = fb.fp_rate
            fp_rates[name] = rate
            if rate > FP_RATE_WARNING_THRESHOLD and fb.total_findings >= 10:
                logger.warning(
                    "Agent '%s' has %.0f%% false positive rate (%d/%d findings suppressed). "
                    "Consider tuning its prompts or adjusting .forgeignore rules.",
                    name, rate * 100, fb.total_suppressed, fb.total_findings,
                )

        return fp_rates
