"""Utility functions for swarm workers.

Extracted from worker.py — JSON parsing, content truncation, and
prompt formatting helpers used by all worker types.
"""

from __future__ import annotations

import json
import logging
import re
from forge.graph.models import SegmentContext

logger = logging.getLogger(__name__)

# M2.5 degrades beyond ~90k tokens (~300k chars). Guard against this.
MAX_M2_CONTEXT_CHARS = 300_000


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response — hardened for M2.5 quirks.

    Handles:
    - <think>...</think> reasoning tags (M2.5 may emit these)
    - Markdown code fences (```json ... ```)
    - Natural language preamble before the JSON object
    """
    cleaned = text.strip()

    # Strip <think>...</think> reasoning tags (M2.5 may emit these)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

    # Strip markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end]).strip()

    # M2.5 sometimes prepends natural language — find first { and last }
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        cleaned = cleaned[first_brace:last_brace + 1]

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def _truncate_contents(file_contents: dict[str, str], max_chars: int) -> dict[str, str]:
    """Truncate file contents to fit within a character budget."""
    result = {}
    chars_used = 0
    for path, content in sorted(file_contents.items()):
        if chars_used + len(content) > max_chars:
            remaining = max_chars - chars_used
            if remaining > 200:
                result[path] = content[:remaining] + "\n... (truncated for context budget)"
            break
        result[path] = content
        chars_used += len(content)
    return result


def _format_file_contents(file_contents: dict[str, str], max_total: int = 60_000) -> str:
    """Format file contents for prompt inclusion."""
    parts = []
    chars_used = 0
    for path, content in sorted(file_contents.items()):
        if chars_used + len(content) > max_total:
            break
        parts.append(f"### {path}\n```\n{content}\n```\n")
        chars_used += len(content)
    return "\n".join(parts) if parts else "(no files)"


def _format_graph_context(context: SegmentContext) -> str:
    """Format graph edges and node info for prompt context."""
    lines = []
    lines.append(f"Segment: {context.segment.id} ({context.segment.label})")
    lines.append(f"Files: {len(context.segment.files)}")
    lines.append(f"LOC: {context.segment.loc}")

    if context.segment.entry_points:
        lines.append(f"Entry Points: {', '.join(context.segment.entry_points[:10])}")

    if context.segment.external_deps:
        lines.append(f"External Dependencies: {', '.join(context.segment.external_deps[:15])}")

    if context.segment.internal_deps:
        lines.append(f"Depends on segments: {', '.join(context.segment.internal_deps)}")

    # Summarize edges
    edge_summary = {}
    for e in context.edges:
        edge_summary[e.kind.value] = edge_summary.get(e.kind.value, 0) + 1
    if edge_summary:
        lines.append(f"Edges: {', '.join(f'{k}={v}' for k, v in edge_summary.items())}")

    return "\n".join(lines)


def _format_neighbor_findings(findings: list[dict]) -> str:
    """Format neighbor findings for Wave 2 context."""
    if not findings:
        return "(no findings from neighboring segments)"

    parts = []
    for f in findings[:20]:  # Cap at 20 neighbor findings
        parts.append(
            f"- [{f.get('category', '?')}] {f.get('title', '?')}: "
            f"{f.get('description', '')[:200]}"
        )
    return "\n".join(parts)
