"""Hive Discovery Orchestrator — coordinates the three-layer pipeline.

Layer 0: Code Graph Builder (deterministic, no LLM)
Layer 1: Swarm Analysis (parallel minimax workers, two waves)
Layer 2: Synthesis (single sonnet-4.6 call)

The orchestrator replaces the current serial Agent 1 → Agents 2-4 → Agents 5-6
flow when discovery_mode = "swarm".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from forge.graph.builder import CodeGraphBuilder
from forge.graph.models import CodeGraph, Segment
from forge.swarm.worker import (
    ArchitectureWorker,
    QualityWorker,
    SecurityWorker,
    SwarmWorker,
)
from forge.swarm.synthesizer import SynthesisAgent

logger = logging.getLogger(__name__)


class HiveOrchestrator:
    """Orchestrates the three-layer Hive Discovery pipeline.

    Usage:
        orchestrator = HiveOrchestrator(
            repo_path="/path/to/repo",
            worker_model="minimax/minimax-m2.5",
            synthesis_model="anthropic/claude-sonnet-4.6",
        )
        result = await orchestrator.run()
    """

    def __init__(
        self,
        repo_path: str,
        repo_url: str = "",
        artifacts_dir: str = "",
        worker_model: str = "minimax/minimax-m2.5",
        synthesis_model: str = "anthropic/claude-sonnet-4.6",
        ai_provider: str = "openrouter_direct",
        target_segments: int = 5,
        enable_wave2: bool = True,
        worker_types: list[str] | None = None,
        pattern_library_path: str = "",
        project_context: dict | None = None,
    ):
        self.repo_path = repo_path
        self.repo_url = repo_url
        self.artifacts_dir = artifacts_dir or os.path.join(repo_path, ".artifacts")
        self.worker_model = worker_model
        self.synthesis_model = synthesis_model
        self.ai_provider = ai_provider
        self.target_segments = target_segments
        self.enable_wave2 = enable_wave2
        self.worker_types = worker_types or ["security", "quality", "architecture"]
        self.pattern_library_path = pattern_library_path
        self.project_context = project_context or {}

        self._graph: CodeGraph | None = None
        self._total_invocations: int = 0

    async def run(self) -> dict:
        """Execute the full Hive Discovery pipeline.

        Returns:
            Dict with:
              - codebase_map: CodebaseMap-compatible dict
              - findings: list of AuditFinding-compatible dicts
              - remediation_plan: RemediationPlan-compatible dict
              - triage_result: TriageResult-compatible dict
              - graph: enriched CodeGraph dict
              - stats: execution statistics
        """
        start_time = time.time()
        os.makedirs(self.artifacts_dir, exist_ok=True)

        logger.info("Hive Discovery: starting for %s", self.repo_path)

        # ── Layer 0: Deterministic Code Graph Builder ────────────────
        logger.info("Hive Discovery: Layer 0 — building code graph")
        layer0_start = time.time()

        builder = CodeGraphBuilder(
            repo_path=self.repo_path,
            target_segments=self.target_segments,
        )
        self._graph = builder.build()
        layer0_time = time.time() - layer0_start

        logger.info(
            "Hive Discovery: Layer 0 complete — %d files, %d segments (%.1fs)",
            self._graph.stats.get("total_files", 0),
            len(self._graph.segments),
            layer0_time,
        )

        # Save Layer 0 artifact
        self._save_artifact("hive/layer0_graph.json", self._graph.get_enriched_graph())

        # ── Layer 1: Swarm Analysis ──────────────────────────────────
        logger.info("Hive Discovery: Layer 1 — swarm analysis")
        layer1_start = time.time()

        # Wave 1: Parallel analysis
        wave1_findings = await self._run_wave(1)
        self._save_artifact("hive/wave1_findings.json", wave1_findings)

        # Wave 2: Re-analysis with neighbor context (MoA pattern)
        wave2_findings = []
        if self.enable_wave2:
            wave2_findings = await self._run_wave(2)
            self._save_artifact("hive/wave2_findings.json", wave2_findings)

        layer1_time = time.time() - layer1_start
        all_worker_findings = wave1_findings + wave2_findings

        logger.info(
            "Hive Discovery: Layer 1 complete — %d findings (wave1=%d, wave2=%d, %.1fs)",
            len(all_worker_findings),
            len(wave1_findings),
            len(wave2_findings),
            layer1_time,
        )

        # Save enriched graph after Layer 1
        self._save_artifact("hive/layer1_enriched_graph.json", self._graph.get_enriched_graph())

        # ── Layer 2: Synthesis ───────────────────────────────────────
        logger.info("Hive Discovery: Layer 2 — synthesis")
        layer2_start = time.time()

        synthesizer = SynthesisAgent(
            model=self.synthesis_model,
            ai_provider=self.ai_provider,
        )
        synthesis_result = await synthesizer.synthesize(self._graph, self.repo_path)
        self._total_invocations += 1
        layer2_time = time.time() - layer2_start

        logger.info(
            "Hive Discovery: Layer 2 complete — %d findings, %.1fs",
            len(synthesis_result.get("findings", [])),
            layer2_time,
        )

        self._save_artifact("hive/synthesis_result.json", synthesis_result)

        # ── Build final result ───────────────────────────────────────
        elapsed = time.time() - start_time

        result = {
            "codebase_map": synthesis_result.get("codebase_map", {}),
            "findings": synthesis_result.get("findings", []),
            "triage_result": synthesis_result.get("triage_result", {}),
            "remediation_plan": synthesis_result.get("remediation_plan", {}),
            "graph": self._graph.get_enriched_graph(),
            "stats": {
                "total_invocations": self._total_invocations,
                "layer0_time_seconds": round(layer0_time, 2),
                "layer1_time_seconds": round(layer1_time, 2),
                "layer2_time_seconds": round(layer2_time, 2),
                "total_time_seconds": round(elapsed, 2),
                "segments": len(self._graph.segments),
                "wave1_findings": len(wave1_findings),
                "wave2_findings": len(wave2_findings),
                "synthesis_findings": len(synthesis_result.get("findings", [])),
            },
        }

        # Save final combined result
        self._save_artifact("hive/discovery_result.json", result)

        logger.info(
            "Hive Discovery complete: %d findings, %d invocations, %.1fs total",
            len(result["findings"]),
            self._total_invocations,
            elapsed,
        )

        return result

    async def _run_wave(self, wave: int) -> list[dict]:
        """Run a single wave of swarm workers across all segments."""
        workers = self._create_workers()
        tasks = []

        for worker in workers:
            tasks.append(worker.analyze(self._graph, wave, self.repo_path))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._total_invocations += len(tasks)

        all_findings = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Worker %d failed in wave %d: %s",
                    i, wave, result,
                )
                continue
            if isinstance(result, list):
                all_findings.extend(result)

        return all_findings

    def _create_workers(self) -> list[SwarmWorker]:
        """Create worker instances for all segments × worker types."""
        workers: list[SwarmWorker] = []

        worker_classes = {
            "security": SecurityWorker,
            "quality": QualityWorker,
            "architecture": ArchitectureWorker,
        }

        # Load pattern context once for all SecurityWorkers
        pattern_ctx = self._build_pattern_context()

        # Build project context string once for all workers
        project_ctx = self._build_project_context()

        for segment in self._graph.segments:
            for worker_type in self.worker_types:
                cls = worker_classes.get(worker_type)
                if cls:
                    kwargs: dict = {
                        "segment_id": segment.id,
                        "model": self.worker_model,
                        "ai_provider": self.ai_provider,
                    }
                    if worker_type == "security" and pattern_ctx:
                        kwargs["pattern_context"] = pattern_ctx
                    if project_ctx:
                        kwargs["project_context"] = project_ctx
                    workers.append(cls(**kwargs))

        return workers

    def _build_pattern_context(self) -> str:
        """Load pattern library and build LLM context string."""
        try:
            from forge.patterns.context import build_pattern_context_for_prompt
            from forge.patterns.loader import PatternLibrary

            if self.pattern_library_path:
                library = PatternLibrary.load_from_directory(self.pattern_library_path)
            else:
                library = PatternLibrary.load_default()

            if not library:
                return ""

            return build_pattern_context_for_prompt(library, category="security")
        except Exception as exc:
            logger.warning("Failed to load pattern library: %s", exc)
            return ""

    def _build_project_context(self) -> str:
        """Build project context string from user-provided metadata."""
        if not self.project_context:
            return ""
        try:
            from forge.prompts.project_context import build_project_context_string
            return build_project_context_string(self.project_context)
        except Exception as exc:
            logger.warning("Failed to build project context: %s", exc)
            return ""

    def _save_artifact(self, rel_path: str, data: Any) -> None:
        """Save a JSON artifact."""
        full_path = Path(self.artifacts_dir) / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(data, indent=2, default=str))
        logger.debug("Saved artifact: %s", full_path)
