"""Swarm workers for Layer 1 of Hive Discovery.

Each worker analyzes a single segment through a specific lens
(security, quality, or architecture). Workers use minimax-m2.5
and write findings to the shared CodeGraph.

Two-wave execution:
  Wave 1: Workers analyze their primary segment in isolation
  Wave 2: Workers re-analyze with access to neighbor findings (MoA pattern)

Prompt structure follows research-backed patterns:
  - Sequential analysis steps (M2.5 performs better with step-by-step vs parallel)
  - Evidence requirements (Semgrep: eliminates theoretical findings)
  - Hard exclusion lists (Anthropic: filters known false-positive magnets)
  - Anti-sycophancy calibration (Stanford: 58% sycophancy rate without instruction)
  - <think> tag handling (M2.5 may emit reasoning tags)
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from forge.graph.models import CodeGraph, SegmentContext
from forge.swarm.worker_utils import (
    MAX_M2_CONTEXT_CHARS,
    _format_file_contents,
    _format_graph_context,
    _format_neighbor_findings,
    _parse_json_response,
    _truncate_contents,
)

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

        # M2.5 context budget guard — degrades beyond ~90k tokens
        total_chars = len(system_prompt) + len(task_prompt)
        if "minimax" in self.model and total_chars > MAX_M2_CONTEXT_CHARS:
            logger.warning(
                "M2.5 context size %d exceeds safe threshold %d — truncating file contents",
                total_chars, MAX_M2_CONTEXT_CHARS,
            )
            budget = MAX_M2_CONTEXT_CHARS - len(system_prompt) - 5000
            context.file_contents = _truncate_contents(context.file_contents, budget)
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


# ── Concrete Workers (re-exported for backward compatibility) ────────
# Actual implementations live in forge.swarm.workers.
from forge.swarm.workers import (  # noqa: E402
    ArchitectureWorker,
    QualityWorker,
    SecurityWorker,
)
