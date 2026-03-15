"""Real-time telemetry for FORGE runs with cost + time circuit breakers.

Writes to disk after every update so state survives crashes.
Readable by external tools (MCP server, CLI status, monitoring) at any time.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentStatus:
    """Current state of a single agent invocation."""
    agent_name: str
    model: str
    status: str  # "running" | "completed" | "failed" | "killed"
    started_at: float
    completed_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finding_id: str = ""
    error: str = ""


class RunTelemetry:
    """Real-time telemetry for a FORGE run. Always accessible, always current.

    Writes to disk after every update so state survives crashes.
    Readable by external tools (MCP server, CLI status, monitoring) at any time.
    """

    def __init__(
        self,
        artifacts_dir: str,
        max_cost_usd: float = 5.0,
        max_duration_seconds: float = 1800.0,  # 30 min default
    ):
        self._dir = Path(artifacts_dir) / "telemetry"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._start_time = time.time()

        # Budget limits
        self.max_cost_usd = max_cost_usd
        self.max_duration_seconds = max_duration_seconds

        # Cumulative counters (updated after every LLM call)
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_invocations: int = 0
        self.failed_invocations: int = 0

        # Per-agent breakdown
        self.cost_by_agent: dict[str, float] = {}
        self.cost_by_model: dict[str, float] = {}
        self.tokens_by_agent: dict[str, int] = {}

        # Active agents (currently running)
        self.active_agents: dict[str, AgentStatus] = {}

        # Phase tracking
        self.current_phase: str = "initializing"
        self.phases_completed: list[str] = []

        # Findings progress
        self.total_findings: int = 0
        self.findings_fixed: int = 0
        self.findings_deferred: int = 0
        self.findings_in_progress: int = 0

        # Write initial state
        self._flush()

    # ── Circuit Breakers ──────────────────────────────────────────

    def check_budget(self) -> None:
        """Raise immediately if cost or time budget exceeded."""
        if self.total_cost_usd >= self.max_cost_usd:
            self._flush()
            raise CostLimitExceeded(
                f"BUDGET EXCEEDED: ${self.total_cost_usd:.2f} >= "
                f"${self.max_cost_usd:.2f} limit. Run killed."
            )
        elapsed = time.time() - self._start_time
        if elapsed >= self.max_duration_seconds:
            self._flush()
            raise TimeLimitExceeded(
                f"TIME EXCEEDED: {elapsed:.0f}s >= "
                f"{self.max_duration_seconds:.0f}s limit. Run killed."
            )

    # ── Recording (called after every LLM call) ──────────────────

    async def record_invocation(
        self,
        agent_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        success: bool = True,
        finding_id: str = "",
    ) -> None:
        """Record a single LLM call. Updates all counters and flushes to disk."""
        async with self._lock:
            self.total_cost_usd += cost_usd
            self.total_tokens += input_tokens + output_tokens
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_invocations += 1
            if not success:
                self.failed_invocations += 1

            self.cost_by_agent[agent_name] = (
                self.cost_by_agent.get(agent_name, 0) + cost_usd
            )
            self.cost_by_model[model] = (
                self.cost_by_model.get(model, 0) + cost_usd
            )
            self.tokens_by_agent[agent_name] = (
                self.tokens_by_agent.get(agent_name, 0) + input_tokens + output_tokens
            )

            # Append to invocations log (survives crashes)
            self._append_invocation({
                "ts": time.time(),
                "agent": agent_name,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "success": success,
                "finding_id": finding_id,
                "cumulative_cost": self.total_cost_usd,
                "budget_remaining": self.max_cost_usd - self.total_cost_usd,
                "elapsed_seconds": time.time() - self._start_time,
            })

            # Flush summary to disk
            self._flush()

        # Check circuit breakers AFTER recording
        self.check_budget()

    # ── Agent lifecycle tracking ──────────────────────────────────

    def agent_started(self, agent_id: str, agent_name: str, model: str, finding_id: str = "") -> None:
        self.active_agents[agent_id] = AgentStatus(
            agent_name=agent_name,
            model=model,
            status="running",
            started_at=time.time(),
            finding_id=finding_id,
        )
        self._flush()

    def agent_completed(self, agent_id: str, cost_usd: float = 0) -> None:
        if agent_id in self.active_agents:
            self.active_agents[agent_id].status = "completed"
            self.active_agents[agent_id].completed_at = time.time()
            self.active_agents[agent_id].cost_usd = cost_usd
        self._flush()

    def agent_failed(self, agent_id: str, error: str = "") -> None:
        if agent_id in self.active_agents:
            self.active_agents[agent_id].status = "failed"
            self.active_agents[agent_id].completed_at = time.time()
            self.active_agents[agent_id].error = error
        self._flush()

    # ── Phase tracking ────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        if self.current_phase != "initializing":
            self.phases_completed.append(self.current_phase)
        self.current_phase = phase
        self._flush()

    def update_findings_progress(self, total: int = 0, fixed: int = 0, deferred: int = 0, in_progress: int = 0) -> None:
        if total:
            self.total_findings = total
        if fixed:
            self.findings_fixed = fixed
        if deferred:
            self.findings_deferred = deferred
        if in_progress:
            self.findings_in_progress = in_progress
        self._flush()

    # ── Snapshot (readable by external tools at any time) ─────────

    def snapshot(self) -> dict:
        """Current state as a dict. Safe to call from any thread/process."""
        elapsed = time.time() - self._start_time
        return {
            "status": "running",
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_human": f"{int(elapsed // 60)}m {int(elapsed % 60)}s",
            "budget": {
                "cost_spent": round(self.total_cost_usd, 4),
                "cost_limit": self.max_cost_usd,
                "cost_remaining": round(self.max_cost_usd - self.total_cost_usd, 4),
                "cost_percent": round(self.total_cost_usd / self.max_cost_usd * 100, 1) if self.max_cost_usd > 0 else 0,
                "time_spent": round(elapsed, 0),
                "time_limit": self.max_duration_seconds,
                "time_remaining": round(max(0, self.max_duration_seconds - elapsed), 0),
                "time_percent": round(elapsed / self.max_duration_seconds * 100, 1) if self.max_duration_seconds > 0 else 0,
            },
            "totals": {
                "invocations": self.total_invocations,
                "failed": self.failed_invocations,
                "tokens": self.total_tokens,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
            },
            "cost_by_agent": dict(sorted(self.cost_by_agent.items(), key=lambda x: -x[1])),
            "cost_by_model": dict(sorted(self.cost_by_model.items(), key=lambda x: -x[1])),
            "phase": self.current_phase,
            "phases_completed": self.phases_completed,
            "findings": {
                "total": self.total_findings,
                "fixed": self.findings_fixed,
                "deferred": self.findings_deferred,
                "in_progress": self.findings_in_progress,
            },
            "active_agents": [
                {
                    "name": a.agent_name,
                    "model": a.model,
                    "status": a.status,
                    "running_for": f"{time.time() - a.started_at:.0f}s",
                    "finding_id": a.finding_id,
                }
                for a in self.active_agents.values()
                if a.status == "running"
            ],
        }

    # ── Disk persistence ──────────────────────────────────────────

    def _flush(self) -> None:
        """Write current snapshot to disk. Called after every state change."""
        try:
            snapshot = self.snapshot()
            # Atomic write via temp file
            tmp = self._dir / "live_status.tmp"
            target = self._dir / "live_status.json"
            tmp.write_text(json.dumps(snapshot, indent=2))
            tmp.rename(target)
        except Exception:
            pass  # Never fail on telemetry write

    def _append_invocation(self, record: dict) -> None:
        """Append a single invocation to the JSONL log."""
        try:
            with open(self._dir / "invocations.jsonl", "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass


class CostLimitExceeded(Exception):
    """Hard cost cap hit. Run must stop immediately."""
    pass


class TimeLimitExceeded(Exception):
    """Hard time cap hit. Run must stop immediately."""
    pass


# ── Context var for async-safe access ────────────────────────────────

_current_run_telemetry: contextvars.ContextVar[RunTelemetry | None] = (
    contextvars.ContextVar("_current_run_telemetry", default=None)
)
