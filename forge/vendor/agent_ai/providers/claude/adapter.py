"""Claude SDK adapter helpers."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    TextBlock as _TextBlock,
    ThinkingBlock as _ThinkingBlock,
    ToolResultBlock as _ToolResultBlock,
    ToolUseBlock as _ToolUseBlock,
)

from forge.vendor.agent_ai.types import (
    Content,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
)


def convert_content_block(block: Any) -> Content:
    """Map Claude SDK content blocks to provider-agnostic content dataclasses."""
    if isinstance(block, _TextBlock):
        return TextContent(text=block.text)
    if isinstance(block, _ToolUseBlock):
        return ToolUseContent(id=block.id, name=block.name, input=block.input)
    if isinstance(block, _ToolResultBlock):
        return ToolResultContent(
            tool_use_id=block.tool_use_id,
            content=block.content,
            is_error=block.is_error or False,
        )
    if isinstance(block, _ThinkingBlock):
        return ThinkingContent(thinking=block.thinking, signature=block.signature)
    return TextContent(text=str(block)[:500])
