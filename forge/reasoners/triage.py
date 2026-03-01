"""Triage-mode reasoners: Agents 5-6.

Agent 5: Fix Strategist — deduplication + dependency ordering
Agent 6: Triage Classifier — tier assignment (rule-based + LLM fallback)
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
from forge.prompts.triage_classifier import (
    SYSTEM_PROMPT as TRIAGE_SYSTEM_PROMPT,
    TIER_0_SIGNALS,
    TIER_1_PATTERNS,
    triage_classifier_task_prompt,
)
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    RemediationPlan,
    RemediationTier,
    TriageDecision,
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


# ── Agent 6: Triage Classifier ────────────────────────────────────────


def _rule_based_triage(
    finding: AuditFinding,
    codebase_map: CodebaseMap,
) -> TriageDecision | None:
    """Attempt rule-based tier classification without LLM.

    Returns a TriageDecision if the finding matches a known pattern,
    or None if LLM fallback is needed.
    """
    title_lower = (finding.title + " " + finding.description).lower()

    # Intent signal from Intent Analyzer → intentional patterns are Tier 0
    if getattr(finding, "intent_signal", "") == "intentional":
        return TriageDecision(
            finding_id=finding.id,
            tier=RemediationTier.TIER_0,
            confidence=0.9,
            rationale="Flagged as intentional developer choice by Intent Analyzer",
        )

    # Check for Tier 0 signals
    for signal in TIER_0_SIGNALS:
        if signal in title_lower:
            return TriageDecision(
                finding_id=finding.id,
                tier=RemediationTier.TIER_0,
                confidence=0.95,
                rationale=f"Matched Tier 0 signal: '{signal}'",
            )

    # Check if referenced files exist
    codebase_paths = {f.path for f in codebase_map.files}
    if finding.locations:
        missing = [
            loc.file_path for loc in finding.locations
            if loc.file_path and loc.file_path not in codebase_paths
        ]
        if missing and len(missing) == len(finding.locations):
            return TriageDecision(
                finding_id=finding.id,
                tier=RemediationTier.TIER_0,
                confidence=0.9,
                rationale=f"All referenced files not found: {', '.join(missing)}",
            )

    # Check for Tier 1 known patterns
    for pattern in TIER_1_PATTERNS:
        if any(kw in title_lower for kw in pattern["keywords"]):
            return TriageDecision(
                finding_id=finding.id,
                tier=RemediationTier.TIER_1,
                confidence=0.85,
                rationale=f"Matched Tier 1 pattern: {pattern['pattern']}",
                fix_template_id=pattern["template_id"],
            )

    # Check for Tier 3 signals (architectural keywords)
    tier_3_signals = ["refactor", "restructure", "separation of concerns",
                      "circular dependency", "god module", "tight coupling"]
    if any(signal in title_lower for signal in tier_3_signals):
        return TriageDecision(
            finding_id=finding.id,
            tier=RemediationTier.TIER_3,
            confidence=0.8,
            rationale="Contains architectural keywords suggesting cross-cutting changes",
        )

    # Could not classify via rules — need LLM
    return None


@router.reasoner()
async def run_triage_classifier(
    findings: list[dict],
    codebase_map: dict,
    artifacts_dir: str = "",
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
) -> dict:
    """Agent 6: Classify findings into tiers 0-3.

    Uses rule-based fast path for known patterns and LLM fallback
    for ambiguous cases.
    """
    logger.info("Agent 6: Triage Classifier starting with %d findings", len(findings))

    cm = CodebaseMap(**codebase_map)
    parsed_findings = [AuditFinding(**f) for f in findings]

    decisions: list[TriageDecision] = []
    needs_llm: list[AuditFinding] = []

    # ── Phase 1: Rule-based classification ─────────────────────────────
    for finding in parsed_findings:
        decision = _rule_based_triage(finding, cm)
        if decision:
            decisions.append(decision)
            logger.debug("Rule-based: %s → Tier %d", finding.id, decision.tier.value)
        else:
            needs_llm.append(finding)

    logger.info(
        "Rule-based: %d classified, %d need LLM",
        len(decisions), len(needs_llm),
    )

    # ── Phase 2: LLM fallback for ambiguous findings ──────────────────
    if needs_llm:
        findings_json = json.dumps(
            [f.model_dump() for f in needs_llm],
            indent=2, default=str,
        )
        codebase_map_json = json.dumps(
            {"files": [{"path": f.path} for f in cm.files[:200]]},
            indent=2,
        )

        task = triage_classifier_task_prompt(
            findings_json=findings_json,
            codebase_map_json=codebase_map_json,
        )

        ai = AgentAI(AgentAIConfig(
            provider=ai_provider,
            model=model,
            cwd=".",
            max_turns=1,
            allowed_tools=[],
            env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
            agent_name="triage_classifier",
        ))

        response = await ai.run(task, system_prompt=TRIAGE_SYSTEM_PROMPT)

        data = {}
        if response.parsed:
            data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
        elif response.text:
            data = _parse_json_response(response.text)

        for d in data.get("decisions", []):
            try:
                decisions.append(TriageDecision(**d))
            except Exception as e:
                logger.warning("Failed to parse triage decision: %s", e)

    # ── Ensure every finding has a decision ────────────────────────────
    classified_ids = {d.finding_id for d in decisions}
    for finding in parsed_findings:
        if finding.id not in classified_ids:
            # Default unclassified to Tier 2 (conservative)
            decisions.append(TriageDecision(
                finding_id=finding.id,
                tier=RemediationTier.TIER_2,
                confidence=0.5,
                rationale="Defaulted to Tier 2 (unclassified)",
            ))

    # Build result with counts
    result = TriageResult(
        decisions=decisions,
        tier_0_count=sum(1 for d in decisions if d.tier == RemediationTier.TIER_0),
        tier_1_count=sum(1 for d in decisions if d.tier == RemediationTier.TIER_1),
        tier_2_count=sum(1 for d in decisions if d.tier == RemediationTier.TIER_2),
        tier_3_count=sum(1 for d in decisions if d.tier == RemediationTier.TIER_3),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/triage_result.json", result.model_dump())

    logger.info(
        "Agent 6: Complete — T0:%d T1:%d T2:%d T3:%d",
        result.tier_0_count, result.tier_1_count,
        result.tier_2_count, result.tier_3_count,
    )
    return result.model_dump()


# ── Agent 5: Fix Strategist ──────────────────────────────────────────


@router.reasoner()
async def run_fix_strategist(
    all_findings: list[dict],
    codebase_map: dict,
    triage_result: dict | None = None,
    artifacts_dir: str = "",
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
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
