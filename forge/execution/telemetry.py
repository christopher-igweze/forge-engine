"""Cost monitoring and training data logging for FORGE.

Every agent invocation logs: model used, input/output tokens, cost,
latency, success/failure. Training data logs capture finding->fix
patterns for the fine-tuning flywheel.

The ``ForgeTelemetry`` class supports a **context-var backed singleton**
so any code in the async call-chain can access the active instance via
``ForgeTelemetry.current()`` without explicit plumbing.  This is the
"microservice" pattern: set up telemetry at the pipeline entry point and
every agent invocation is automatically captured.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Generator

logger = logging.getLogger(__name__)


# ── Per-model pricing (USD per million tokens) ──────────────────────

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_mtok, output_per_mtok)
    "minimax/minimax-m2.5": (0.30, 1.20),
    "deepseek/deepseek-v3.2": (0.28, 0.40),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
}

DEFAULT_PRICING = (1.00, 5.00)  # Fallback for unknown models

# ── Context var for async-safe singleton ─────────────────────────────

_current_telemetry: contextvars.ContextVar[ForgeTelemetry | None] = (
    contextvars.ContextVar("_current_telemetry", default=None)
)

# Module-level fallback for when contextvars are lost across RPC boundaries
# (e.g. AgentField dispatches agents via HTTP, creating new async contexts).
_module_telemetry: ForgeTelemetry | None = None


@dataclass
class AgentInvocationLog:
    """A single agent invocation record."""

    agent_name: str
    model: str
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = True
    error: str = ""
    finding_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class TrainingDataEntry:
    """A finding->fix pair for the training data flywheel."""

    finding_id: str
    finding_category: str
    finding_severity: str
    finding_title: str
    finding_description: str
    tier: int
    fix_outcome: str
    fix_summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    retry_count: int = 0
    escalated: bool = False
    model_used: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ForgeTelemetry:
    """Collects cost and training data during a FORGE run.

    Data is accumulated in memory and flushed to artifacts_dir at
    the end of the run.

    **Singleton access:** Use ``ForgeTelemetry.current()`` to get the
    active instance from anywhere in the async call-chain.  Activate
    via the ``activate()`` context manager at the pipeline entry point.
    """

    def __init__(
        self,
        artifacts_dir: str = "",
        run_id: str = "",
        stream_log_path: str = "",
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.run_id = run_id
        self.stream_log_path = stream_log_path
        self.invocations: list[AgentInvocationLog] = []
        self.training_data: list[TrainingDataEntry] = []
        self._start_time = time.monotonic()

    # ── Context-var singleton API ────────────────────────────────────

    @staticmethod
    def current() -> ForgeTelemetry | None:
        """Return the active telemetry instance, or None.

        Checks the contextvar first, then falls back to the module-level
        instance (set when running under AgentField where contextvars
        are lost across RPC boundaries).
        """
        return _current_telemetry.get() or _module_telemetry

    @contextmanager
    def activate(self) -> Generator[ForgeTelemetry, None, None]:
        """Activate this instance as the current telemetry context.

        Sets both the contextvar (for same-process calls) and the
        module-level fallback (for AgentField RPC calls where contextvars
        are lost).

        Usage::

            telemetry = ForgeTelemetry(run_id="abc")
            with telemetry.activate():
                # Any code here (including AgentAI.run()) will
                # auto-log invocations to this instance.
                await run_standalone(...)
        """
        global _module_telemetry
        prev_module = _module_telemetry
        _module_telemetry = self
        token = _current_telemetry.set(self)
        try:
            yield self
        finally:
            _current_telemetry.reset(token)
            _module_telemetry = prev_module

    # ── Logging API ──────────────────────────────────────────────────

    def log_invocation(
        self,
        agent_name: str,
        model: str,
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        success: bool = True,
        error: str = "",
        finding_id: str = "",
    ) -> AgentInvocationLog:
        """Log a single agent invocation with cost calculation."""
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        cost = (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000

        entry = AgentInvocationLog(
            agent_name=agent_name,
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            success=success,
            error=error,
            finding_id=finding_id,
        )
        self.invocations.append(entry)

        logger.debug(
            "Telemetry: %s | %s | %d+%d tok | $%.6f | %dms",
            agent_name, model, input_tokens, output_tokens, entry.cost_usd, latency_ms,
        )

        # Stream each invocation to disk immediately so costs survive failures
        if self.stream_log_path:
            try:
                row = asdict(entry)
                row["running_total_usd"] = self.total_cost
                with open(self.stream_log_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass  # non-fatal

        return entry

    def log_training_pair(
        self,
        finding_id: str,
        category: str,
        severity: str,
        title: str,
        description: str,
        tier: int,
        outcome: str,
        summary: str = "",
        files_changed: list[str] | None = None,
        retry_count: int = 0,
        escalated: bool = False,
        model_used: str = "",
    ) -> None:
        """Log a finding->fix pair for the training data flywheel."""
        entry = TrainingDataEntry(
            finding_id=finding_id,
            finding_category=category,
            finding_severity=severity,
            finding_title=title,
            finding_description=description,
            tier=tier,
            fix_outcome=outcome,
            fix_summary=summary,
            files_changed=files_changed or [],
            retry_count=retry_count,
            escalated=escalated,
            model_used=model_used,
        )
        self.training_data.append(entry)

    @property
    def total_cost(self) -> float:
        """Total cost of all invocations."""
        return round(sum(i.cost_usd for i in self.invocations), 4)

    @property
    def total_tokens(self) -> int:
        """Total tokens across all invocations."""
        return sum(i.input_tokens + i.output_tokens for i in self.invocations)

    def summary(self) -> dict[str, Any]:
        """Generate a cost summary."""
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)

        by_agent: dict[str, float] = {}
        by_model: dict[str, float] = {}
        for inv in self.invocations:
            by_agent[inv.agent_name] = by_agent.get(inv.agent_name, 0) + inv.cost_usd
            by_model[inv.model] = by_model.get(inv.model, 0) + inv.cost_usd

        success_count = sum(1 for i in self.invocations if i.success)
        fail_count = sum(1 for i in self.invocations if not i.success)

        return {
            "run_id": self.run_id,
            "total_cost_usd": self.total_cost,
            "total_tokens": self.total_tokens,
            "total_invocations": len(self.invocations),
            "successful_invocations": success_count,
            "failed_invocations": fail_count,
            "elapsed_ms": elapsed_ms,
            "cost_by_agent": {k: round(v, 4) for k, v in sorted(by_agent.items())},
            "cost_by_model": {k: round(v, 4) for k, v in sorted(by_model.items())},
            "training_pairs_logged": len(self.training_data),
        }

    def flush(self) -> None:
        """Write collected data to artifacts_dir."""
        if not self.artifacts_dir:
            logger.warning("No artifacts_dir set -- skipping telemetry flush")
            return

        try:
            telemetry_dir = os.path.join(self.artifacts_dir, "telemetry")
            os.makedirs(telemetry_dir, exist_ok=True)

            # Cost summary
            summary_path = os.path.join(telemetry_dir, "cost_summary.json")
            _write_json(summary_path, self.summary())

            # Full invocation log
            invocations_path = os.path.join(telemetry_dir, "invocations.jsonl")
            with open(invocations_path, "w") as f:
                for inv in self.invocations:
                    f.write(json.dumps(asdict(inv)) + "\n")

            # Training data
            if self.training_data:
                training_path = os.path.join(telemetry_dir, "training_data.jsonl")
                with open(training_path, "w") as f:
                    for entry in self.training_data:
                        f.write(json.dumps(asdict(entry)) + "\n")

            logger.info(
                "Telemetry flushed: %d invocations, $%.4f total, %d training pairs",
                len(self.invocations), self.total_cost, len(self.training_data),
            )
        except OSError as e:
            logger.warning("Telemetry flush failed (non-fatal): %s", e)


def _write_json(path: str, data: Any) -> None:
    """Write JSON to a file."""
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except OSError as e:
        logger.warning("Failed to write telemetry file %s: %s", path, e)
