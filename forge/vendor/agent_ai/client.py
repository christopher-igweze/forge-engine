"""Provider-agnostic AI client facade.

Auto-instruments every LLM call via ``ForgeTelemetry.current()``:
if a telemetry context is active, ``AgentAI.run()`` automatically
extracts token counts and cost from the provider response and logs
them.  No manual instrumentation needed in reasoners.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Type

from pydantic import BaseModel

from forge.vendor.agent_ai.factory import build_provider_client
from forge.vendor.agent_ai.types import AgentResponse, Tool

logger = logging.getLogger(__name__)

DEFAULT_TOOLS: list[str] = [
    Tool.READ,
    Tool.WRITE,
    Tool.EDIT,
    Tool.BASH,
    Tool.GLOB,
    Tool.GREP,
]


@dataclass
class AgentAIConfig:
    """Configuration for AgentAI."""

    provider: Literal["claude", "codex", "opencode", "openrouter_direct", "openrouter_tools"] = "claude"
    codex_bin: str = "codex"
    opencode_bin: str = "opencode"
    model: str = "sonnet"
    cwd: str | Path = "."
    max_turns: int = 10
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    system_prompt: str | None = None
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    permission_mode: str | None = None
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    agent_name: str = ""


class AgentAI:
    """Async facade that dispatches requests to the selected provider client.

    If ``ForgeTelemetry.current()`` is active, every ``run()`` call
    automatically logs an invocation with token counts, cost, and latency.
    """

    def __init__(self, config: AgentAIConfig | None = None) -> None:
        self.config = config or AgentAIConfig()

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: str | Path | None = None,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        output_schema: Type[BaseModel] | None = None,
        max_retries: int | None = None,
        max_budget_usd: float | None = None,
        permission_mode: str | None = None,
        env: dict[str, str] | None = None,
        log_file: str | Path | None = None,
    ) -> AgentResponse[BaseModel]:
        provider_client = build_provider_client(self.config)
        response = await provider_client.run(
            prompt,
            model=model,
            cwd=cwd,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            output_schema=output_schema,
            max_retries=max_retries,
            max_budget_usd=max_budget_usd,
            permission_mode=permission_mode,
            env=env,
            log_file=log_file,
        )

        # ── Auto-instrument: log to active telemetry context ────────
        _auto_log_invocation(self.config, response)

        return response


# ── Auto-instrumentation ────────────────────────────────────────────


def _auto_log_invocation(
    config: AgentAIConfig,
    response: AgentResponse,
) -> None:
    """Extract metrics from an AgentResponse and log to active telemetry.

    This is the key wiring that connects every LLM call to the telemetry
    system.  If no telemetry context is active, this is a no-op.
    """
    from forge.execution.telemetry import ForgeTelemetry

    telemetry = ForgeTelemetry.current()
    if telemetry is None:
        return

    metrics = response.metrics
    usage = metrics.usage or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Some providers report total_tokens but not the breakdown
    if not input_tokens and not output_tokens:
        total = usage.get("total_tokens", 0)
        if total:
            # Rough split: assume 70/30 input/output
            input_tokens = int(total * 0.7)
            output_tokens = total - input_tokens

    telemetry.log_invocation(
        agent_name=config.agent_name or "unknown",
        model=config.model,
        provider=config.provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=metrics.duration_ms,
        success=not response.is_error,
        error=str(response.messages[-1].error) if response.is_error and response.messages else "",
    )


# Backward-compatible aliases retained during migration.
ClaudeAI = AgentAI
ClaudeAIConfig = AgentAIConfig
