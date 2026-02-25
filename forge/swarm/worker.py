"""Swarm workers for Layer 1 of Hive Discovery.

Each worker analyzes a single segment through a specific lens
(security, quality, or architecture). Workers use minimax-m2.5
and write findings to the shared CodeGraph.

Two-wave execution:
  Wave 1: Workers analyze their primary segment in isolation
  Wave 2: Workers re-analyze with access to neighbor findings (MoA pattern)
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from forge.graph.models import CodeGraph, SegmentContext, Segment

logger = logging.getLogger(__name__)


def _read_file_safe(path: str, max_chars: int = 12_000) -> str:
    """Read a file safely with truncation."""
    try:
        content = Path(path).read_text(errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content
    except OSError:
        return ""


class SwarmWorker(ABC):
    """Base class for swarm analysis workers.

    Each worker:
    1. Receives a SegmentContext (files, graph neighbors, edges)
    2. Calls a cheap LLM (minimax-m2.5) with a focused prompt
    3. Writes findings back to the CodeGraph
    """

    worker_type: str = "base"

    def __init__(
        self,
        segment_id: str,
        model: str = "minimax/minimax-m2.5",
        ai_provider: str = "openrouter_direct",
    ):
        self.segment_id = segment_id
        self.model = model
        self.ai_provider = ai_provider

    @abstractmethod
    def build_system_prompt(self) -> str:
        """Return the system prompt for this worker type."""

    @abstractmethod
    def build_task_prompt(
        self,
        context: SegmentContext,
        wave: int,
        repo_path: str,
    ) -> str:
        """Build the task prompt with segment context."""

    async def analyze(
        self,
        graph: CodeGraph,
        wave: int,
        repo_path: str,
    ) -> list[dict]:
        """Run analysis on the assigned segment.

        Args:
            graph: The shared CodeGraph
            wave: 1 or 2 (Wave 2 includes neighbor findings)
            repo_path: Path to the repository root

        Returns:
            List of finding dicts written to the graph
        """
        from forge.vendor.agent_ai import AgentAI, AgentAIConfig

        # Build context
        context = graph.query_segment(self.segment_id)

        # Load file contents
        root = Path(repo_path)
        for file_path in context.segment.files:
            abs_path = str(root / file_path)
            content = _read_file_safe(abs_path)
            if content:
                context.file_contents[file_path] = content

        # In Wave 2, add neighbor findings
        if wave == 2:
            context.neighbor_findings = graph.query_neighbors(self.segment_id)

        # Build prompts
        system_prompt = self.build_system_prompt()
        task_prompt = self.build_task_prompt(context, wave, repo_path)

        # Call LLM
        ai = AgentAI(AgentAIConfig(
            provider=self.ai_provider,
            model=self.model,
            cwd=repo_path,
            max_turns=1,
            allowed_tools=[],
            env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
            agent_name=f"hive_worker/{self.worker_type}",
        ))

        response = await ai.run(task_prompt, system_prompt=system_prompt)

        # Parse findings from response
        findings = self._parse_findings(response)

        # Write findings to graph
        for finding in findings:
            finding["worker_type"] = self.worker_type
            finding["segment_id"] = self.segment_id
            finding["wave"] = wave
            graph.add_finding(finding, self.segment_id)

        logger.info(
            "Worker %s/%s wave %d: %d findings",
            self.worker_type, self.segment_id, wave, len(findings),
        )
        return findings

    def _parse_findings(self, response) -> list[dict]:
        """Parse findings from LLM response."""
        data = {}
        if response.parsed:
            data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
        elif response.text:
            data = _parse_json_response(response.text)

        findings = data.get("findings", [])
        if not isinstance(findings, list):
            findings = []

        return findings


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


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


# ── Concrete Workers ─────────────────────────────────────────────────


class SecurityWorker(SwarmWorker):
    """Security-focused analysis worker."""

    worker_type = "security"

    def build_system_prompt(self) -> str:
        return """You are a security auditor analyzing a code segment for vulnerabilities.

Focus areas:
- Authentication/authorization flaws
- Input validation and sanitization gaps
- SQL injection, XSS, CSRF risks
- Secrets management and exposure
- Insecure cryptographic patterns
- Rate limiting and DoS protection gaps
- OWASP Top 10 issues

Output JSON with this structure:
{
  "findings": [
    {
      "id": "SEC-001",
      "title": "Short descriptive title",
      "description": "Detailed explanation of the vulnerability",
      "category": "security",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "path/to/file.py", "line_start": 10, "line_end": 15, "snippet": "vulnerable code"}],
      "suggested_fix": "How to fix this",
      "confidence": 0.8,
      "cwe_id": "CWE-XXX",
      "owasp_ref": "A01:2021"
    }
  ],
  "summary": "Brief summary of security posture"
}"""

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Security Analysis Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments (cross-reference these)\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nCross-reference these neighbor findings with this segment's code. "
                "Look for vulnerability chains that span segments."
            )
        parts.append("\nAnalyze the code above for security vulnerabilities. Output valid JSON only.")
        return "\n".join(parts)


class QualityWorker(SwarmWorker):
    """Code quality analysis worker."""

    worker_type = "quality"

    def build_system_prompt(self) -> str:
        return """You are a code quality auditor analyzing a code segment for quality issues.

Focus areas:
- Error handling gaps (uncaught exceptions, missing error boundaries)
- Code duplication and DRY violations
- Complex functions (high cyclomatic complexity)
- Missing input validation
- Inconsistent patterns and anti-patterns
- Performance issues (N+1 queries, missing pagination, memory leaks)
- Testability concerns

Output JSON with this structure:
{
  "findings": [
    {
      "id": "QUAL-001",
      "title": "Short descriptive title",
      "description": "Detailed explanation of the quality issue",
      "category": "quality",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "path/to/file.py", "line_start": 10, "line_end": 15, "snippet": "problematic code"}],
      "suggested_fix": "How to improve this",
      "confidence": 0.8
    }
  ],
  "summary": "Brief summary of code quality"
}"""

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Code Quality Analysis Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nConsider these neighbor findings. "
                "Look for quality patterns that repeat across segments."
            )
        parts.append("\nAnalyze the code above for quality issues. Output valid JSON only.")
        return "\n".join(parts)


class ArchitectureWorker(SwarmWorker):
    """Architecture analysis worker."""

    worker_type = "architecture"

    def build_system_prompt(self) -> str:
        return """You are an architecture reviewer analyzing a code segment for structural issues.

Focus areas:
- Coupling between modules (tight coupling, circular dependencies)
- Layering violations (presentation accessing data directly)
- Missing abstractions or over-abstraction
- Inconsistent architectural patterns
- Scalability concerns
- Configuration management issues
- Dependency management issues

Output JSON with this structure:
{
  "findings": [
    {
      "id": "ARCH-001",
      "title": "Short descriptive title",
      "description": "Detailed explanation of the architecture issue",
      "category": "architecture",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "path/to/file.py", "line_start": 10, "line_end": 15, "snippet": "problematic code"}],
      "suggested_fix": "How to improve this",
      "confidence": 0.8
    }
  ],
  "summary": "Brief summary of architecture quality"
}"""

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Architecture Review Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments (cross-cutting concerns)\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nExamine how this segment's architecture relates to neighboring segment issues. "
                "Look for cross-cutting architectural concerns."
            )
        parts.append("\nAnalyze the code above for architectural issues. Output valid JSON only.")
        return "\n".join(parts)
