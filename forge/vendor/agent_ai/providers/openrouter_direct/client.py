"""OpenRouter direct provider — stdlib-only HTTPS client for planning agents.

This provider calls OpenRouter's Chat Completions endpoint directly over HTTPS
using only Python stdlib (``urllib.request``). No subprocess, no CLI, no extra
deps. Intended for planning-only phases (PM, Architect, Sprint Planner, Tech
Lead) that just need text output and don't use file-editing tools.

The ``OPENROUTER_API_KEY`` is read from the merged env at call time:
    ``{os.environ, config.env}``
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Type, TypeVar

from pydantic import BaseModel

from forge.vendor.agent_ai.types import (
    AgentResponse,
    Message,
    Metrics,
    TextContent,
)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Schema helpers — flatten $ref/$defs into a concrete example for the model
# ---------------------------------------------------------------------------


def _resolve_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve all $ref/$defs in a JSON schema into a flat, self-contained schema."""
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g. "#/$defs/ArchitectureComponent"
                ref_name = ref_path.rsplit("/", 1)[-1]
                if ref_name in defs:
                    return _resolve(defs[ref_name])
                return node
            return {k: _resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    resolved = _resolve(schema)
    resolved.pop("$defs", None)
    return resolved


def _schema_to_example(schema: dict[str, Any]) -> dict[str, Any]:
    """Generate a concrete example JSON object from a resolved JSON schema."""
    defs = schema.get("$defs", {})

    def _example_for(node: dict[str, Any]) -> Any:
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            if ref_name in defs:
                return _example_for(defs[ref_name])
            return {}

        node_type = node.get("type", "string")

        if node_type == "object":
            props = node.get("properties", {})
            return {k: _example_for(v) for k, v in props.items()}
        elif node_type == "array":
            items = node.get("items", {"type": "string"})
            return [_example_for(items)]
        elif node_type == "boolean":
            return node.get("default", True)
        elif node_type == "integer":
            return node.get("default", 1)
        elif node_type == "number":
            return node.get("default", 1.0)
        else:  # string
            default = node.get("default")
            if default is not None:
                return default
            title = node.get("title", "value")
            return f"<{title.lower()}>"

    return _example_for(schema)


def _build_schema_prompt(schema_class: type) -> str:
    """Build a clear prompt describing the expected JSON structure with an example."""
    raw_schema = schema_class.model_json_schema()
    resolved = _resolve_refs(raw_schema)
    example = _schema_to_example(raw_schema)

    # Build a clean field description from the resolved schema
    required = set(resolved.get("required", []))
    props = resolved.get("properties", {})
    field_lines = []
    for name, prop in props.items():
        ftype = prop.get("type", "string")
        req = "(REQUIRED)" if name in required else "(optional)"
        if ftype == "array":
            items = prop.get("items", {})
            item_type = items.get("type", "object")
            if item_type == "object":
                sub_props = items.get("properties", {})
                sub_fields = ", ".join(f'"{k}": {v.get("type", "string")}' for k, v in sub_props.items())
                field_lines.append(f'  "{name}": array of objects with {{{sub_fields}}} {req}')
            else:
                field_lines.append(f'  "{name}": array of {item_type}s {req}')
        elif ftype == "object":
            field_lines.append(f'  "{name}": object {req}')
        else:
            field_lines.append(f'  "{name}": {ftype} {req}')

    fields_desc = "\n".join(field_lines)
    example_json = json.dumps(example, indent=2)

    return (
        f"IMPORTANT — STRUCTURED OUTPUT REQUIREMENT:\n"
        f"You MUST respond with a single valid JSON object with these fields:\n"
        f"{fields_desc}\n\n"
        f"Example of the expected format:\n"
        f"```json\n{example_json}\n```\n\n"
        f"Respond with ONLY the raw JSON object — no markdown fences, "
        f"no explanation, no extra text. Just the JSON."
    )


def _normalize_json(data: Any, schema_class: type) -> Any:
    """Normalize model output to match the expected Pydantic schema.

    Handles common LLM quirks:
      1. Returns a bare array when a wrapper object with a list field is expected
      2. Serializes nested objects as JSON strings instead of dicts
    """
    # Quirk 1: bare array → wrap into object if schema has a primary list field
    if isinstance(data, list):
        schema = schema_class.model_json_schema()
        props = schema.get("properties", {})
        list_fields = [k for k, v in props.items() if v.get("type") == "array"]
        if len(list_fields) >= 1:
            # Use the first list field as the primary one
            primary = list_fields[0]
            wrapped = {primary: data}
            # Add defaults for other required fields
            for req in schema.get("required", []):
                if req not in wrapped:
                    prop_type = props.get(req, {}).get("type", "string")
                    if prop_type == "string":
                        wrapped[req] = ""
                    elif prop_type == "boolean":
                        wrapped[req] = False
                    elif prop_type == "array":
                        wrapped[req] = []
            data = wrapped

    # Quirk 2: recursively deserialize JSON strings that should be objects
    if isinstance(data, dict):
        data = {k: _try_deserialize_nested(v) for k, v in data.items()}
    elif isinstance(data, list):
        data = [_try_deserialize_nested(item) for item in data]

    return data


def _try_deserialize_nested(value: Any) -> Any:
    """If value is a JSON string that parses to a dict/list, return the parsed version."""
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    if isinstance(value, dict):
        return {k: _try_deserialize_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_try_deserialize_nested(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Transient error detection (same patterns as other providers)
# ---------------------------------------------------------------------------

_TRANSIENT_PATTERNS = frozenset(
    {
        "rate limit",
        "rate_limit",
        "overloaded",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "service unavailable",
        "503",
        "502",
        "504",
        "internal server error",
        "500",
    }
)


def _is_transient(error: str) -> bool:
    low = error.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


# ---------------------------------------------------------------------------
# Logging helpers (identical pattern to other providers)
# ---------------------------------------------------------------------------


def _write_log(fh: IO[str], event: str, **data: Any) -> None:
    entry = {"ts": time.time(), "event": event, **data}
    fh.write(json.dumps(entry, default=str) + "\n")
    fh.flush()


def _open_log(log_file: str | Path | None) -> IO[str] | None:
    if log_file is None:
        return None
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP helper (stdlib-only, sync — called via run_in_executor)
# ---------------------------------------------------------------------------


def _http_post_sync(
    url: str,
    payload: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    """Synchronous HTTP POST to OpenRouter. Raises on non-2xx status."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/livekit/clarity-check",
            "X-Title": "SWE-AF Planning Agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenRouter HTTP {exc.code}: {exc.reason} — {raw[:500]}"
        ) from exc


# ---------------------------------------------------------------------------
# Config & Client
# ---------------------------------------------------------------------------


@dataclass
class OpenrouterDirectConfig:
    """Configuration for the OpenRouter direct provider."""

    model: str = "minimax/minimax-m2.5"
    base_url: str = "https://openrouter.ai/api/v1"
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    system_prompt: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # The following fields are accepted for interface compatibility but unused:
    # cwd, max_turns, allowed_tools, permission_mode, max_budget_usd


class OpenrouterDirectClient:
    """Async client that calls OpenRouter Chat Completions directly over HTTPS.

    Implements the ``ProviderClient`` protocol. Suitable for planning agents
    that only need text-in / JSON-out (no file-editing tools).
    """

    def __init__(self, config: OpenrouterDirectConfig | None = None) -> None:
        self.config = config or OpenrouterDirectConfig()

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: str | None = None,          # accepted, unused
        max_turns: int | None = None,     # accepted, unused
        allowed_tools: list[str] | None = None,  # accepted, unused
        system_prompt: str | None = None,
        output_schema: Type[T] | None = None,
        max_retries: int | None = None,
        max_budget_usd: float | None = None,  # accepted, unused
        permission_mode: str | None = None,   # accepted, unused
        env: dict[str, str] | None = None,
        log_file: str | None = None,
    ) -> AgentResponse[T]:
        cfg = self.config
        # Normalise: the opencode CLI uses "openrouter/<model>" as a routing prefix
        # but the real OpenRouter API expects just "<provider>/<model>" without the leading "openrouter/" segment.
        raw_model = model or cfg.model
        effective_model = raw_model.removeprefix("openrouter/")
        effective_env = {**cfg.env, **(env or {})}
        effective_system = system_prompt or cfg.system_prompt
        effective_retries = max_retries if max_retries is not None else cfg.max_retries

        # Resolve API key: call-level env > process env
        merged_env = {**os.environ, **effective_env}
        api_key = merged_env.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. "
                "Pass it via the env argument or set it in the process environment."
            )

        log_fh = _open_log(log_file)
        try:
            return await self._run_with_retries(
                prompt=prompt,
                model=effective_model,
                system_prompt=effective_system,
                output_schema=output_schema,
                api_key=api_key,
                effective_retries=effective_retries,
                log_fh=log_fh,
            )
        finally:
            if log_fh:
                log_fh.close()

    async def _run_with_retries(
        self,
        *,
        prompt: str,
        model: str,
        system_prompt: str | None,
        output_schema: Type[T] | None,
        api_key: str,
        effective_retries: int,
        log_fh: IO[str] | None,
    ) -> AgentResponse[T]:
        cfg = self.config
        delay = cfg.initial_delay
        last_exc: Exception | None = None

        if log_fh:
            _write_log(log_fh, "start", prompt=prompt[:200], model=model)

        for attempt in range(effective_retries + 1):
            try:
                response = await self._execute(
                    prompt=prompt,
                    model=model,
                    system_prompt=system_prompt,
                    output_schema=output_schema,
                    api_key=api_key,
                    log_fh=log_fh,
                )
                if log_fh:
                    _write_log(
                        log_fh,
                        "end",
                        is_error=response.is_error,
                        num_turns=response.metrics.num_turns,
                    )
                return response

            except Exception as exc:
                last_exc = exc
                if attempt < effective_retries and _is_transient(str(exc)):
                    if log_fh:
                        _write_log(
                            log_fh,
                            "retry",
                            attempt=attempt + 1,
                            error=str(exc),
                            delay=delay,
                        )
                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay)
                    continue
                if log_fh:
                    _write_log(log_fh, "end", is_error=True, error=str(exc))
                raise

        raise last_exc  # type: ignore[misc]

    async def _execute(
        self,
        *,
        prompt: str,
        model: str,
        system_prompt: str | None,
        output_schema: Type[T] | None,
        api_key: str,
        log_fh: IO[str] | None,
    ) -> AgentResponse[T]:
        """Execute one call to the OpenRouter Chat Completions endpoint."""
        start = time.time()

        # When a schema is required, inject it into the prompt so the model
        # knows exactly what JSON structure to produce.
        effective_prompt = prompt
        if output_schema is not None:
            schema_suffix = _build_schema_prompt(output_schema)
            effective_prompt = prompt + "\n\n---\n" + schema_suffix

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": effective_prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        # Request JSON output when a schema is provided
        if output_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"

        # Run the blocking HTTP call in a thread so as not to block the event loop
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: _http_post_sync(url, payload, api_key)
        )

        duration_ms = int((time.time() - start) * 1000)

        # Extract text from the response
        choices = data.get("choices") or []
        text: str | None = None
        if choices:
            text = choices[0].get("message", {}).get("content") or None

        # Usage metrics
        usage_data = data.get("usage") or {}
        total_tokens = usage_data.get("total_tokens")

        metrics = Metrics(
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=1,
            total_cost_usd=None,   # OpenRouter doesn't return cost in this field
            usage=usage_data or None,
            session_id="",
        )

        msg = Message(
            role="assistant",
            content=[TextContent(text=text)] if text else [],
            model=model,
            error=None,
            parent_tool_use_id=None,
        )

        if log_fh:
            _write_log(
                log_fh,
                "result",
                num_turns=1,
                duration_ms=duration_ms,
                total_tokens=total_tokens,
            )

        # Parse structured output if schema was requested
        parsed: T | None = None
        if output_schema is not None and text:
            raw = text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                lines = raw.split("\n", 1)
                raw = lines[1] if len(lines) > 1 else raw
                if raw.endswith("```"):
                    raw = raw[: -len("```")]
                raw = raw.strip()

            # Strategy 1: parse as JSON string directly
            try:
                parsed = output_schema.model_validate_json(raw)
            except Exception as e1:
                # Strategy 2: parse JSON, normalize quirks, validate as dict
                try:
                    data = json.loads(raw)
                    data = _normalize_json(data, output_schema)
                    parsed = output_schema.model_validate(data)
                except Exception as e2:
                    if log_fh:
                        _write_log(
                            log_fh,
                            "parse_error",
                            schema=output_schema.__name__,
                            error_json=str(e1)[:300],
                            error_dict=str(e2)[:300],
                            response_preview=raw[:500],
                        )

        return AgentResponse(
            result=text,
            parsed=parsed,
            messages=[msg],
            metrics=metrics,
            is_error=False,
        )
