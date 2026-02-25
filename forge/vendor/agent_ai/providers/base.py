"""Provider interface for AI backends."""

from __future__ import annotations

from typing import Any, Protocol, Type, TypeVar

from pydantic import BaseModel

from forge.vendor.agent_ai.types import AgentResponse

T = TypeVar("T", bound=BaseModel)


class ProviderClient(Protocol):
    """Protocol implemented by provider-specific clients."""

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        output_schema: Type[T] | None = None,
        max_retries: int | None = None,
        max_budget_usd: float | None = None,
        permission_mode: str | None = None,
        env: dict[str, str] | None = None,
        log_file: str | None = None,
    ) -> AgentResponse[T]:
        ...


def make_empty_response(is_error: bool = True) -> AgentResponse[Any]:
    """Small helper for providers that need a safe fallback value."""
    from forge.vendor.agent_ai.types import Metrics

    return AgentResponse(
        result=None,
        parsed=None,
        messages=[],
        metrics=Metrics(
            duration_ms=0,
            duration_api_ms=0,
            num_turns=0,
            total_cost_usd=None,
            usage=None,
            session_id="",
        ),
        is_error=is_error,
    )
