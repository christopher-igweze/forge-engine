"""AgentField app for the FORGE remediation engine.

Exposes:
  - ``remediate``: full scan → triage → fix → validate pipeline
  - ``discover``:  scan-only mode (Agents 1-5)
  - ``scan``:      alias for discover (free tier)
  - ``fix_single``: fix a single finding (useful for testing)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

try:
    from agentfield import Agent
except ImportError:
    Agent = None  # Standalone mode — app.py is not used directly

from forge.config import ForgeConfig
from forge.execution.json_utils import safe_parse_agent_response
from forge.reasoners import router
from forge.schemas import (
    AuditFinding,
    ForgeExecutionState,
    ForgeMode,
    ForgeResult,
    IntegrationValidationResult,
    ProductionReadinessReport,
    RemediationPlan,
)

NODE_ID = os.getenv("FORGE_NODE_ID", "forge-engine")
WORKSPACES_DIR = os.getenv("WORKSPACES_DIR", "/workspaces")

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


# ── Helpers ───────────────────────────────────────────────────────────


def _resolve_repo_path(repo_url: str, repo_path: str) -> str:
    """Determine the repo path — clone if needed, else use provided."""
    if repo_path and Path(repo_path).is_dir():
        return repo_path

    if repo_url:
        # Derive workspace path from URL
        import re
        match = re.search(r"/([^/]+?)(?:\.git)?$", repo_url.rstrip("/"))
        name = match.group(1) if match else "repo"
        workspace = os.path.join(WORKSPACES_DIR, name)

        if Path(workspace).is_dir():
            logger.info("Reusing existing workspace: %s", workspace)
            return workspace

        # Clone
        logger.info("Cloning %s → %s", repo_url, workspace)
        os.makedirs(workspace, exist_ok=True)
        import subprocess
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, workspace],
            check=True, capture_output=True, text=True,
        )
        return workspace

    raise ValueError("Either repo_url or repo_path must be provided")


# ── Main Reasoners ────────────────────────────────────────────────────


@_reasoner_decorator()
async def remediate(
    repo_url: str = "",
    repo_path: str = "",
    config: dict | None = None,
    tier1_findings: list[dict] | None = None,
) -> dict:
    """Full FORGE pipeline: discover → triage → fix → validate.

    This is the primary entry point for FORGE remediation runs.
    """
    start_time = time.time()
    cfg = ForgeConfig(**(config or {}))
    resolved = cfg.resolved_models()

    state = ForgeExecutionState(
        mode=cfg.mode,
        repo_url=repo_url,
    )

    # Initialize telemetry if learning is enabled
    telemetry = None
    if cfg.enable_learning:
        from forge.execution.telemetry import ForgeTelemetry
        telemetry = ForgeTelemetry(run_id=state.forge_run_id)

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

        # ── Step 3: Remediation (Agents 7-10) ────────────────────────
        if cfg.mode in (ForgeMode.FULL, ForgeMode.REMEDIATION) and not cfg.dry_run:
            if resume_phase not in (
                CheckpointPhase.REMEDIATION, CheckpointPhase.VALIDATION,
            ):
                remediation_result = await _run_remediation(
                    app, state, cfg, resolved,
                )
                state.total_agent_invocations += remediation_result["invocations"]
                save_checkpoint(state.repo_path, CheckpointPhase.REMEDIATION, state)

        # ── Step 4: Validation (Agents 11-12) ────────────────────────
        if cfg.mode in (ForgeMode.FULL, ForgeMode.VALIDATION) and not cfg.dry_run:
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

    # Finalize
    elapsed = time.time() - start_time

    # Flush telemetry: training data pairs + cost summary
    if telemetry is not None:
        telemetry.artifacts_dir = state.artifacts_dir
        # Log training data for each completed fix
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
    state.finished_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )

    result = ForgeResult(
        forge_run_id=state.forge_run_id,
        success=state.success,
        mode=state.mode,
        summary=_build_summary(state),
        total_findings=len(state.all_findings),
        findings_fixed=len(state.completed_fixes),
        findings_deferred=len(state.outer_loop.deferred_findings),
        agent_invocations=state.total_agent_invocations,
        cost_usd=state.estimated_cost_usd,
        duration_seconds=elapsed,
        readiness_report=state.readiness_report,
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
    the triage → coder → reviewer pipeline.
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
        from forge.schemas import CodebaseMap
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


# ── Internal Pipeline Stages ─────────────────────────────────────────


async def _run_discovery(
    app: Agent,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Discovery phase: Agents 1-4."""
    invocations = 0

    # Agent 1: Codebase Analyst (always runs first — everything depends on it)
    logger.info("Discovery: Running Agent 1 (Codebase Analyst)")
    codebase_map_dict = await app.call(
        f"{NODE_ID}.run_codebase_analyst",
        repo_path=state.repo_path,
        repo_url=state.repo_url,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("codebase_analyst_model", "minimax/minimax-m2.5"),
        ai_provider=cfg.provider_for_role("codebase_analyst"),
    )
    state.codebase_map = _unwrap_to_model(codebase_map_dict)
    invocations += 1

    # Agents 2, 3, 4: Run in parallel (all depend only on CodebaseMap)
    logger.info("Discovery: Running Agents 2-4 in parallel")

    coros = []
    # Agent 2: Security Auditor
    coros.append(app.call(
        f"{NODE_ID}.run_security_auditor",
        repo_path=state.repo_path,
        codebase_map=codebase_map_dict if isinstance(codebase_map_dict, dict)
        else codebase_map_dict,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("security_auditor_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("security_auditor"),
        parallel=cfg.enable_parallel_audit,
    ))

    # Agent 3: Quality Auditor
    coros.append(app.call(
        f"{NODE_ID}.run_quality_auditor",
        repo_path=state.repo_path,
        codebase_map=codebase_map_dict if isinstance(codebase_map_dict, dict)
        else codebase_map_dict,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("quality_auditor_model", "minimax/minimax-m2.5"),
        ai_provider=cfg.provider_for_role("quality_auditor"),
    ))

    # Agent 4: Architecture Reviewer
    coros.append(app.call(
        f"{NODE_ID}.run_architecture_reviewer",
        repo_path=state.repo_path,
        codebase_map=codebase_map_dict if isinstance(codebase_map_dict, dict)
        else codebase_map_dict,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("architecture_reviewer_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("architecture_reviewer"),
    ))

    results = await asyncio.gather(*coros, return_exceptions=True)
    invocations += 3  # 3 agents called (security has 3 sub-passes but is 1 agent)

    # Parse results
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("Discovery agent %d failed: %s", i + 2, r)
            continue

        result_dict = r if isinstance(r, dict) else {}

        if i == 0:  # Security
            state.security_findings = [
                AuditFinding(**f) for f in result_dict.get("findings", [])
            ]
        elif i == 1:  # Quality
            state.quality_findings = [
                AuditFinding(**f) for f in result_dict.get("findings", [])
            ]
        elif i == 2:  # Architecture
            state.architecture_findings = [
                AuditFinding(**f) for f in result_dict.get("findings", [])
            ]

    # Merge all findings
    state.all_findings = (
        state.security_findings +
        state.quality_findings +
        state.architecture_findings
    )

    logger.info(
        "Discovery complete: %d security, %d quality, %d architecture findings",
        len(state.security_findings),
        len(state.quality_findings),
        len(state.architecture_findings),
    )

    return {"invocations": invocations}


async def _run_triage(
    app: Agent,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    tier1_findings: list[dict] | None = None,
) -> dict:
    """Run Triage phase: Agents 5-6."""
    invocations = 0

    if not state.all_findings:
        logger.info("Triage: No findings to triage")
        return {"invocations": 0}

    all_findings_dicts = [f.model_dump() for f in state.all_findings]

    # Merge Tier 1 findings if provided
    if tier1_findings:
        for t1f in tier1_findings:
            # Convert Tier1Finding format to AuditFinding format
            all_findings_dicts.append({
                "id": t1f.get("check_id", t1f.get("id", "")),
                "title": t1f.get("title", ""),
                "description": t1f.get("description", ""),
                "category": t1f.get("category", "quality"),
                "severity": t1f.get("severity", "medium"),
                "locations": [{"file_path": loc} for loc in t1f.get("locations", [])],
                "suggested_fix": t1f.get("suggested_fix", ""),
                "agent": "tier1_scanner",
            })

    codebase_map_dict = state.codebase_map.model_dump() if state.codebase_map else {}

    # Agent 6: Triage Classifier
    logger.info("Triage: Running Agent 6 (Triage Classifier)")
    triage_dict = await app.call(
        f"{NODE_ID}.run_triage_classifier",
        findings=all_findings_dicts,
        codebase_map=codebase_map_dict,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("triage_classifier_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("triage_classifier"),
    )
    if isinstance(triage_dict, dict):
        from forge.schemas import TriageResult
        state.triage_result = TriageResult(**triage_dict)
    invocations += 1

    # Agent 5: Fix Strategist
    logger.info("Triage: Running Agent 5 (Fix Strategist)")
    plan_dict = await app.call(
        f"{NODE_ID}.run_fix_strategist",
        all_findings=all_findings_dicts,
        codebase_map=codebase_map_dict,
        triage_result=triage_dict if isinstance(triage_dict, dict) else None,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("fix_strategist_model", "anthropic/claude-haiku-4.5"),
        ai_provider=cfg.provider_for_role("fix_strategist"),
    )
    if isinstance(plan_dict, dict):
        from forge.schemas import RemediationPlan
        state.remediation_plan = RemediationPlan(**plan_dict)
    invocations += 1

    logger.info(
        "Triage complete: %d items in remediation plan",
        state.remediation_plan.total_items if state.remediation_plan else 0,
    )

    return {"invocations": invocations}


async def _run_remediation(
    app: Agent,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Remediation phase: Tier routing + Agents 7-10 via control loops."""
    from forge.execution.tier_router import route_plan_items
    from forge.execution.forge_executor import execute_remediation

    invocations = 0

    if not state.remediation_plan or not state.remediation_plan.items:
        logger.info("Remediation: no plan items to execute")
        return {"invocations": 0}

    # ── Step 3a: Tier 0/1 — deterministic fixes ──────────────────────
    logger.info("Remediation: routing %d items through tier router", len(state.remediation_plan.items))
    handled, ai_items = route_plan_items(
        state.remediation_plan,
        state.all_findings,
        state,
        state.repo_path,
        cfg,
    )
    invocations += len(handled)  # Tier 0/1 count as 1 invocation each

    if not ai_items:
        logger.info("Remediation: all items handled by Tier 0/1 — skipping AI pipeline")
        return {"invocations": invocations}

    # ── Step 3b: Tier 2/3 — AI-assisted fixes via control loops ──────
    # Build a filtered plan with only AI items
    ai_plan = RemediationPlan(
        items=ai_items,
        execution_levels=_filter_execution_levels(
            state.remediation_plan.execution_levels,
            {item.finding_id for item in ai_items},
        ),
        total_items=len(ai_items),
    )
    state.remediation_plan = ai_plan

    logger.info(
        "Remediation: executing %d AI items across %d levels",
        len(ai_items), len(ai_plan.execution_levels),
    )

    await execute_remediation(app, NODE_ID, state, cfg, resolved_models)
    invocations += state.total_agent_invocations  # executor tracks its own

    logger.info(
        "Remediation complete: %d fixed, %d deferred",
        len(state.completed_fixes), len(state.outer_loop.deferred_findings),
    )

    return {"invocations": invocations}


async def _run_validation(
    app: Agent,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Validation phase: Agents 11-12."""
    invocations = 0

    if not state.completed_fixes:
        logger.info("Validation: no fixes to validate")
        return {"invocations": 0}

    all_findings_json = [f.model_dump() for f in state.all_findings]
    all_fixes_json = [f.model_dump() for f in state.completed_fixes]
    deferred_items = [
        {"finding_id": fid}
        for fid in state.outer_loop.deferred_findings
    ]

    # Agent 11: Integration Validator
    logger.info("Validation: Running Agent 11 (Integration Validator)")
    try:
        validation_dict = await app.call(
            f"{NODE_ID}.run_integration_validator",
            repo_path=state.repo_path,
            all_findings=all_findings_json,
            all_fixes=all_fixes_json,
            artifacts_dir=state.artifacts_dir,
            model=resolved_models.get("integration_validator_model", "anthropic/claude-haiku-4.5"),
            ai_provider=cfg.provider_for_role("integration_validator"),
        )
        if isinstance(validation_dict, dict):
            state.integration_result = IntegrationValidationResult(
                **_unwrap_to_model(validation_dict)
                if isinstance(_unwrap_to_model(validation_dict), dict)
                else {}
            )
        invocations += 1
    except Exception as e:
        logger.error("Integration validator failed: %s", e)
        state.integration_result = IntegrationValidationResult(
            passed=False, summary=f"Validator failed: {e}",
        )

    # Agent 12: Debt Tracker & Report Generator
    logger.info("Validation: Running Agent 12 (Debt Tracker)")
    try:
        report_dict = await app.call(
            f"{NODE_ID}.run_debt_tracker",
            all_findings=all_findings_json,
            completed_fixes=all_fixes_json,
            deferred_items=deferred_items,
            validation_result=state.integration_result.model_dump()
            if state.integration_result else {},
            artifacts_dir=state.artifacts_dir,
            model=resolved_models.get("debt_tracker_model", "minimax/minimax-m2.5"),
            ai_provider=cfg.provider_for_role("debt_tracker"),
        )
        if isinstance(report_dict, dict):
            unwrapped = _unwrap_to_model(report_dict)
            if isinstance(unwrapped, dict):
                state.readiness_report = ProductionReadinessReport(**unwrapped)
        invocations += 1
    except Exception as e:
        logger.error("Debt tracker failed: %s", e)

    # Generate formatted reports (JSON + HTML + PDF if weasyprint available)
    if state.readiness_report:
        try:
            from forge.execution.report import generate_reports
            report_paths = generate_reports(
                state.readiness_report,
                state.artifacts_dir,
                run_id=state.forge_run_id,
            )
            logger.info("Reports generated: %s", ", ".join(report_paths.keys()))
        except Exception as e:
            logger.error("Report generation failed: %s", e)

    logger.info(
        "Validation complete: integration=%s, readiness_score=%d",
        "PASS" if state.integration_result and state.integration_result.passed else "FAIL",
        state.readiness_report.overall_score if state.readiness_report else 0,
    )

    return {"invocations": invocations}


def _filter_execution_levels(
    levels: list[list[str]],
    keep_ids: set[str],
) -> list[list[str]]:
    """Filter execution levels to only include specified finding IDs."""
    filtered = []
    for level in levels:
        kept = [fid for fid in level if fid in keep_ids]
        if kept:
            filtered.append(kept)
    return filtered


# ── Utilities ─────────────────────────────────────────────────────────


def _unwrap_to_model(result):
    """Handle AgentField envelope unwrapping with resilient JSON parsing."""
    return safe_parse_agent_response(result, fallback=result)


def _build_summary(state: ForgeExecutionState) -> str:
    """Build a human-readable summary of the FORGE run."""
    parts = [f"FORGE run {state.forge_run_id}"]

    if state.all_findings:
        parts.append(f"Found {len(state.all_findings)} issues")
        by_sev = {}
        for f in state.all_findings:
            by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
        sev_str = ", ".join(f"{v} {k}" for k, v in sorted(by_sev.items()))
        parts.append(f"({sev_str})")

    if state.remediation_plan:
        parts.append(
            f"Remediation plan: {state.remediation_plan.total_items} items "
            f"across {len(state.remediation_plan.execution_levels)} levels"
        )

    if state.completed_fixes:
        parts.append(f"Fixed: {len(state.completed_fixes)}")

    if state.outer_loop.deferred_findings:
        parts.append(f"Deferred: {len(state.outer_loop.deferred_findings)}")

    return ". ".join(parts) + "."
