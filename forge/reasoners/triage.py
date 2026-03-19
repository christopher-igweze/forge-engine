"""Triage-mode reasoners: Agent 5 (Fix Strategist).

Agent 5: Fix Strategist — deduplication + dependency ordering + tier assignment
"""

from __future__ import annotations

import json
import logging
import os

from forge.vendor.agent_ai import AgentAI, AgentAIConfig

from forge.prompts.fix_strategist import (
    SYSTEM_PROMPT as STRATEGIST_SYSTEM_PROMPT,
    fix_strategist_task_prompt,
)
from forge.schemas import (
    CodebaseMap,
    RemediationPlan,
    TriageResult,
)

from . import router

logger = logging.getLogger(__name__)


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response, stripping markdown fences."""
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
        logger.warning("Failed to parse JSON from LLM response")
        return {}


# ── Agent 5: Fix Strategist ──────────────────────────────────────────


@router.reasoner()
async def run_fix_strategist(
    all_findings: list[dict],
    codebase_map: dict,
    triage_result: dict | None = None,
    artifacts_dir: str = "",
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
    convergence_context: str = "",
) -> dict:
    """Agent 5: Create a prioritized remediation plan.

    Deduplicates findings, assigns dependencies, and produces
    execution levels for parallel fix execution.
    """
    logger.info("Agent 5: Fix Strategist starting with %d findings", len(all_findings))

    codebase_map_json = json.dumps(
        CodebaseMap(**codebase_map).model_dump(),
        indent=2, default=str,
    )

    # Merge triage tier assignments into findings
    if triage_result:
        tr = TriageResult(**triage_result)
        tier_map = {d.finding_id: d.tier for d in tr.decisions}
        for f in all_findings:
            if f.get("id") in tier_map:
                f["tier"] = tier_map[f["id"]].value

    all_findings_json = json.dumps(all_findings, indent=2, default=str)

    task = fix_strategist_task_prompt(
        all_findings_json=all_findings_json,
        codebase_map_json=codebase_map_json,
        convergence_context=convergence_context,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=".",
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="fix_strategist",
    ))

    response = await ai.run(task, system_prompt=STRATEGIST_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    # Normalize plan items before validation — LLMs sometimes return priority=0
    for item in data.get("items", []):
        if isinstance(item, dict):
            if isinstance(item.get("priority"), int) and item["priority"] < 1:
                item["priority"] = 1

    # Normalize dependencies — LLMs sometimes return depends_on_finding_id as list
    for dep in data.get("dependencies", []):
        if isinstance(dep, dict):
            val = dep.get("depends_on_finding_id")
            if isinstance(val, list):
                dep["depends_on_finding_id"] = val[0] if val else ""

    # Parse into RemediationPlan
    try:
        plan = RemediationPlan(**data)
    except Exception as e:
        logger.warning("Failed to parse full remediation plan: %s", e)
        plan = RemediationPlan(
            items=[],
            summary=f"Parse error: {e}. Raw findings count: {len(all_findings)}",
        )

    # Fill computed fields
    plan.total_items = len(plan.items)

    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/remediation_plan.json", plan.model_dump())

    logger.info(
        "Agent 5: Complete — %d items, %d levels, %d deferred",
        plan.total_items, len(plan.execution_levels), len(plan.deferred_finding_ids),
    )
    return plan.model_dump()


# ── Artifact persistence ──────────────────────────────────────────────


def _save_artifact(artifacts_dir: str, rel_path: str, data: dict) -> None:
    """Save a JSON artifact to the artifacts directory."""
    from pathlib import Path

    full_path = Path(artifacts_dir) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Saved artifact: %s", full_path)
