"""Remediation-mode reasoners: Agents 7-10.

Agent 7:  Coder Tier 2 — scoped fixes (1-3 files, Sonnet 4.6)
Agent 8:  Coder Tier 3 — architectural fixes (5-15 files, Sonnet 4.6)
Agent 9:  Test Generator — tests for each fix (Haiku 4.5)
Agent 10: Code Reviewer — validates fixes (Haiku 4.5)
"""

from __future__ import annotations

import json
import logging
import os

from forge.vendor.agent_ai import AgentAI, AgentAIConfig
from forge.vendor.agent_ai.types import Tool

from forge.prompts.coder import (
    TIER2_SYSTEM_PROMPT,
    TIER3_SYSTEM_PROMPT,
    coder_task_prompt,
)
from forge.prompts.test_generator import (
    SYSTEM_PROMPT as TEST_GEN_SYSTEM_PROMPT,
    test_generator_task_prompt,
)
from forge.prompts.code_reviewer import (
    SYSTEM_PROMPT as REVIEWER_SYSTEM_PROMPT,
    code_reviewer_task_prompt,
)
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    ForgeCodeReviewResult,
    ReviewDecision,
    TestGeneratorResult,
)

from . import router

logger = logging.getLogger(__name__)

# Tools available to coding agents (includes NotebookEdit for .ipynb files)
_CODER_TOOLS = [Tool.READ, Tool.WRITE, Tool.EDIT, Tool.BASH, Tool.GLOB, Tool.GREP, Tool.NOTEBOOK_EDIT]


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


# ── Agent 7: Coder Tier 2 (Scoped Fixes) ─────────────────────────────


@router.reasoner()
async def run_coder_tier2(
    finding: dict,
    worktree_path: str,
    codebase_map: dict | None = None,
    review_feedback: str = "",
    iteration: int = 1,
    model: str = "anthropic/claude-sonnet-4.6",
    ai_provider: str = "opencode",
    max_turns: int = 30,
) -> dict:
    """Agent 7: Apply a scoped fix (1-3 files) for a Tier 2 finding.

    Uses opencode provider with full file tools (Read/Write/Edit/Bash/Glob/Grep).
    Model: Sonnet 4.6 — NON-NEGOTIABLE.
    """
    finding_obj = AuditFinding(**finding)
    logger.info(
        "Agent 7: Coder Tier 2 starting — %s (iteration %d)",
        finding_obj.title, iteration,
    )

    finding_json = json.dumps(finding, indent=2, default=str)
    codebase_map_json = json.dumps(codebase_map, indent=2, default=str) if codebase_map else ""

    task = coder_task_prompt(
        finding_json=finding_json,
        relevant_files="(Agent will discover relevant files via Read/Glob tools)",
        codebase_map_json=codebase_map_json,
        review_feedback=review_feedback,
        iteration=iteration,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=worktree_path,
        max_turns=max_turns,
        allowed_tools=[t.value for t in _CODER_TOOLS],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="coder_tier2",
    ))

    response = await ai.run(task, system_prompt=TIER2_SYSTEM_PROMPT)

    # Extract results — coder may output JSON or just text
    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    # Determine outcome from tool uses
    files_changed = data.get("files_changed", [])
    if not files_changed:
        # Infer from tool uses
        for tu in response.tool_uses:
            if tu.name in ("Write", "Edit") and "file_path" in tu.input:
                fp = tu.input["file_path"]
                if fp not in files_changed:
                    files_changed.append(fp)

    outcome = FixOutcome.COMPLETED if files_changed else FixOutcome.FAILED_RETRYABLE

    result = CoderFixResult(
        finding_id=finding_obj.id,
        outcome=outcome,
        files_changed=files_changed,
        summary=data.get("summary", response.text[:500] if response.text else ""),
        tests_passed=data.get("tests_passed"),
        error_message="" if outcome == FixOutcome.COMPLETED else "No files changed",
        iteration=iteration,
    )

    logger.info(
        "Agent 7: Tier 2 %s — %s (%d files changed)",
        result.outcome.value, finding_obj.title, len(files_changed),
    )
    return result.model_dump()


# ── Agent 8: Coder Tier 3 (Architectural Fixes) ──────────────────────


@router.reasoner()
async def run_coder_tier3(
    finding: dict,
    worktree_path: str,
    codebase_map: dict | None = None,
    review_feedback: str = "",
    iteration: int = 1,
    model: str = "anthropic/claude-sonnet-4.6",
    ai_provider: str = "opencode",
    max_turns: int = 60,
) -> dict:
    """Agent 8: Apply an architectural fix (5-15 files) for a Tier 3 finding.

    Uses opencode provider with full file tools.
    Model: Sonnet 4.6 — NON-NEGOTIABLE.
    Higher max_turns than Tier 2 due to larger scope.
    """
    finding_obj = AuditFinding(**finding)
    logger.info(
        "Agent 8: Coder Tier 3 starting — %s (iteration %d)",
        finding_obj.title, iteration,
    )

    finding_json = json.dumps(finding, indent=2, default=str)
    codebase_map_json = json.dumps(codebase_map, indent=2, default=str) if codebase_map else ""

    task = coder_task_prompt(
        finding_json=finding_json,
        relevant_files="(Agent will discover relevant files via Read/Glob tools)",
        codebase_map_json=codebase_map_json,
        review_feedback=review_feedback,
        iteration=iteration,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=worktree_path,
        max_turns=max_turns,
        allowed_tools=[t.value for t in _CODER_TOOLS],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="coder_tier3",
    ))

    response = await ai.run(task, system_prompt=TIER3_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    files_changed = data.get("files_changed", [])
    if not files_changed:
        for tu in response.tool_uses:
            if tu.name in ("Write", "Edit") and "file_path" in tu.input:
                fp = tu.input["file_path"]
                if fp not in files_changed:
                    files_changed.append(fp)

    outcome = FixOutcome.COMPLETED if files_changed else FixOutcome.FAILED_RETRYABLE

    result = CoderFixResult(
        finding_id=finding_obj.id,
        outcome=outcome,
        files_changed=files_changed,
        summary=data.get("summary", response.text[:500] if response.text else ""),
        tests_passed=data.get("tests_passed"),
        error_message="" if outcome == FixOutcome.COMPLETED else "No files changed",
        iteration=iteration,
    )

    logger.info(
        "Agent 8: Tier 3 %s — %s (%d files changed)",
        result.outcome.value, finding_obj.title, len(files_changed),
    )
    return result.model_dump()


# ── Agent 9: Test Generator ──────────────────────────────────────────


@router.reasoner()
async def run_test_generator(
    finding: dict,
    code_change: dict,
    worktree_path: str,
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "opencode",
    max_turns: int = 20,
) -> dict:
    """Agent 9: Generate tests for a fix applied by the Coder agent.

    Runs in the same worktree as the coder so it can read the changes
    and existing test infrastructure.
    """
    finding_obj = AuditFinding(**finding)
    logger.info("Agent 9: Test Generator starting for %s", finding_obj.title)

    task = test_generator_task_prompt(
        finding_json=json.dumps(finding, indent=2, default=str),
        code_change_json=json.dumps(code_change, indent=2, default=str),
        existing_tests="(Agent will discover existing tests via Glob/Read)",
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=worktree_path,
        max_turns=max_turns,
        allowed_tools=[t.value for t in _CODER_TOOLS],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="test_generator",
    ))

    response = await ai.run(task, system_prompt=TEST_GEN_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    test_files = data.get("test_files_created", [])
    if not test_files:
        for tu in response.tool_uses:
            if tu.name in ("Write", "Edit") and "file_path" in tu.input:
                fp = tu.input["file_path"]
                if fp not in test_files:
                    test_files.append(fp)

    result = TestGeneratorResult(
        finding_id=finding_obj.id,
        test_files_created=test_files,
        tests_written=data.get("tests_written", len(test_files)),
        tests_passing=data.get("tests_passing", 0),
        coverage_summary=data.get("coverage_summary", ""),
        summary=data.get("summary", ""),
    )

    logger.info("Agent 9: Complete — %d test files for %s", len(test_files), finding_obj.title)
    return result.model_dump()


# ── Agent 10: Code Reviewer ──────────────────────────────────────────


@router.reasoner()
async def run_code_reviewer(
    finding: dict,
    code_change: dict,
    code_diff: str = "",
    codebase_map: dict | None = None,
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
) -> dict:
    """Agent 10: Review a fix for correctness, safety, and consistency.

    Uses openrouter_direct (read-only analysis, no file tools needed).
    """
    finding_obj = AuditFinding(**finding)
    logger.info("Agent 10: Code Reviewer starting for %s", finding_obj.title)

    task = code_reviewer_task_prompt(
        finding_json=json.dumps(finding, indent=2, default=str),
        code_change_json=json.dumps(code_change, indent=2, default=str),
        codebase_map_json=json.dumps(codebase_map, indent=2, default=str) if codebase_map else "",
        code_diff=code_diff,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=".",
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="code_reviewer",
    ))

    response = await ai.run(task, system_prompt=REVIEWER_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    # Parse decision — default to APPROVE if unclear (bias toward accepting valid fixes)
    decision_str = str(data.get("decision", "APPROVE")).upper()
    try:
        decision = ReviewDecision(decision_str)
    except ValueError:
        decision = ReviewDecision.APPROVE

    result = ForgeCodeReviewResult(
        finding_id=finding_obj.id,
        decision=decision,
        summary=data.get("summary", ""),
        issues=data.get("issues", []),
        suggestions=data.get("suggestions", []),
        regression_risk=data.get("regression_risk", "LOW"),
    )

    logger.info(
        "Agent 10: %s — %s (risk: %s)",
        result.decision.value, finding_obj.title, result.regression_risk,
    )
    return result.model_dump()


# ── Escalation Agent (Middle Loop) ─────────────────────────────────


@router.reasoner()
async def run_escalation_agent(
    system_prompt: str,
    task_prompt: str,
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
) -> dict:
    """LLM-based escalation agent for the middle loop.

    Decides RECLASSIFY/SPLIT/DEFER/ESCALATE when the inner loop
    is exhausted. Uses openrouter_direct (read-only analysis).
    """
    logger.info("Escalation agent: starting analysis")

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=".",
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="escalation_agent",
    ))

    response = await ai.run(task_prompt, system_prompt=system_prompt)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    if not data:
        data = {"text": response.text or ""}

    logger.info("Escalation agent: decided %s", data.get("action", "unknown"))
    return data
