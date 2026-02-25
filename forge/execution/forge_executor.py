"""FORGE three-loop control system.

Adapts SWE-AF's three-nested-loop architecture with triggers specific
to remediation rather than construction.

Inner Loop:  Coder retry on REQUEST_CHANGES (max 3)
Middle Loop: Escalation when inner loop exhausted (RECLASSIFY/SPLIT/DEFER/ESCALATE)
Outer Loop:  Re-run Fix Strategist on remaining plan (max 1)

Philosophy: "First, do no harm." If a fix can't be applied cleanly within
3 attempts, defer it as documented technical debt rather than risk
destabilizing the codebase.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

try:
    from agentfield import Agent
except ImportError:
    from typing import Any as Agent  # Standalone: accepts StandaloneDispatcher

from forge.execution.json_utils import safe_parse_agent_response
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    EscalationAction,
    EscalationDecision,
    FixOutcome,
    ForgeCodeReviewResult,
    ForgeExecutionState,
    InnerLoopState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
    ReviewDecision,
    TestGeneratorResult,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)


# ── Inner Loop: Coder → Review → Retry ───────────────────────────────


async def run_inner_loop(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    worktree_path: str,
    codebase_map: dict | None,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> InnerLoopState:
    """Execute the inner control loop for a single finding.

    Coder produces a fix → Code Reviewer evaluates → retry if REQUEST_CHANGES.
    Max iterations controlled by cfg.max_inner_retries.
    """
    loop_state = InnerLoopState(
        finding_id=finding.id,
        max_iterations=cfg.max_inner_retries,
    )

    finding_dict = finding.model_dump()

    # Select coder based on tier
    if item.tier == RemediationTier.TIER_3:
        coder_reasoner = f"{node_id}.run_coder_tier3"
        coder_model = resolved_models.get("coder_tier3_model", "anthropic/claude-sonnet-4.6")
    else:
        coder_reasoner = f"{node_id}.run_coder_tier2"
        coder_model = resolved_models.get("coder_tier2_model", "anthropic/claude-sonnet-4.6")

    review_feedback = ""

    for iteration in range(1, cfg.max_inner_retries + 1):
        loop_state.iteration = iteration
        logger.info(
            "Inner loop: %s — iteration %d/%d",
            finding.title, iteration, cfg.max_inner_retries,
        )

        # ── Step 1: Coder ──────────────────────────────────────────────
        coder_result_dict = await app.call(
            coder_reasoner,
            finding=finding_dict,
            worktree_path=worktree_path,
            codebase_map=codebase_map,
            review_feedback=review_feedback,
            iteration=iteration,
            model=coder_model,
            ai_provider=cfg.provider_for_role(
                "coder_tier3" if item.tier == RemediationTier.TIER_3 else "coder_tier2"
            ),
        )

        coder_result = CoderFixResult(**_unwrap(coder_result_dict))
        loop_state.coder_result = coder_result

        if coder_result.outcome in (FixOutcome.FAILED_RETRYABLE, FixOutcome.FAILED_ESCALATED):
            review_feedback = coder_result.error_message
            continue

        # ── Step 2: Test Generator + Code Reviewer (parallel) ──────────
        test_coro = app.call(
            f"{node_id}.run_test_generator",
            finding=finding_dict,
            code_change=coder_result.model_dump(),
            worktree_path=worktree_path,
            model=resolved_models.get("test_generator_model", "anthropic/claude-haiku-4.5"),
            ai_provider=cfg.provider_for_role("test_generator"),
        )
        review_coro = app.call(
            f"{node_id}.run_code_reviewer",
            finding=finding_dict,
            code_change=coder_result.model_dump(),
            codebase_map=codebase_map,
            model=resolved_models.get("code_reviewer_model", "anthropic/claude-haiku-4.5"),
            ai_provider=cfg.provider_for_role("code_reviewer"),
        )

        test_raw, review_raw = await asyncio.gather(
            test_coro, review_coro, return_exceptions=True,
        )

        # Parse test result
        if isinstance(test_raw, Exception):
            logger.error("Test generator failed: %s", test_raw)
            loop_state.test_result = TestGeneratorResult(finding_id=finding.id)
        else:
            loop_state.test_result = TestGeneratorResult(**_unwrap(test_raw))

        # Parse review result
        if isinstance(review_raw, Exception):
            logger.error("Code reviewer failed: %s", review_raw)
            loop_state.review_result = ForgeCodeReviewResult(
                finding_id=finding.id,
                decision=ReviewDecision.REQUEST_CHANGES,
                summary=f"Review failed: {review_raw}",
            )
        else:
            loop_state.review_result = ForgeCodeReviewResult(**_unwrap(review_raw))

        # ── Step 3: Decision ───────────────────────────────────────────
        if loop_state.review_result.decision == ReviewDecision.APPROVE:
            coder_result.outcome = FixOutcome.COMPLETED
            loop_state.coder_result = coder_result
            logger.info("Inner loop: APPROVED — %s", finding.title)
            return loop_state

        if loop_state.review_result.decision == ReviewDecision.BLOCK:
            coder_result.outcome = FixOutcome.FAILED_ESCALATED
            loop_state.coder_result = coder_result
            logger.info("Inner loop: BLOCKED — %s", finding.title)
            return loop_state

        # REQUEST_CHANGES — retry with feedback
        review_feedback = loop_state.review_result.summary
        logger.info("Inner loop: REQUEST_CHANGES — retrying %s", finding.title)

    # Exhausted retries
    if loop_state.coder_result:
        loop_state.coder_result.outcome = FixOutcome.FAILED_RETRYABLE
    logger.info(
        "Inner loop: exhausted %d retries for %s",
        cfg.max_inner_retries, finding.title,
    )
    return loop_state


# ── Middle Loop: Escalation ───────────────────────────────────────────


async def run_middle_loop(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    inner_state: InnerLoopState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str] | None = None,
) -> EscalationDecision:
    """Execute the middle loop when the inner loop is exhausted.

    Decides: RECLASSIFY, SPLIT, DEFER, or ESCALATE.
    Uses an LLM escalation agent with heuristic fallback.
    """
    logger.info("Middle loop: evaluating %s", finding.title)

    review = inner_state.review_result

    # Fast path: BLOCKED by reviewer — defer immediately (no LLM needed)
    if review and review.decision == ReviewDecision.BLOCK:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.DEFER,
            rationale=f"Blocked by reviewer: {review.summary}",
        )

    # Try LLM escalation agent
    if resolved_models:
        try:
            return await _llm_escalation(
                app, node_id, item, finding, inner_state, cfg, resolved_models,
            )
        except Exception as e:
            logger.warning("LLM escalation agent failed, falling back to heuristic: %s", e)

    # Heuristic fallback
    return _heuristic_escalation(item, finding)


async def _llm_escalation(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    inner_state: InnerLoopState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> EscalationDecision:
    """Use the LLM escalation agent to decide the next action."""
    from forge.prompts.escalation_agent import (
        ESCALATION_SYSTEM_PROMPT,
        build_escalation_task,
    )

    task_prompt = build_escalation_task(
        finding=finding.model_dump(),
        coder_result=inner_state.coder_result.model_dump() if inner_state.coder_result else None,
        review_result=inner_state.review_result.model_dump() if inner_state.review_result else None,
        current_tier=item.tier.value,
        iteration_count=inner_state.iteration,
    )

    model = resolved_models.get("fix_strategist_model", "anthropic/claude-haiku-4.5")
    provider = cfg.provider_for_role("fix_strategist")

    result = await app.call(
        f"{node_id}.run_escalation_agent",
        system_prompt=ESCALATION_SYSTEM_PROMPT,
        task_prompt=task_prompt,
        model=model,
        ai_provider=provider,
    )

    # Parse the response
    result_dict = safe_parse_agent_response(result)
    if not result_dict:
        raise ValueError("Empty response from escalation agent")

    action_str = result_dict.get("action", "DEFER").upper()
    action = (
        EscalationAction(action_str)
        if action_str in EscalationAction.__members__
        else EscalationAction.DEFER
    )

    decision = EscalationDecision(
        finding_id=finding.id,
        action=action,
        rationale=result_dict.get("rationale", "LLM escalation decision"),
    )

    if action == EscalationAction.RECLASSIFY:
        new_tier_val = result_dict.get("new_tier", 3)
        decision.new_tier = RemediationTier(new_tier_val)

    if action == EscalationAction.SPLIT:
        split_items_raw = result_dict.get("split_items", [])
        for si in split_items_raw:
            decision.split_items.append(RemediationItem(
                finding_id=f"{finding.id}-split-{len(decision.split_items) + 1}",
                title=si.get("title", finding.title),
                tier=RemediationTier.TIER_2,
                priority=item.priority,
                estimated_files=si.get("estimated_files", 1),
            ))

    logger.info("LLM escalation: %s → %s (%s)", finding.id, action.value, decision.rationale)
    return decision


def _heuristic_escalation(
    item: RemediationItem,
    finding: AuditFinding,
) -> EscalationDecision:
    """Fallback heuristic escalation logic."""
    if item.tier == RemediationTier.TIER_2:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.RECLASSIFY,
            rationale="Tier 2 fix failed after retries — promoting to Tier 3 for broader context",
            new_tier=RemediationTier.TIER_3,
        )

    if item.tier == RemediationTier.TIER_3:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.DEFER,
            rationale="Tier 3 fix failed after retries — deferring as technical debt",
        )

    return EscalationDecision(
        finding_id=finding.id,
        action=EscalationAction.DEFER,
        rationale="Could not fix within retry budget",
    )


# ── Outer Loop: Re-plan ──────────────────────────────────────────────


async def run_outer_loop(
    app: Agent,
    node_id: str,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> RemediationPlan | None:
    """Execute the outer loop — re-run Fix Strategist on remaining items.

    Only triggers when multiple fixes in the same module are failing
    or dependency conflicts are detected.
    """
    if state.outer_loop.iteration >= cfg.max_outer_replans:
        logger.info("Outer loop: max replans reached (%d)", cfg.max_outer_replans)
        return None

    escalated = [e for e in state.outer_loop.escalations if e.action == EscalationAction.ESCALATE]
    if not escalated:
        logger.info("Outer loop: no escalations requiring replan")
        return None

    logger.info("Outer loop: replanning with %d escalations", len(escalated))
    state.outer_loop.iteration += 1

    # Re-run Fix Strategist with remaining findings (excluding completed/deferred)
    completed_ids = {f.finding_id for f in state.completed_fixes}
    deferred_ids = set(state.outer_loop.deferred_findings)
    excluded = completed_ids | deferred_ids

    remaining_findings = [
        f.model_dump() for f in state.all_findings
        if f.id not in excluded
    ]

    if not remaining_findings:
        logger.info("Outer loop: no remaining findings to replan")
        return None

    codebase_map_dict = state.codebase_map.model_dump() if state.codebase_map else {}

    plan_dict = await app.call(
        f"{node_id}.run_fix_strategist",
        all_findings=remaining_findings,
        codebase_map=codebase_map_dict,
        triage_result=state.triage_result.model_dump() if state.triage_result else None,
        model=resolved_models.get("fix_strategist_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("fix_strategist"),
    )

    try:
        return RemediationPlan(**_unwrap(plan_dict))
    except Exception as e:
        logger.error("Outer loop: failed to parse replan: %s", e)
        return None


# ── Full Remediation Executor ─────────────────────────────────────────


async def execute_remediation(
    app: Agent,
    node_id: str,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> None:
    """Execute the full remediation phase with three control loops.

    Iterates through the remediation plan level by level,
    running fixes in parallel within each level.
    """
    plan = state.remediation_plan
    if not plan or not plan.items:
        logger.info("Remediation: no items in plan")
        return

    # Build finding lookup
    finding_map: dict[str, AuditFinding] = {f.id: f for f in state.all_findings}

    # Execute level by level
    for level_idx, level_ids in enumerate(plan.execution_levels):
        logger.info("Remediation: level %d — %d items", level_idx, len(level_ids))

        # Get items for this level
        item_map = {i.finding_id: i for i in plan.items}
        level_items = [
            item_map[fid] for fid in level_ids
            if fid in item_map and fid not in state.outer_loop.deferred_findings
        ]

        if not level_items:
            continue

        # Run fixes in parallel within each level
        tasks = []
        for item in level_items:
            finding = finding_map.get(item.finding_id)
            if not finding:
                logger.warning("Finding %s not found — skipping", item.finding_id)
                continue

            # Skip Tier 0 and Tier 1 (handled by tier_router)
            if item.tier in (RemediationTier.TIER_0, RemediationTier.TIER_1):
                continue

            tasks.append(_execute_single_fix(
                app, node_id, item, finding, state, cfg, resolved_models,
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Outer loop: replan if needed ───────────────────────────────────
    new_plan = await run_outer_loop(app, node_id, state, cfg, resolved_models)
    if new_plan and new_plan.items:
        logger.info("Outer loop: replanning with %d items", len(new_plan.items))
        state.remediation_plan = new_plan
        # Re-execute with new plan (recursive, but max 1 replan)
        await execute_remediation(app, node_id, state, cfg, resolved_models)


async def _execute_single_fix(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> None:
    """Execute a single fix through inner + middle loops.

    Each fix runs in an isolated git worktree to avoid conflicts
    with parallel fixes.
    """
    from forge.execution.worktree import (
        create_worktree,
        merge_worktree,
        remove_worktree,
        get_current_branch,
    )

    codebase_map = state.codebase_map.model_dump() if state.codebase_map else None

    # Create isolated worktree for this fix
    try:
        worktree_path = create_worktree(
            state.repo_path,
            finding.id,
            base_branch=get_current_branch(state.repo_path),
        )
    except Exception as e:
        logger.error("Failed to create worktree for %s: %s", finding.id, e)
        worktree_path = state.repo_path  # Fallback to main repo

    try:
        # ── Inner loop ─────────────────────────────────────────────────
        inner_state = await run_inner_loop(
            app, node_id, item, finding, worktree_path, codebase_map, cfg, resolved_models,
        )
        state.inner_loop_states[finding.id] = inner_state
        state.total_agent_invocations += inner_state.iteration * 3  # coder + test + review per iter

        # Check outcome
        if inner_state.coder_result and inner_state.coder_result.outcome == FixOutcome.COMPLETED:
            # Merge worktree back into main branch
            if worktree_path != state.repo_path:
                merged = merge_worktree(
                    state.repo_path,
                    worktree_path,
                    target_branch=get_current_branch(state.repo_path),
                )
                if not merged:
                    logger.warning("Merge failed for %s — marking as debt", finding.id)
                    inner_state.coder_result.outcome = FixOutcome.COMPLETED_WITH_DEBT
            state.completed_fixes.append(inner_state.coder_result)
            return

        # ── Middle loop ────────────────────────────────────────────────
        escalation = await run_middle_loop(
            app, node_id, item, finding, inner_state, cfg, resolved_models,
        )
        state.outer_loop.escalations.append(escalation)

        if escalation.action == EscalationAction.DEFER:
            state.outer_loop.deferred_findings.append(finding.id)
            if inner_state.coder_result:
                inner_state.coder_result.outcome = FixOutcome.DEFERRED
                state.completed_fixes.append(inner_state.coder_result)

        elif escalation.action == EscalationAction.RECLASSIFY and escalation.new_tier:
            # Promote to higher tier and retry
            item.tier = escalation.new_tier
            new_inner = await run_inner_loop(
                app, node_id, item, finding, worktree_path, codebase_map, cfg, resolved_models,
            )
            state.inner_loop_states[finding.id] = new_inner

            if new_inner.coder_result and new_inner.coder_result.outcome == FixOutcome.COMPLETED:
                if worktree_path != state.repo_path:
                    merged = merge_worktree(
                        state.repo_path,
                        worktree_path,
                        target_branch=get_current_branch(state.repo_path),
                    )
                    if not merged:
                        new_inner.coder_result.outcome = FixOutcome.COMPLETED_WITH_DEBT
                state.completed_fixes.append(new_inner.coder_result)
            else:
                state.outer_loop.deferred_findings.append(finding.id)

        elif escalation.action == EscalationAction.ESCALATE:
            # Will be handled by outer loop
            pass

    finally:
        # Clean up worktree (unless it was the fallback)
        if worktree_path != state.repo_path:
            try:
                remove_worktree(state.repo_path, worktree_path)
            except Exception as e:
                logger.warning("Failed to clean up worktree %s: %s", worktree_path, e)


# ── Helpers ───────────────────────────────────────────────────────────


def _unwrap(result) -> dict:
    """Handle AgentField envelope unwrapping with resilient JSON parsing."""
    return safe_parse_agent_response(result)
