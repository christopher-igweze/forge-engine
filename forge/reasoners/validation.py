"""Validation-mode reasoners: Agents 11-12.

Agent 11: Integration Validator — post-merge validation
Agent 12: Debt Tracker & Report Generator — production readiness report
"""

from __future__ import annotations

import json
import logging
import os

from forge.vendor.agent_ai import AgentAI, AgentAIConfig
from forge.vendor.agent_ai.types import Tool

from forge.prompts.integration_validator import (
    SYSTEM_PROMPT as VALIDATOR_SYSTEM_PROMPT,
    integration_validator_task_prompt,
)
from forge.prompts.debt_tracker import (
    SYSTEM_PROMPT as DEBT_TRACKER_SYSTEM_PROMPT,
    debt_tracker_task_prompt,
)
from forge.schemas import (
    CategoryScore,
    DebtEntry,
    IntegrationValidationResult,
    ProductionReadinessReport,
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


# ── Agent 11: Integration Validator ───────────────────────────────────


@router.reasoner()
async def run_integration_validator(
    repo_path: str,
    all_findings: list[dict],
    completed_fixes: list[dict],
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "opencode",
    max_turns: int = 20,
    artifacts_dir: str = "",
) -> dict:
    """Agent 11: Validate the merged codebase after all fixes.

    Uses opencode provider because it needs to run tests via Bash.
    """
    logger.info("Agent 11: Integration Validator starting")

    task = integration_validator_task_prompt(
        all_findings_json=json.dumps(all_findings, indent=2, default=str),
        all_fixes_json=json.dumps(completed_fixes, indent=2, default=str),
        test_results="(Agent will run tests via Bash and report results)",
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=[Tool.READ, Tool.BASH, Tool.GLOB, Tool.GREP],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="integration_validator",
    ))

    response = await ai.run(task, system_prompt=VALIDATOR_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    result = IntegrationValidationResult(
        passed=data.get("passed", False),
        tests_run=data.get("tests_run", 0),
        tests_passed=data.get("tests_passed", 0),
        tests_failed=data.get("tests_failed", 0),
        regressions_detected=data.get("regressions_detected", []),
        new_issues_introduced=data.get("new_issues_introduced", []),
        summary=data.get("summary", ""),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "validation/integration_report.json", result.model_dump())

    logger.info(
        "Agent 11: %s — %d/%d tests passed, %d regressions",
        "PASSED" if result.passed else "FAILED",
        result.tests_passed, result.tests_run, len(result.regressions_detected),
    )
    return result.model_dump()


# ── Agent 12: Debt Tracker & Report Generator ─────────────────────────


@router.reasoner()
async def run_debt_tracker(
    all_findings: list[dict],
    completed_fixes: list[dict],
    deferred_items: list[dict] | None = None,
    validation_result: dict | None = None,
    model: str = "minimax/minimax-m2.5",
    ai_provider: str = "openrouter_direct",
    artifacts_dir: str = "",
) -> dict:
    """Agent 12: Generate Production Readiness Report.

    Uses openrouter_direct (structured writing, no file tools).
    This produces the viral acquisition hook — the readiness score.
    """
    logger.info("Agent 12: Debt Tracker starting")

    task = debt_tracker_task_prompt(
        all_findings_json=json.dumps(all_findings, indent=2, default=str),
        completed_fixes_json=json.dumps(completed_fixes, indent=2, default=str),
        deferred_items_json=json.dumps(deferred_items or [], indent=2, default=str),
        validation_result_json=json.dumps(validation_result or {}, indent=2, default=str),
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=".",
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="debt_tracker",
    ))

    response = await ai.run(task, system_prompt=DEBT_TRACKER_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    # Parse category scores
    category_scores = []
    for cs in data.get("category_scores", []):
        try:
            category_scores.append(CategoryScore(**cs))
        except Exception:
            pass

    # If no category scores from LLM, compute defaults
    if not category_scores:
        total = len(all_findings)
        fixed = len(completed_fixes)
        deferred = len(deferred_items or [])
        fix_rate = (fixed / total * 100) if total > 0 else 0

        category_scores = [
            CategoryScore(name="Security", score=int(fix_rate * 0.9), weight=0.30),
            CategoryScore(name="Error Handling", score=int(fix_rate * 0.8), weight=0.20),
            CategoryScore(name="Test Coverage", score=int(fix_rate * 0.5), weight=0.15),
            CategoryScore(name="Architecture", score=int(fix_rate * 0.7), weight=0.15),
            CategoryScore(name="Performance", score=int(fix_rate * 0.6), weight=0.10),
            CategoryScore(name="Documentation", score=int(fix_rate * 0.4), weight=0.10),
        ]

    # Parse debt items
    debt_items = []
    for di in data.get("debt_items", []):
        try:
            debt_items.append(DebtEntry(**di))
        except Exception:
            pass

    # Add deferred findings as debt items
    for df in (deferred_items or []):
        debt_items.append(DebtEntry(
            title=df.get("title", "Deferred finding"),
            description=df.get("description", ""),
            severity=df.get("severity", "medium"),
            source_finding_id=df.get("id", ""),
            reason_deferred="Deferred during remediation",
        ))

    overall = data.get("overall_score", 0)
    if not overall and category_scores:
        overall = int(sum(cs.score * cs.weight for cs in category_scores))

    report = ProductionReadinessReport(
        overall_score=min(100, max(0, overall)),
        category_scores=category_scores,
        findings_total=len(all_findings),
        findings_fixed=len(completed_fixes),
        findings_deferred=len(deferred_items or []),
        debt_items=debt_items,
        summary=data.get("summary", ""),
        recommendations=data.get("recommendations", []),
        investor_summary=data.get("investor_summary", ""),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "report/production_readiness.json", report.model_dump())

    logger.info(
        "Agent 12: Complete — score: %d/100, %d fixed, %d deferred",
        report.overall_score, report.findings_fixed, report.findings_deferred,
    )
    return report.model_dump()


# ── Artifact persistence ──────────────────────────────────────────────


def _save_artifact(artifacts_dir: str, rel_path: str, data: dict) -> None:
    """Save a JSON artifact to the artifacts directory."""
    from pathlib import Path

    full_path = Path(artifacts_dir) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Saved artifact: %s", full_path)
