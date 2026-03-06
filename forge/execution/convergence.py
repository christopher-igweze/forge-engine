"""Convergence loop — iterates remediation until production readiness target is met.

Architecture:
  Iteration 0: normal remediation + validation (baseline score)
  Iteration 1+: delta discovery → merge findings → re-triage → remediation → validation
  Exit when: score >= target OR stalling OR max iterations

Safety mechanisms:
  - Hard cap: max_convergence_iterations (default 3)
  - Stall detection: stop if score improves < convergence_min_improvement
  - Cost tracking via existing telemetry
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from forge.app_helpers import NODE_ID
from forge.schemas import (
    AuditFinding,
    ConvergenceIterationRecord,
    ConvergenceResult,
    FindingCategory,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    OuterLoopState,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)


async def run_convergence_loop(
    dispatcher,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    tier1_findings: list[dict] | None = None,
) -> ConvergenceResult:
    """Run the convergence loop until score >= target or safety limits hit.

    Args:
        dispatcher: AgentField app or StandaloneDispatcher.
        state: Current execution state (already has discovery + triage done).
        cfg: FORGE config with convergence settings.
        resolved_models: Model ID map.
        tier1_findings: Tier 1 deterministic findings from initial scan.

    Returns:
        ConvergenceResult with iteration records and final score.
    """
    from forge.phases import _run_remediation, _run_triage, _run_validation

    result = ConvergenceResult()
    target = cfg.convergence_target_score
    max_iters = cfg.max_convergence_iterations
    min_improvement = cfg.convergence_min_improvement
    prev_score = 0

    for iteration in range(max_iters):
        state.convergence_iteration = iteration
        logger.info(
            "Convergence loop: iteration %d/%d (target: %d)",
            iteration, max_iters, target,
        )

        record = ConvergenceIterationRecord(
            iteration=iteration,
            score_before=prev_score,
            findings_total=len(state.all_findings),
        )

        # ── Re-triage if iteration > 0 (with convergence context) ─────
        if iteration > 0:
            convergence_ctx = _build_convergence_context(state, target)
            triage_result = await _run_triage(
                dispatcher, state, cfg, resolved_models,
                tier1_findings=tier1_findings,
                convergence_context=convergence_ctx,
            )
            state.total_agent_invocations += triage_result["invocations"]

        # ── Remediation ───────────────────────────────────────────────
        remediation_result = await _run_remediation(
            dispatcher, state, cfg, resolved_models,
        )
        state.total_agent_invocations += remediation_result["invocations"]

        # Track files changed this iteration
        state.files_changed_this_run = []
        for fix in state.completed_fixes:
            if fix.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT):
                state.files_changed_this_run.extend(fix.files_changed)

        # ── Validation ────────────────────────────────────────────────
        validation_result = await _run_validation(
            dispatcher, state, cfg, resolved_models,
        )
        state.total_agent_invocations += validation_result["invocations"]

        # Extract score
        current_score = (
            state.readiness_report.overall_score
            if state.readiness_report
            else 0
        )
        record.score_after = current_score
        record.findings_fixed = len([
            f for f in state.completed_fixes
            if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
        ])
        record.findings_deferred = len(state.outer_loop.deferred_findings)

        # Extract low-scoring categories
        if state.readiness_report and state.readiness_report.category_scores:
            record.low_categories = [
                cs.name for cs in state.readiness_report.category_scores
                if cs.score < 70
            ]

        state.convergence_records.append(record)
        result.iteration_records.append(record)

        logger.info(
            "Convergence: iteration %d score=%d (prev=%d, target=%d)",
            iteration, current_score, prev_score, target,
        )

        # ── Check convergence ─────────────────────────────────────────
        if current_score >= target:
            logger.info("Convergence: target reached! Score=%d >= %d", current_score, target)
            result.converged = True
            result.final_score = current_score
            result.iterations_run = iteration + 1
            return result

        # ── Stall detection ───────────────────────────────────────────
        improvement = current_score - prev_score
        if iteration > 0 and improvement < min_improvement:
            logger.info(
                "Convergence: stalling — improvement %d < %d threshold",
                improvement, min_improvement,
            )
            result.converged = False
            result.final_score = current_score
            result.iterations_run = iteration + 1
            return result

        prev_score = current_score

        # ── Last iteration check — don't do delta discovery if we're done
        if iteration == max_iters - 1:
            break

        # ── Delta Discovery + Merge Findings ──────────────────────────
        logger.info("Convergence: running delta discovery on changed files")
        new_findings = await _run_delta_discovery(
            dispatcher, state, cfg, resolved_models,
        )

        # Merge findings for next iteration
        merged = merge_findings(
            existing=state.all_findings,
            new_findings=new_findings,
            completed_fixes=state.completed_fixes,
            deferred_ids=set(state.outer_loop.deferred_findings),
            integration_result=state.integration_result,
            escalate_dropped=cfg.convergence_escalate_dropped,
            prior_plan=state.remediation_plan,
        )

        record.findings_new = len(new_findings)

        # Reset state for next iteration
        state.prior_iteration_findings = list(state.all_findings)
        state.all_findings = merged
        state.remediation_plan = None
        state.inner_loop_states = {}
        # Keep completed_fixes for cumulative tracking
        # Reset outer loop for fresh escalation tracking
        state.outer_loop = OuterLoopState()

        logger.info(
            "Convergence: merged %d findings for iteration %d (%d new)",
            len(merged), iteration + 1, len(new_findings),
        )

    # Max iterations reached
    result.converged = False
    result.final_score = prev_score
    result.iterations_run = max_iters
    logger.info(
        "Convergence: max iterations reached (%d). Final score=%d",
        max_iters, prev_score,
    )
    return result


async def _run_delta_discovery(
    dispatcher,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> list[AuditFinding]:
    """Run discovery on only the files changed during remediation.

    Skips Agent 1 (Codebase Analyst) — repo structure unchanged.
    Runs Agents 2-4 (Security, Quality, Architecture) scoped to changed files.

    Returns new findings discovered in the changed files.
    """
    import asyncio

    changed_files = list(set(state.files_changed_this_run))
    if not changed_files:
        logger.info("Delta discovery: no files changed, skipping")
        return []

    logger.info("Delta discovery: scanning %d changed files", len(changed_files))

    # Build a focused codebase context with only changed files
    codebase_map_dict = (
        state.codebase_map.model_dump()
        if state.codebase_map and hasattr(state.codebase_map, "model_dump")
        else state.codebase_map if isinstance(state.codebase_map, dict)
        else {}
    )

    # Scope the scan to changed files by injecting them into the prompt context
    # We pass the changed_files list so auditors focus on those
    changed_files_str = "\n".join(changed_files[:50])  # Cap at 50 to stay in context

    # Build prior findings context so agents don't re-report known issues
    changed_set = set(changed_files)
    prior_on_changed: list[AuditFinding] = []
    for f in state.all_findings:
        if f.locations:
            for loc in f.locations:
                if loc.file_path in changed_set:
                    prior_on_changed.append(f)
                    break

    prior_str = ""
    if prior_on_changed:
        lines = [f"  - [{f.severity.value}] {f.title}" for f in prior_on_changed[:30]]
        prior_str = (
            "\n## ALREADY REPORTED — DO NOT RE-REPORT\n"
            "These issues are already tracked. Do NOT report these or variations:\n"
            + "\n".join(lines) + "\n"
        )

    # Build project context — scoped to regressions only
    project_context_str = (
        f"## DELTA SCAN — REGRESSIONS ONLY\n"
        f"Targeted re-scan after remediation fixes were applied.\n"
        f"Changed files:\n{changed_files_str}\n\n"
        f"CRITICAL: ONLY report issues INTRODUCED by the changes.\n"
        f"Do NOT report pre-existing issues in the same files.\n"
        f"Do NOT report general code quality observations.\n"
        f"Valid: fix added try/catch but exposes raw exception details.\n"
        f"Invalid: file was always missing input validation.\n"
        + prior_str
    )

    # Run Agents 2-4 in parallel on changed files
    coros = [
        dispatcher.call(
            f"{NODE_ID}.run_security_auditor",
            repo_path=state.repo_path,
            codebase_map=codebase_map_dict,
            artifacts_dir=state.artifacts_dir,
            model=resolved_models.get("security_auditor_model", "anthropic/claude-haiku-4.5"),
            ai_provider=cfg.provider_for_role("security_auditor"),
            parallel=cfg.enable_parallel_audit,
            pattern_library_path=cfg.pattern_library_path,
            project_context=project_context_str,
        ),
        dispatcher.call(
            f"{NODE_ID}.run_quality_auditor",
            repo_path=state.repo_path,
            codebase_map=codebase_map_dict,
            artifacts_dir=state.artifacts_dir,
            model=resolved_models.get("quality_auditor_model", "minimax/minimax-m2.5"),
            ai_provider=cfg.provider_for_role("quality_auditor"),
            project_context=project_context_str,
        ),
        dispatcher.call(
            f"{NODE_ID}.run_architecture_reviewer",
            repo_path=state.repo_path,
            codebase_map=codebase_map_dict,
            artifacts_dir=state.artifacts_dir,
            model=resolved_models.get("architecture_reviewer_model", "anthropic/claude-haiku-4.5"),
            ai_provider=cfg.provider_for_role("architecture_reviewer"),
            project_context=project_context_str,
        ),
    ]

    results = await asyncio.gather(*coros, return_exceptions=True)

    new_findings: list[AuditFinding] = []
    for i, r in enumerate(results):
        agent_names = ["security_auditor", "quality_auditor", "architecture_reviewer"]
        if isinstance(r, Exception):
            logger.error("Delta discovery agent %s failed: %s", agent_names[i], r)
            continue
        if isinstance(r, dict):
            for f_data in r.get("findings", []):
                try:
                    finding = AuditFinding(**f_data)
                    finding.agent = f"delta_{agent_names[i]}"
                    new_findings.append(finding)
                except Exception as e:
                    logger.warning("Failed to parse delta finding: %s", e)

    logger.info("Delta discovery: found %d new findings", len(new_findings))

    return new_findings


def merge_findings(
    *,
    existing: list[AuditFinding],
    new_findings: list[AuditFinding],
    completed_fixes: list,
    deferred_ids: set[str],
    integration_result=None,
    escalate_dropped: bool = True,
    prior_plan=None,
) -> list[AuditFinding]:
    """Merge findings across iterations for the convergence loop.

    Logic:
    1. Remove fixed findings from active set
    2. Keep unresolved findings (not fixed, not deferred)
    3. Add new findings from delta discovery
    4. Re-inject deferred findings with must_fix actionability
    5. Convert integration validator's new_issues_introduced to findings
    6. Deduplicate by dedup_key or (title + first location)
    """
    fixed_ids = {
        f.finding_id for f in completed_fixes
        if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
    }

    # Pre-compute IDs dropped by the Fix Strategist (not in plan, not fixed)
    dropped_by_strategist: set[str] = set()
    if escalate_dropped and prior_plan:
        planned_ids = {item.finding_id for item in prior_plan.items}
        dropped_by_strategist = {
            f.id for f in existing
            if f.id not in planned_ids and f.id not in fixed_ids
        }

    # Start with unresolved findings from previous iteration
    merged: list[AuditFinding] = []
    seen_keys: set[str] = set()

    for f in existing:
        if f.id in fixed_ids:
            continue  # Successfully fixed — remove from active set

        key = _dedup_key(f)
        if key not in seen_keys:
            # Re-inject deferred findings with elevated priority
            if f.id in deferred_ids and escalate_dropped:
                f.actionability = "must_fix"
            # Escalate findings dropped by Fix Strategist
            elif f.id in dropped_by_strategist:
                f.actionability = "must_fix"
            merged.append(f)
            seen_keys.add(key)

    # Add new findings from delta discovery
    for f in new_findings:
        key = _dedup_key(f)
        if key not in seen_keys:
            merged.append(f)
            seen_keys.add(key)

    # Convert integration validator's new_issues_introduced to findings
    if integration_result and hasattr(integration_result, "new_issues_introduced"):
        for issue_desc in integration_result.new_issues_introduced:
            if not issue_desc or not isinstance(issue_desc, str):
                continue
            new_f = AuditFinding(
                id=f"F-{uuid4().hex[:8]}",
                title=f"Issue introduced by remediation: {issue_desc[:80]}",
                description=issue_desc,
                category=FindingCategory.QUALITY,
                severity=FindingSeverity.HIGH,
                actionability="must_fix",
                agent="integration_validator",
            )
            key = _dedup_key(new_f)
            if key not in seen_keys:
                merged.append(new_f)
                seen_keys.add(key)

    # Re-inject findings that were dropped by Fix Strategist
    if escalate_dropped and prior_plan:
        planned_ids = {item.finding_id for item in prior_plan.items}
        for f in existing:
            if f.id not in planned_ids and f.id not in fixed_ids:
                key = _dedup_key(f)
                if key not in seen_keys:
                    f.actionability = "must_fix"
                    merged.append(f)
                    seen_keys.add(key)

    logger.info(
        "Findings merge: %d existing, %d new, %d fixed → %d merged",
        len(existing), len(new_findings), len(fixed_ids), len(merged),
    )
    return merged


def _dedup_key(f: AuditFinding) -> str:
    """Generate a deduplication key for a finding."""
    if f.dedup_key:
        return f.dedup_key
    location = f.locations[0].file_path if f.locations else ""
    return f"{f.title}|{location}"


def _build_convergence_context(
    state: ForgeExecutionState,
    target_score: int,
) -> str:
    """Build context string for the Fix Strategist about prior iteration results.

    This tells the strategist what scored low, what the debt tracker recommended,
    and what new issues were introduced.
    """
    parts = [f"## Feedback from Previous Iteration"]

    current_score = (
        state.readiness_report.overall_score
        if state.readiness_report
        else 0
    )
    parts.append(f"Score: {current_score}/100 (target: {target_score})")

    # Low-scoring categories
    if state.readiness_report and state.readiness_report.category_scores:
        low = [
            f"{cs.name} ({cs.score})"
            for cs in state.readiness_report.category_scores
            if cs.score < 70
        ]
        if low:
            parts.append(f"Low categories: {', '.join(low)}")

    # Recommendations from debt tracker
    if state.readiness_report and state.readiness_report.recommendations:
        recs = []
        for r in state.readiness_report.recommendations[:5]:
            title = r.title if hasattr(r, "title") else str(r)
            recs.append(f"  - {title}")
        parts.append("Recommendations:\n" + "\n".join(recs))

    # Issues introduced by previous fixes
    if state.integration_result and state.integration_result.new_issues_introduced:
        parts.append(
            "MUST-FIX issues introduced by previous fixes:\n"
            + "\n".join(f"  - {i}" for i in state.integration_result.new_issues_introduced[:10])
        )

    # Deferred findings that must now be addressed — with failure context
    if state.outer_loop.deferred_findings:
        deferred_lines = []
        finding_map = {f.id: f for f in state.all_findings}
        for fid in state.outer_loop.deferred_findings[:10]:
            f = finding_map.get(fid)
            if not f:
                continue
            line = f"  - [{f.severity.value}] {f.title}"
            # Attach failure context so Fix Strategist knows WHY it failed
            ctx = state.outer_loop.deferred_context.get(fid)
            if ctx:
                details = []
                if ctx.review_feedback:
                    details.append(f"Review: {ctx.review_feedback}")
                if ctx.test_output:
                    details.append(f"Test output: {ctx.test_output}")
                if ctx.escalation_reason:
                    details.append(f"Escalation: {ctx.escalation_reason}")
                if details:
                    line += "\n    " + "\n    ".join(details)
            deferred_lines.append(line)
        if deferred_lines:
            parts.append(
                "Previously deferred findings that MUST be planned.\n"
                "Use the failure context below to guide the coder on a DIFFERENT approach:\n"
                + "\n".join(deferred_lines)
            )

    return "\n".join(parts)
