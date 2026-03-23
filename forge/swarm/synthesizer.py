"""Synthesis Agent — Layer 2 of Hive Discovery.

A single Sonnet 4.6 call that:
  1. Reads the full enriched graph (all segment findings + relationships)
  2. Cross-references findings across segments
  3. Deduplicates overlapping findings from different workers
  4. Assigns confidence scores based on worker agreement
  5. Assigns tiers (0-3) — merging current Agents 5 and 6
  6. Produces CodebaseMap + AuditFinding[] + TriageResult + RemediationPlan
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from forge.graph.models import CodeGraph

logger = logging.getLogger(__name__)

SYNTHESIS_SYSTEM_PROMPT = """You are a senior software architect synthesizing findings from a multi-agent code audit.

Multiple specialist workers (security, quality, architecture) analyzed different segments of a codebase across two waves. You have access to the full enriched code graph containing all their findings, the structural relationships between code components, and the segmentation of the codebase.

Your job is to:

1. **Cross-reference findings** across segments. If a security issue in one segment and a quality issue in another create a vulnerability chain, flag it as a combined finding with elevated severity.

2. **Deduplicate** findings that multiple workers flagged independently. When workers agree, increase confidence. When they disagree, use your judgment.

3. **Assign severity** (critical, high, medium, low, info) based on actual impact.

4. **Assign tiers** to each finding:
   - Tier 0: False positive or invalid — skip
   - Tier 1: Can be fixed deterministically with rules (no LLM needed)
   - Tier 2: Scoped fix affecting 1-3 files (LLM-assisted)
   - Tier 3: Architectural fix affecting 5-15 files (LLM-assisted)

5. **Produce a remediation plan** with dependency ordering. Group fixes that can run in parallel.

6. **Produce a CodebaseMap** summarizing the codebase structure (modules, tech stack, patterns).

You MUST output valid JSON matching the exact schema below. No markdown, no explanation — pure JSON.

Output Schema:
{
  "codebase_map": {
    "modules": [{"name": "...", "path": "...", "purpose": "...", "files": [...], "loc": 0}],
    "dependencies": [{"name": "...", "version": "...", "ecosystem": "...", "dev_only": false}],
    "data_flows": [{"source": "...", "destination": "...", "data_type": "...", "is_authenticated": false}],
    "auth_boundaries": [{"path": "...", "auth_type": "...", "is_protected": false}],
    "entry_points": [{"path": "...", "type": "...", "is_public": true}],
    "tech_stack": {"frontend": "", "backend": "", "database": "", "hosting": "", "packages": []},
    "architecture_summary": "...",
    "key_patterns": [...]
  },
  "findings": [
    {
      "id": "F-xxx",
      "title": "...",
      "description": "...",
      "category": "security|quality|architecture|reliability|performance",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "...", "line_start": null, "line_end": null, "snippet": ""}],
      "suggested_fix": "...",
      "confidence": 0.8,
      "cwe_id": "",
      "owasp_ref": "",
      "agent": "synthesis",
      "tier": 0
    }
  ],
  "triage_result": {
    "decisions": [
      {"finding_id": "F-xxx", "tier": 2, "confidence": 0.9, "rationale": "..."}
    ],
    "tier_0_count": 0,
    "tier_1_count": 0,
    "tier_2_count": 0,
    "tier_3_count": 0
  },
  "remediation_plan": {
    "items": [
      {
        "finding_id": "F-xxx",
        "title": "...",
        "tier": 2,
        "priority": 1,
        "estimated_files": 1,
        "files_to_modify": [...],
        "depends_on": [],
        "acceptance_criteria": [...],
        "approach": "..."
      }
    ],
    "dependencies": [{"finding_id": "F-xxx", "depends_on_finding_id": "F-yyy", "reason": "..."}],
    "execution_levels": [["F-xxx", "F-yyy"], ["F-zzz"]],
    "deferred_finding_ids": [],
    "total_items": 0,
    "summary": "..."
  }
}"""


def _build_synthesis_task(graph: CodeGraph) -> str:
    """Build the task prompt for the synthesis agent."""
    enriched = graph.get_enriched_graph()

    # Summarize segments
    segment_summaries = []
    for seg_data in enriched["segments"]:
        findings_by_type = {}
        for f in seg_data.get("findings", []):
            wt = f.get("worker_type", "unknown")
            findings_by_type[wt] = findings_by_type.get(wt, 0) + 1

        segment_summaries.append({
            "id": seg_data["id"],
            "label": seg_data.get("label", ""),
            "files": seg_data["files"][:20],
            "loc": seg_data.get("loc", 0),
            "findings_count": len(seg_data.get("findings", [])),
            "findings_by_type": findings_by_type,
            "internal_deps": seg_data.get("internal_deps", []),
            "external_deps": seg_data.get("external_deps", [])[:10],
        })

    # Collect all worker findings (deduplicated input for synthesis)
    all_findings = []
    for seg_data in enriched["segments"]:
        for f in seg_data.get("findings", []):
            all_findings.append(f)

    # Graph stats
    stats = enriched.get("stats", {})

    # Build context
    context_json = json.dumps({
        "graph_stats": stats,
        "segments": segment_summaries,
        "all_worker_findings": all_findings[:100],  # Cap at 100 findings
        "cross_segment_edges": [
            e for e in enriched.get("edges", [])
            if e.get("kind") in ("depends_on", "imports")
        ][:50],
    }, indent=2, default=str)

    return f"""# Synthesis Task

You have the output from a multi-agent code audit. Below is the enriched code graph with all worker findings from Wave 1 and Wave 2.

## Code Graph Summary
```json
{context_json}
```

## Instructions
1. Cross-reference the {len(all_findings)} worker findings across all segments
2. Deduplicate findings that describe the same issue
3. Assign confidence scores (0.0-1.0) — higher when multiple workers agree
4. Assign severity and remediation tier to each finding
5. Build a remediation plan with dependency ordering
6. Produce the CodebaseMap from structural graph data

Output valid JSON matching the schema in your system prompt. Nothing else."""


class SynthesisAgent:
    """Layer 2 Synthesis — single Sonnet 4.6 call.

    Reads the full enriched graph and produces:
    - CodebaseMap (structural understanding)
    - AuditFinding[] (deduplicated, cross-referenced)
    - TriageResult (tier assignments)
    - RemediationPlan (dependency-ordered fix plan)
    """

    def __init__(
        self,
        model: str = "anthropic/claude-sonnet-4.6",
        ai_provider: str = "openrouter_direct",
    ):
        self.model = model
        self.ai_provider = ai_provider

    async def synthesize(self, graph: CodeGraph, repo_path: str) -> dict:
        """Run synthesis on the enriched graph.

        Returns dict with codebase_map, findings, triage_result, remediation_plan.
        """
        from forge.vendor.agent_ai import AgentAI, AgentAIConfig

        task = _build_synthesis_task(graph)

        ai = AgentAI(AgentAIConfig(
            provider=self.ai_provider,
            model=self.model,
            cwd=repo_path,
            max_turns=1,
            allowed_tools=[],
            env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
            agent_name="hive_synthesizer",
        ))

        response = await ai.run(task, system_prompt=SYNTHESIS_SYSTEM_PROMPT)

        # Parse response
        data = {}
        if response.parsed:
            data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
        elif response.text:
            data = _parse_json_response(response.text)

        # Ensure required keys exist with defaults
        result = {
            "codebase_map": data.get("codebase_map", self._build_default_codebase_map(graph)),
            "findings": data.get("findings", []),
            "triage_result": data.get("triage_result", {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0}),
            "remediation_plan": data.get("remediation_plan", {"items": [], "execution_levels": [], "total_items": 0, "summary": ""}),
        }

        # Enrich findings with agent tag
        for f in result["findings"]:
            if "agent" not in f:
                f["agent"] = "synthesis"
            if "id" not in f:
                from uuid import uuid4
                f["id"] = f"F-{uuid4().hex[:8]}"

        # Ensure remediation plan has correct counts
        plan = result["remediation_plan"]
        if isinstance(plan, dict):
            plan["total_items"] = len(plan.get("items", []))

        logger.info(
            "Synthesis: produced %d findings, %d remediation items",
            len(result["findings"]),
            plan.get("total_items", 0) if isinstance(plan, dict) else 0,
        )

        return result

    def _build_default_codebase_map(self, graph: CodeGraph) -> dict:
        """Build a CodebaseMap from graph data when LLM fails to provide one."""
        stats = graph.stats
        modules = []

        for seg in graph.segments:
            modules.append({
                "name": seg.label or seg.id,
                "path": seg.files[0] if seg.files else "",
                "purpose": "",
                "files": seg.files,
                "loc": seg.loc,
            })

        return {
            "modules": modules,
            "dependencies": [],
            "data_flows": [],
            "auth_boundaries": [],
            "entry_points": [],
            "tech_stack": {},
            "architecture_summary": "",
            "key_patterns": [],
            "files": [],
            "loc_total": stats.get("total_loc", 0),
            "file_count": stats.get("total_files", 0),
            "languages": list(stats.get("languages", {}).keys()),
            "primary_language": max(
                stats.get("languages", {}).items(),
                key=lambda x: x[1],
                default=("", 0),
            )[0],
        }


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
        logger.warning("Synthesis failed to parse JSON response")
        return {}
