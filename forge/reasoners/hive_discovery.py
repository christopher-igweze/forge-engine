"""Hive Discovery reasoner — swarm-based discovery replacing Agents 1-6.

This module exposes the ``run_hive_discovery`` function which can be
called as a reasoner via AgentField or StandaloneDispatcher.

When ``config.discovery_mode == "swarm"``, the pipeline calls this
instead of the sequential Agent 1 → 2-4 → 5-6 flow.
"""

from __future__ import annotations

import logging
import os

from forge.reasoners import router

logger = logging.getLogger(__name__)


@router.reasoner()
async def run_hive_discovery(
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
) -> dict:
    """Run the full Hive Discovery pipeline (Layers 0-2).

    Replaces Agents 1-6 when discovery_mode = "swarm".

    Returns:
        Dict with codebase_map, findings, triage_result, remediation_plan,
        graph, and stats.
    """
    from forge.swarm.orchestrator import HiveOrchestrator

    logger.info("Hive Discovery: starting for %s", repo_url or repo_path)

    orchestrator = HiveOrchestrator(
        repo_path=repo_path,
        repo_url=repo_url,
        artifacts_dir=artifacts_dir or os.path.join(repo_path, ".artifacts"),
        worker_model=worker_model,
        synthesis_model=synthesis_model,
        ai_provider=ai_provider,
        target_segments=target_segments,
        enable_wave2=enable_wave2,
        worker_types=worker_types,
        pattern_library_path=pattern_library_path,
        project_context=project_context,
    )

    result = await orchestrator.run()

    logger.info(
        "Hive Discovery: complete — %d findings, %d invocations",
        len(result.get("findings", [])),
        result.get("stats", {}).get("total_invocations", 0),
    )

    return result
