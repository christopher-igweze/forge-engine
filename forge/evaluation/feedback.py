"""False positive rate tracking for FORGE v3 evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Feedback:
    checks: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, dict] = field(default_factory=dict)
    updated_at: str = ""

    @classmethod
    def load(cls, repo_path: str) -> Feedback:
        """Load feedback from .artifacts/feedback.json, or return empty."""
        path = Path(repo_path) / ".artifacts" / "feedback.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                checks=data.get("checks", {}),
                agents=data.get("agents", {}),
                updated_at=data.get("updated_at", ""),
            )
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self, repo_path: str) -> None:
        """Persist feedback to .artifacts/feedback.json."""
        artifacts = Path(repo_path) / ".artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        path = artifacts / "feedback.json"
        path.write_text(
            json.dumps(
                {
                    "checks": self.checks,
                    "agents": self.agents,
                    "updated_at": self.updated_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def record_check_results(self, check_results: list) -> None:
        """Update check trigger counts from a scan."""
        for result in check_results:
            cid = result.check_id
            if cid not in self.checks:
                self.checks[cid] = {
                    "total_triggers": 0,
                    "confirmed_fp": 0,
                    "fp_rate": 0.0,
                }
            entry = self.checks[cid]
            if not result.passed:
                entry["total_triggers"] += 1
            # FP rate is updated when user marks false positives externally;
            # here we just track trigger counts.
            if entry["total_triggers"] > 0:
                entry["fp_rate"] = entry["confirmed_fp"] / entry["total_triggers"]

    def record_suppressed_findings(
        self, agent_name: str, total: int, suppressed: int
    ) -> None:
        """Update agent FP tracking."""
        if agent_name not in self.agents:
            self.agents[agent_name] = {
                "total_findings": 0,
                "suppressed": 0,
                "fp_rate": 0.0,
            }
        entry = self.agents[agent_name]
        entry["total_findings"] += total
        entry["suppressed"] += suppressed
        if entry["total_findings"] > 0:
            entry["fp_rate"] = entry["suppressed"] / entry["total_findings"]

    def agent_fp_rate(self, agent_name: str) -> float:
        """Get false positive rate for an agent. Returns 0.0 if unknown."""
        entry = self.agents.get(agent_name)
        if entry is None:
            return 0.0
        return entry.get("fp_rate", 0.0)

    def high_fp_agents(self, threshold: float = 0.4) -> list[str]:
        """Return agent names with FP rate above threshold."""
        return [
            name
            for name, entry in self.agents.items()
            if entry.get("fp_rate", 0.0) > threshold
        ]

    def to_dict(self) -> dict:
        """JSON-serializable representation."""
        return {
            "checks": self.checks,
            "agents": self.agents,
            "updated_at": self.updated_at,
        }
