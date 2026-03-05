"""OpenRouter tools provider — stdlib-only HTTPS with native function calling.

Extends the openrouter_direct pattern (same HTTP plumbing via ``urllib.request``)
with OpenAI-compatible tool calling. Intended for coding agents that need
Read/Write/Edit/Bash/Glob/Grep tools but can't use the opencode CLI
(which is optimized for Anthropic models).

Multi-turn loop: call model → parse tool_calls → execute locally → feed
results back → repeat until model stops calling tools or max_turns reached.

Token tracking: extracts ``usage.prompt_tokens`` / ``completion_tokens``
from each API response and accumulates across turns.
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob as glob_module
import json
import logging
import os
import re
import subprocess
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
    ToolResultContent,
    ToolUseContent,
)

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI-compatible function calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute file path to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute file path to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in a file. The old_text must match exactly "
                "(including whitespace/indentation). Use for surgical edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute file path to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the repository directory. "
                "Use for running tests, installing dependencies, git operations, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files matching a glob pattern in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in (default: repo root).",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob (e.g. '*.py').",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Local tool execution
# ---------------------------------------------------------------------------

def _resolve_path(path: str, cwd: str) -> str:
    """Resolve a path relative to the working directory."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p.resolve())


def _execute_tool(name: str, args: dict[str, Any], cwd: str) -> str:
    """Execute a tool locally and return the result as a string."""
    try:
        if name == "read_file":
            fpath = _resolve_path(args["path"], cwd)
            try:
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                # Truncate very large files
                if len(content) > 100_000:
                    content = content[:100_000] + "\n... [truncated at 100K chars]"
                return content
            except FileNotFoundError:
                return f"Error: File not found: {fpath}"
            except IsADirectoryError:
                return f"Error: Path is a directory, not a file: {fpath}"

        elif name == "write_file":
            fpath = _resolve_path(args["path"], cwd)
            Path(fpath).parent.mkdir(parents=True, exist_ok=True)
            Path(fpath).write_text(args["content"], encoding="utf-8")
            return f"File written: {fpath} ({len(args['content'])} chars)"

        elif name == "edit_file":
            fpath = _resolve_path(args["path"], cwd)
            try:
                content = Path(fpath).read_text(encoding="utf-8")
            except FileNotFoundError:
                return f"Error: File not found: {fpath}"

            old_text = args["old_text"]
            new_text = args["new_text"]
            count = content.count(old_text)
            if count == 0:
                return f"Error: old_text not found in {fpath}. Make sure it matches exactly."
            if count > 1:
                return f"Error: old_text found {count} times in {fpath}. Provide more context to make it unique."
            content = content.replace(old_text, new_text, 1)
            Path(fpath).write_text(content, encoding="utf-8")
            return f"File edited: {fpath} (replaced 1 occurrence)"

        elif name == "bash":
            command = args["command"]
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                output = ""
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    output += ("\n" if output else "") + result.stderr
                if result.returncode != 0:
                    output += f"\n[exit code: {result.returncode}]"
                if not output.strip():
                    output = f"[completed with exit code {result.returncode}]"
                # Truncate large outputs
                if len(output) > 50_000:
                    output = output[:50_000] + "\n... [truncated at 50K chars]"
                return output
            except subprocess.TimeoutExpired:
                return "Error: Command timed out after 120 seconds."

        elif name == "glob_files":
            pattern = args["pattern"]
            matches = sorted(glob_module.glob(
                os.path.join(cwd, pattern), recursive=True,
            ))
            # Make paths relative to cwd
            rel = [os.path.relpath(m, cwd) for m in matches]
            if not rel:
                return "No files matched the pattern."
            if len(rel) > 200:
                rel = rel[:200]
                rel.append(f"... and more ({len(matches)} total)")
            return "\n".join(rel)

        elif name == "grep_files":
            pattern = args["pattern"]
            search_path = _resolve_path(args.get("path", "."), cwd)
            file_glob = args.get("glob", "")

            matches: list[str] = []
            search_root = Path(search_path)

            if search_root.is_file():
                files = [search_root]
            else:
                files = sorted(search_root.rglob("*"))

            regex = re.compile(pattern, re.IGNORECASE)
            for fpath in files:
                if not fpath.is_file():
                    continue
                if file_glob and not fnmatch.fnmatch(fpath.name, file_glob):
                    continue
                # Skip binary/large files
                try:
                    if fpath.stat().st_size > 1_000_000:
                        continue
                except OSError:
                    continue
                try:
                    lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, cwd)
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 100:
                                break
                except (OSError, UnicodeDecodeError):
                    continue
                if len(matches) >= 100:
                    break

            if not matches:
                return "No matches found."
            result = "\n".join(matches)
            if len(matches) >= 100:
                result += "\n... [results truncated at 100 matches]"
            return result

        else:
            return f"Error: Unknown tool '{name}'"

    except Exception as e:
        return f"Error executing {name}: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Schema helpers (reused from openrouter_direct)
# ---------------------------------------------------------------------------

def _resolve_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].rsplit("/", 1)[-1]
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
    defs = schema.get("$defs", {})

    def _example_for(node: dict[str, Any]) -> Any:
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            if ref_name in defs:
                return _example_for(defs[ref_name])
            return {}
        node_type = node.get("type", "string")
        if node_type == "object":
            return {k: _example_for(v) for k, v in node.get("properties", {}).items()}
        elif node_type == "array":
            return [_example_for(node.get("items", {"type": "string"}))]
        elif node_type == "boolean":
            return node.get("default", True)
        elif node_type == "integer":
            return node.get("default", 1)
        elif node_type == "number":
            return node.get("default", 1.0)
        else:
            default = node.get("default")
            if default is not None:
                return default
            title = node.get("title", "value")
            return f"<{title.lower()}>"

    return _example_for(schema)


def _build_schema_prompt(schema_class: type) -> str:
    raw_schema = schema_class.model_json_schema()
    resolved = _resolve_refs(raw_schema)
    example = _schema_to_example(raw_schema)

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
                sub_fields = ", ".join(
                    f'"{k}": {v.get("type", "string")}' for k, v in sub_props.items()
                )
                field_lines.append(
                    f'  "{name}": array of objects with {{{sub_fields}}} {req}'
                )
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
    if isinstance(data, list):
        schema = schema_class.model_json_schema()
        props = schema.get("properties", {})
        list_fields = [k for k, v in props.items() if v.get("type") == "array"]
        if len(list_fields) >= 1:
            primary = list_fields[0]
            wrapped = {primary: data}
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

    if isinstance(data, dict):
        data = {k: _try_deser(v) for k, v in data.items()}
    elif isinstance(data, list):
        data = [_try_deser(item) for item in data]

    return data


def _try_deser(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    if isinstance(value, dict):
        return {k: _try_deser(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_try_deser(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Transient error detection
# ---------------------------------------------------------------------------

_TRANSIENT_PATTERNS = frozenset({
    "rate limit", "rate_limit", "overloaded", "timeout", "timed out",
    "connection reset", "connection refused", "temporarily unavailable",
    "service unavailable", "503", "502", "504", "internal server error", "500",
})


def _is_transient(error: str) -> bool:
    low = error.lower()
    return any(p in low for p in _TRANSIENT_PATTERNS)


# ---------------------------------------------------------------------------
# Logging helpers
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
# HTTP helper (stdlib-only)
# ---------------------------------------------------------------------------

def _http_post_sync(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: int = 180,
) -> dict[str, Any]:
    """Synchronous HTTP POST to OpenRouter. Raises on non-2xx."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/antigravity-ai/forge-engine",
            "X-Title": "FORGE Coding Agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
class OpenrouterToolsConfig:
    """Configuration for the OpenRouter tools provider."""

    model: str = "minimax/minimax-m2.5"
    base_url: str = "https://openrouter.ai/api/v1"
    max_retries: int = 3
    max_tool_turns: int = 25
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    system_prompt: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    bash_timeout: int = 120


class OpenrouterToolsClient:
    """Async client that calls OpenRouter with native function calling.

    Implements the ``ProviderClient`` protocol. Suitable for coding agents
    that need file-editing tools (Read/Write/Edit/Bash/Glob/Grep).

    Multi-turn loop:
      1. Send messages + tools to OpenRouter
      2. If response contains tool_calls, execute them locally
      3. Append tool results and repeat
      4. When model responds without tool_calls → done
    """

    def __init__(self, config: OpenrouterToolsConfig | None = None) -> None:
        self.config = config or OpenrouterToolsConfig()

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
        cfg = self.config
        raw_model = model or cfg.model
        effective_model = raw_model.removeprefix("openrouter/")
        effective_env = {**cfg.env, **(env or {})}
        effective_system = system_prompt or cfg.system_prompt
        effective_retries = max_retries if max_retries is not None else cfg.max_retries
        effective_cwd = str(cwd) if cwd else "."
        effective_max_turns = max_turns if max_turns is not None else cfg.max_tool_turns

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
                cwd=effective_cwd,
                max_turns=effective_max_turns,
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
        cwd: str,
        max_turns: int,
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
                response = await self._execute_tool_loop(
                    prompt=prompt,
                    model=model,
                    system_prompt=system_prompt,
                    output_schema=output_schema,
                    api_key=api_key,
                    cwd=cwd,
                    max_turns=max_turns,
                    log_fh=log_fh,
                )
                if log_fh:
                    _write_log(
                        log_fh, "end",
                        is_error=response.is_error,
                        num_turns=response.metrics.num_turns,
                    )
                return response

            except Exception as exc:
                last_exc = exc
                if attempt < effective_retries and _is_transient(str(exc)):
                    if log_fh:
                        _write_log(
                            log_fh, "retry",
                            attempt=attempt + 1, error=str(exc), delay=delay,
                        )
                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay)
                    continue
                if log_fh:
                    _write_log(log_fh, "end", is_error=True, error=str(exc))
                raise

        raise last_exc  # type: ignore[misc]

    async def _execute_tool_loop(
        self,
        *,
        prompt: str,
        model: str,
        system_prompt: str | None,
        output_schema: Type[T] | None,
        api_key: str,
        cwd: str,
        max_turns: int,
        log_fh: IO[str] | None,
    ) -> AgentResponse[T]:
        """Multi-turn tool calling loop."""
        start = time.time()

        # Build initial messages
        effective_prompt = prompt
        if output_schema is not None:
            schema_suffix = _build_schema_prompt(output_schema)
            effective_prompt = (
                prompt
                + "\n\n---\n"
                + "When you have completed all tool calls and are ready to give "
                + "your final answer, respond with ONLY the JSON below (no tool calls).\n\n"
                + schema_suffix
            )

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": effective_prompt})

        # Accumulate metrics across turns
        total_input_tokens = 0
        total_output_tokens = 0
        all_tool_uses: list[ToolUseContent] = []
        all_tool_results: list[ToolResultContent] = []
        num_turns = 0
        final_text: str | None = None

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        loop = asyncio.get_event_loop()

        for turn in range(max_turns):
            num_turns += 1

            # Build API payload
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
            }

            # On the last turn, don't offer tools — force a text response
            if turn == max_turns - 1:
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                if output_schema is not None:
                    payload["response_format"] = {"type": "json_object"}

            if log_fh:
                _write_log(log_fh, "turn", turn=turn, messages_count=len(messages))

            # Make the API call
            data = await loop.run_in_executor(
                None, lambda: _http_post_sync(url, payload, api_key)
            )

            # Extract usage
            usage = data.get("usage") or {}
            total_input_tokens += usage.get("prompt_tokens", 0)
            total_output_tokens += usage.get("completion_tokens", 0)

            # Parse response
            choices = data.get("choices") or []
            if not choices:
                final_text = ""
                break

            choice = choices[0]
            finish_reason = choice.get("finish_reason", "stop")
            message = choice.get("message", {})

            # Check for tool calls
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # Model is done — extract final text
                final_text = message.get("content") or ""
                # Append assistant message to history
                messages.append({"role": "assistant", "content": final_text})
                break

            # Process tool calls
            # Append the assistant message (with tool_calls) to history
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if message.get("content"):
                assistant_msg["content"] = message["content"]
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                fn_name = func.get("name", "")
                fn_args_raw = func.get("arguments", "{}")

                try:
                    fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                except json.JSONDecodeError:
                    fn_args = {}

                # Track tool use
                all_tool_uses.append(ToolUseContent(
                    id=tc_id, name=fn_name, input=fn_args,
                ))

                if log_fh:
                    _write_log(
                        log_fh, "tool_call",
                        turn=turn, tool=fn_name,
                        args={k: str(v)[:200] for k, v in fn_args.items()},
                    )

                # Execute the tool locally
                result_str = await loop.run_in_executor(
                    None, lambda n=fn_name, a=fn_args: _execute_tool(n, a, cwd)
                )

                all_tool_results.append(ToolResultContent(
                    tool_use_id=tc_id,
                    content=result_str,
                ))

                if log_fh:
                    _write_log(
                        log_fh, "tool_result",
                        turn=turn, tool=fn_name,
                        result_preview=result_str[:300],
                    )

                # Append tool result to message history
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_str,
                })

            # If finish_reason is "stop" despite having tool_calls,
            # the model is done after these tools
            if finish_reason == "stop" and not tool_calls:
                break

        # If we exhausted turns without a final text, take the last content
        if final_text is None:
            final_text = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    final_text = msg["content"]
                    break

        duration_ms = int((time.time() - start) * 1000)

        # Build response
        usage_data = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "prompt_tokens": total_input_tokens,
            "completion_tokens": total_output_tokens,
        }

        metrics = Metrics(
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=num_turns,
            total_cost_usd=None,
            usage=usage_data,
            session_id="",
        )

        # Build content list for the Message
        content_blocks: list[TextContent | ToolUseContent | ToolResultContent] = []
        for tu in all_tool_uses:
            content_blocks.append(tu)
        for tr in all_tool_results:
            content_blocks.append(tr)
        if final_text:
            content_blocks.append(TextContent(text=final_text))

        msg = Message(
            role="assistant",
            content=content_blocks,  # type: ignore[arg-type]
            model=model,
            error=None,
            parent_tool_use_id=None,
        )

        if log_fh:
            _write_log(
                log_fh, "complete",
                num_turns=num_turns,
                duration_ms=duration_ms,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                tool_calls_made=len(all_tool_uses),
            )

        # Parse structured output if schema was requested
        parsed: T | None = None
        if output_schema is not None and final_text:
            raw = final_text.strip()
            if raw.startswith("```"):
                lines = raw.split("\n", 1)
                raw = lines[1] if len(lines) > 1 else raw
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

            try:
                parsed = output_schema.model_validate_json(raw)
            except Exception:
                try:
                    data = json.loads(raw)
                    data = _normalize_json(data, output_schema)
                    parsed = output_schema.model_validate(data)
                except Exception:
                    if log_fh:
                        _write_log(
                            log_fh, "parse_error",
                            schema=output_schema.__name__,
                            response_preview=raw[:500],
                        )

        return AgentResponse(
            result=final_text,
            parsed=parsed,
            messages=[msg],
            metrics=metrics,
            is_error=False,
        )
