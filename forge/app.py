"""AgentField app for the FORGE remediation engine.

Exposes:
  - ``remediate``: full scan -> triage -> fix -> validate pipeline
  - ``discover``:  scan-only mode (Agents 1-5)
  - ``scan``:      alias for discover (free tier)
  - ``fix_single``: fix a single finding (useful for testing)
"""

from __future__ import annotations

import logging
import os
import time

try:
    from agentfield import Agent
except ImportError:
    Agent = None  # Standalone mode — app.py is not used directly

from forge.app_helpers import (  # noqa: F401
    _resolve_repo_path, _build_summary, _filter_execution_levels,
    _unwrap_to_model, NODE_ID, WORKSPACES_DIR,
)
from forge.phases import (  # noqa: F401
    _run_discovery, _run_swarm_discovery, _run_triage,
    _run_remediation, _run_validation,
)

from forge.config import ForgeConfig
from forge.reasoners import router
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    FixOutcome,
    ForgeExecutionState,
    ForgeMode,
    ForgeResult,
    RemediationPlan,
)

logger = logging.getLogger(__name__)

if Agent is not None:
    app = Agent(
        node_id=NODE_ID,
        version="0.1.0",
        description="FORGE: Framework for Orchestrated Remediation & Governance Engine",
        agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
        api_key=os.getenv("AGENTFIELD_API_KEY"),
    )
    app.include_router(router)
else:
    app = None  # Use forge.standalone for CLI mode


def _reasoner_decorator():
    """Return app.reasoner() if AgentField is available, else a no-op."""
    if app is not None:
        return app.reasoner()
    return lambda fn: fn


# ── Main Reasoners ────────────────────────────────────────────────────


@_reasoner_decorator()
async def remediate(
    repo_url: str = "",
    repo_path: str = "",
    config: dict | None = None,
    tier1_findings: list[dict] | None = None,
) -> dict:
    """Full FORGE pipeline: discover -> triage -> fix -> validate.

    This is the primary entry point for FORGE remediation runs.
    """
    start_time = time.time()
    cfg = ForgeConfig(**(config or {}))
    resolved = cfg.resolved_models()

    state = ForgeExecutionState(
        mode=cfg.mode,
        repo_url=repo_url,
    )

    # Initialize telemetry — reuse existing context (e.g. from test fixture)
    # or create a new one.  Activating as contextvar means AgentAI.run()
    # auto-logs every invocation without manual plumbing.
    from forge.execution.telemetry import ForgeTelemetry
    _outer = ForgeTelemetry.current()
    if _outer is not None:
        telemetry = _outer
        _telemetry_ctx = None
    else:
        telemetry = ForgeTelemetry(run_id=state.forge_run_id)
        _telemetry_ctx = telemetry.activate()
        _telemetry_ctx.__enter__()

    try:
        # ── Step 0: Resolve repo path ──────────────────────────────────
        state.repo_path = _resolve_repo_path(repo_url, repo_path or cfg.repo_path)
        state.artifacts_dir = os.path.join(state.repo_path, ".artifacts")
        os.makedirs(state.artifacts_dir, exist_ok=True)

        logger.info("FORGE remediate starting: %s", state.repo_path)

        # ── Recover from prior crashes (stale worktrees) ─────────────────
        try:
            from forge.execution.worktree import recover_worktrees
            recovered = recover_worktrees(state.repo_path)
            if recovered:
                logger.info("Recovered %d stale worktrees from prior crash", len(recovered))
        except Exception as e:
            logger.warning("Worktree recovery failed (non-fatal): %s", e)

        # ── Check for resumable checkpoint ──────────────────────────────
        from forge.execution.checkpoint import (
            CheckpointPhase, save_checkpoint, get_latest_checkpoint,
            restore_state, clear_checkpoints,
        )
        cp = get_latest_checkpoint(state.repo_path)
        resume_phase = None
        if cp and cp.forge_run_id:
            logger.info("Resuming from checkpoint: %s (phase: %s)", cp.forge_run_id, cp.phase.value)
            restored = restore_state(cp)
            # Copy restored fields into state
            state.forge_run_id = restored.forge_run_id
            state.codebase_map = restored.codebase_map
            state.security_findings = restored.security_findings
            state.quality_findings = restored.quality_findings
            state.architecture_findings = restored.architecture_findings
            state.all_findings = restored.all_findings
            state.triage_result = restored.triage_result
            state.remediation_plan = restored.remediation_plan
            state.completed_fixes = restored.completed_fixes
            state.outer_loop = restored.outer_loop
            state.integration_result = restored.integration_result
            state.readiness_report = restored.readiness_report
            state.total_agent_invocations = restored.total_agent_invocations
            resume_phase = cp.phase

        # ── Step 1: Discovery (Agents 1-4) ─────────────────────────────
        if resume_phase not in (
            CheckpointPhase.DISCOVERY, CheckpointPhase.TRIAGE,
            CheckpointPhase.REMEDIATION, CheckpointPhase.VALIDATION,
        ):
            discovery_result = await _run_discovery(
                app, state, cfg, resolved,
            )
            state.total_agent_invocations += discovery_result["invocations"]
            save_checkpoint(state.repo_path, CheckpointPhase.DISCOVERY, state)

        # ── Step 2: Triage (Agents 5-6) ────────────────────────────────
        if resume_phase not in (
            CheckpointPhase.TRIAGE, CheckpointPhase.REMEDIATION,
            CheckpointPhase.VALIDATION,
        ):
            triage_result = await _run_triage(
                app, state, cfg, resolved, tier1_findings,
            )
            state.total_agent_invocations += triage_result["invocations"]
            save_checkpoint(state.repo_path, CheckpointPhase.TRIAGE, state)

        # ── Step 3+4: Remediation + Validation (convergence or single-pass) ──
        if cfg.mode in (ForgeMode.FULL, ForgeMode.REMEDIATION) and not cfg.dry_run:
            if resume_phase not in (
                CheckpointPhase.REMEDIATION, CheckpointPhase.VALIDATION,
            ):
                if cfg.convergence_enabled:
                    from forge.execution.convergence import run_convergence_loop
                    conv_result = await run_convergence_loop(
                        app, state, cfg, resolved, tier1_findings,
                    )
                    logger.info(
                        "Convergence: %s after %d iterations (score=%d)",
                        "converged" if conv_result.converged else "stopped",
                        conv_result.iterations_run, conv_result.final_score,
                    )
                else:
                    remediation_result = await _run_remediation(
                        app, state, cfg, resolved,
                    )
                    state.total_agent_invocations += remediation_result["invocations"]
                    save_checkpoint(state.repo_path, CheckpointPhase.REMEDIATION, state)

        # Validation (only if convergence disabled — loop handles its own)
        if cfg.mode in (ForgeMode.FULL, ForgeMode.VALIDATION) and not cfg.dry_run:
            if not cfg.convergence_enabled:
                if resume_phase != CheckpointPhase.VALIDATION:
                    validation_result = await _run_validation(
                        app, state, cfg, resolved,
                    )
                    state.total_agent_invocations += validation_result["invocations"]
                    save_checkpoint(state.repo_path, CheckpointPhase.VALIDATION, state)

        # Clear checkpoints on successful completion
        clear_checkpoints(state.repo_path)

        state.success = True

    except Exception as e:
        logger.exception("FORGE remediate failed: %s", e)
        state.success = False
    finally:
        # Clean up all FORGE worktrees
        try:
            from forge.execution.worktree import cleanup_all_worktrees
            cleanup_all_worktrees(state.repo_path)
        except Exception as e:
            logger.warning("Worktree cleanup failed: %s", e)
        # Deactivate telemetry context (only if we created it)
        if _telemetry_ctx is not None:
            _telemetry_ctx.__exit__(None, None, None)

    # Finalize
    elapsed = time.time() - start_time

    # Flush telemetry: invocations auto-logged, add training data pairs
    telemetry.artifacts_dir = state.artifacts_dir
    for fix in state.completed_fixes:
        finding = next(
            (f for f in state.all_findings if f.id == fix.finding_id), None,
        )
        if finding:
            inner = state.inner_loop_states.get(fix.finding_id)
            telemetry.log_training_pair(
                finding_id=finding.id,
                category=finding.category.value,
                severity=finding.severity.value,
                title=finding.title,
                description=finding.description,
                tier=finding.tier.value if finding.tier is not None else 2,
                outcome=fix.outcome.value,
                summary=fix.summary,
                files_changed=fix.files_changed,
                retry_count=inner.iteration if inner else 1,
                escalated=fix.finding_id in state.outer_loop.deferred_findings,
            )
    state.estimated_cost_usd = telemetry.total_cost
    telemetry.flush()

    logger.info(
        "Telemetry: $%.4f total, %d tokens, %d invocations",
        telemetry.total_cost, telemetry.total_tokens, len(telemetry.invocations),
    )

    state.finished_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )

    # Generate discovery report and attach to result for storage in Supabase
    discovery_report_data: dict | None = None
    if state.all_findings and state.artifacts_dir:
        try:
            from forge.execution.report import generate_discovery_report
            _paths, discovery_report_data = generate_discovery_report(
                findings=state.all_findings,
                plan=state.remediation_plan,
                artifacts_dir=state.artifacts_dir,
                run_id=state.forge_run_id,
                duration_seconds=elapsed,
                cost_usd=state.estimated_cost_usd,
                codebase_map=state.codebase_map,
            )
        except Exception as e:
            logger.warning("Discovery report generation failed: %s", e, exc_info=True)

    actually_fixed = [
        f for f in state.completed_fixes
        if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
    ]

    result = ForgeResult(
        forge_run_id=state.forge_run_id,
        success=state.success,
        mode=state.mode,
        summary=_build_summary(state),
        total_findings=len(state.all_findings),
        findings_fixed=len(actually_fixed),
        findings_deferred=len(state.outer_loop.deferred_findings),
        agent_invocations=state.total_agent_invocations,
        cost_usd=state.estimated_cost_usd,
        duration_seconds=elapsed,
        convergence_iterations=state.convergence_iteration + 1 if state.convergence_records else 0,
        readiness_report=state.readiness_report,
        discovery_report=discovery_report_data,
    )

    logger.info(
        "FORGE complete: %s — %d findings, %d fixed, %.1fs",
        "SUCCESS" if result.success else "FAILED",
        result.total_findings, result.findings_fixed, elapsed,
    )
    return result.model_dump()


@_reasoner_decorator()
async def discover(
    repo_url: str = "",
    repo_path: str = "",
    config: dict | None = None,
) -> dict:
    """Discovery mode only: Agents 1-5 (scan + triage, no fixes)."""
    cfg_dict = dict(config or {})
    cfg_dict["mode"] = "discovery"
    cfg_dict["dry_run"] = True  # No fixes in discovery mode
    return await remediate(
        repo_url=repo_url,
        repo_path=repo_path,
        config=cfg_dict,
    )


@_reasoner_decorator()
async def scan(
    repo_url: str = "",
    repo_path: str = "",
    config: dict | None = None,
) -> dict:
    """Scan alias for discover — produces readiness score without fixes."""
    return await discover(repo_url=repo_url, repo_path=repo_path, config=config)


@_reasoner_decorator()
async def fix_single(
    repo_path: str = "",
    finding: dict | None = None,
    codebase_map: dict | None = None,
    config: dict | None = None,
) -> dict:
    """Fix a single finding — useful for iterative fixing or testing.

    Takes a finding object (from a prior scan) and runs it through
    the triage -> coder -> reviewer pipeline.
    """
    if not finding:
        return {"success": False, "error": "No finding provided"}

    cfg = ForgeConfig(**(config or {}))
    resolved = cfg.resolved_models()

    rp = repo_path or cfg.repo_path
    if not rp:
        return {"success": False, "error": "No repo_path provided"}

    state = ForgeExecutionState(
        mode=ForgeMode.REMEDIATION,
        repo_path=rp,
        artifacts_dir=os.path.join(rp, ".artifacts"),
    )
    os.makedirs(state.artifacts_dir, exist_ok=True)

    # Parse the finding
    audit_finding = AuditFinding(**finding)
    state.all_findings = [audit_finding]

    # Parse codebase map if provided
    if codebase_map:
        state.codebase_map = CodebaseMap(**codebase_map)

    # Run triage on the single finding
    triage_dict = await app.call(
        f"{NODE_ID}.run_triage_classifier",
        findings=[finding],
        codebase_map=codebase_map or {},
        artifacts_dir=state.artifacts_dir,
        model=resolved.get("triage_classifier_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("triage_classifier"),
    )

    # Determine tier from triage
    from forge.schemas import RemediationItem, RemediationTier, TriageResult
    tier = RemediationTier.TIER_2  # default
    if isinstance(triage_dict, dict):
        triage = TriageResult(**triage_dict)
        if triage.decisions:
            tier = triage.decisions[0].tier

    # Build a minimal remediation plan
    item = RemediationItem(
        finding_id=audit_finding.id,
        title=audit_finding.title,
        tier=tier,
        priority=1,
    )
    plan = RemediationPlan(
        items=[item],
        execution_levels=[[audit_finding.id]],
        total_items=1,
    )
    state.remediation_plan = plan

    # Run through remediation (tier router + control loops)
    from forge.execution.tier_router import route_plan_items
    from forge.execution.forge_executor import execute_remediation

    handled, ai_items = route_plan_items(plan, [audit_finding], state, rp, cfg)

    if ai_items:
        ai_plan = RemediationPlan(
            items=ai_items,
            execution_levels=[[ai_items[0].finding_id]],
            total_items=len(ai_items),
        )
        state.remediation_plan = ai_plan
        await execute_remediation(app, NODE_ID, state, cfg, resolved)

    # Build result
    fix_result = state.completed_fixes[0] if state.completed_fixes else None
    return {
        "success": bool(fix_result and fix_result.outcome.value in ("completed", "skipped")),
        "finding_id": audit_finding.id,
        "tier": tier.value,
        "outcome": fix_result.outcome.value if fix_result else "no_fix",
        "summary": fix_result.summary if fix_result else "No fix produced",
        "files_changed": fix_result.files_changed if fix_result else [],
    }
