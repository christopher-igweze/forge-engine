"""Standalone execution mode — runs FORGE without AgentField.

In standalone mode, reasoner functions are called directly instead of
being dispatched through AgentField's ``app.call()`` protocol.  This
allows users to ``pip install vibe2prod`` and run FORGE locally:

    vibe2prod scan ./my-app

Code never leaves the user's machine — only LLM API calls go to
OpenRouter via the user's own API key.

The ``StandaloneDispatcher`` implements the same ``.call()`` interface
as ``agentfield.Agent``, so the existing pipeline functions
(``_run_discovery``, ``_run_triage``, ``forge_executor``, etc.) work
unchanged with either the real AgentField app or this dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from forge.config import ForgeConfig
from forge.execution.json_utils import safe_parse_agent_response
from forge.schemas import (
    AuditFinding,
    FixOutcome,
    ForgeExecutionState,
    ForgeMode,
    ForgeResult,
    IntegrationValidationResult,
    ProductionReadinessReport,
    RemediationPlan,
)

logger = logging.getLogger(__name__)


class StandaloneDispatcher:
    """Drop-in replacement for ``agentfield.Agent`` in standalone mode.

    Resolves ``app.call("forge-engine.run_codebase_analyst", ...)`` to a
    direct call to ``run_codebase_analyst(**kwargs)`` by looking up the
    function in the reasoners registry.
    """

    def __init__(self, node_id: str = "forge-engine") -> None:
        self.node_id = node_id
        self._registry: dict[str, callable] = {}
        self._register_all()

    def _register_all(self) -> None:
        """Import and register all reasoner functions."""
        from forge.reasoners.discovery import (
            run_codebase_analyst,
            run_security_auditor,
            run_quality_auditor,
            run_architecture_reviewer,
        )
        from forge.reasoners.triage import (
            run_triage_classifier,
            run_fix_strategist,
        )
        from forge.reasoners.remediation import (
            run_coder_tier2,
            run_coder_tier3,
            run_test_generator,
            run_code_reviewer,
            run_escalation_agent,
        )
        from forge.reasoners.validation import (
            run_integration_validator,
            run_debt_tracker,
        )
        from forge.reasoners.hive_discovery import run_hive_discovery

        for fn in (
            run_codebase_analyst,
            run_security_auditor,
            run_quality_auditor,
            run_architecture_reviewer,
            run_triage_classifier,
            run_fix_strategist,
            run_coder_tier2,
            run_coder_tier3,
            run_test_generator,
            run_code_reviewer,
            run_escalation_agent,
            run_integration_validator,
            run_debt_tracker,
            run_hive_discovery,
        ):
            self._registry[fn.__name__] = fn

    async def call(self, target: str, **kwargs) -> dict:
        """Dispatch a call to a reasoner function directly.

        Args:
            target: AgentField-style target, e.g. "forge-engine.run_codebase_analyst"
            **kwargs: Arguments forwarded to the reasoner function.

        Returns:
            The reasoner's return value (dict).
        """
        # Strip node_id prefix: "forge-engine.run_X" → "run_X"
        parts = target.rsplit(".", 1)
        fn_name = parts[-1] if len(parts) > 1 else target

        fn = self._registry.get(fn_name)
        if fn is None:
            raise ValueError(
                f"Unknown reasoner '{fn_name}'. Available: {sorted(self._registry)}"
            )

        logger.debug("Standalone dispatch: %s", fn_name)
        return await fn(**kwargs)


# ── Public API ───────────────────────────────────────────────────────


def _resolve_repo_path(repo_url: str, repo_path: str) -> str:
    """Determine the repo path — clone if needed, else use provided."""
    if repo_path and Path(repo_path).is_dir():
        return repo_path

    if repo_url:
        import re
        import subprocess

        match = re.search(r"/([^/]+?)(?:\.git)?$", repo_url.rstrip("/"))
        name = match.group(1) if match else "repo"
        workspaces = os.getenv("WORKSPACES_DIR", "/tmp/vibe2prod-workspaces")
        workspace = os.path.join(workspaces, name)

        if Path(workspace).is_dir():
            logger.info("Reusing existing workspace: %s", workspace)
            return workspace

        logger.info("Cloning %s → %s", repo_url, workspace)
        os.makedirs(workspace, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", repo_url, workspace],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise ValueError(
                "git is not installed or not found in PATH. "
                "Install git and retry, or provide a local --path instead."
            )
        except subprocess.CalledProcessError as e:
            raise ValueError(
                f"Failed to clone {repo_url}: "
                f"{e.stderr.strip() or e.stdout.strip() or str(e)}"
            )
        return workspace

    raise ValueError("Either repo_url or repo_path must be provided")


async def run_standalone(
    repo_url: str = "",
    repo_path: str = "",
    config: dict | None = None,
    tier1_findings: list[dict] | None = None,
) -> ForgeResult:
    """Run the full FORGE pipeline without AgentField.

    This is the standalone equivalent of the ``remediate`` reasoner
    in ``forge/app.py``.  It uses ``StandaloneDispatcher`` instead of
    the AgentField ``app`` for agent dispatch.
    """
    # Import pipeline functions from app.py — they accept any object with .call()
    from forge.app import (
        _run_discovery,
        _run_triage,
        _run_remediation,
        _run_validation,
        _build_summary,
    )

    from forge.execution.events import (
        emit_phase_start,
        emit_scan_complete,
        emit_scan_error,
    )

    start_time = time.time()
    cfg = ForgeConfig(**(config or {}))

    # Env-var fallback for webhook config (so callers don't need to pass it)
    if not cfg.webhook_url:
        cfg.webhook_url = os.environ.get("FORGE_WEBHOOK_URL", "")
    if not cfg.webhook_token:
        cfg.webhook_token = os.environ.get("FORGE_WEBHOOK_TOKEN", "")
    if not cfg.webhook_scan_id:
        cfg.webhook_scan_id = os.environ.get("FORGE_WEBHOOK_SCAN_ID", "")

    resolved = cfg.resolved_models()
    dispatcher = StandaloneDispatcher()

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
        _telemetry_ctx = None  # don't push a new context
    else:
        telemetry = ForgeTelemetry(run_id=state.forge_run_id)
        _telemetry_ctx = telemetry.activate()
        _telemetry_ctx.__enter__()

    # Initialize RunTelemetry (real-time observable state + circuit breakers).
    # Activate the contextvar BEFORE any LLM calls so AgentAI.run() can
    # record costs from the very first invocation (including discovery).
    from forge.execution.run_telemetry import (
        RunTelemetry,
        CostLimitExceeded,
        TimeLimitExceeded,
        _current_run_telemetry,
    )
    # Use a temp dir initially; we'll update once repo_path is resolved.
    import tempfile
    _tmp_telemetry_dir = tempfile.mkdtemp(prefix="forge-telemetry-")
    run_telemetry = RunTelemetry(
        artifacts_dir=_tmp_telemetry_dir,
        max_cost_usd=cfg.max_cost_usd,
        max_duration_seconds=cfg.max_duration_seconds,
    )
    _rt_token = _current_run_telemetry.set(run_telemetry)

    try:
        state.repo_path = _resolve_repo_path(repo_url, repo_path or cfg.repo_path)
        state.artifacts_dir = os.path.join(state.repo_path, ".artifacts")
        os.makedirs(state.artifacts_dir, exist_ok=True)

        # Point RunTelemetry at the real artifacts directory now that we know it
        run_telemetry._dir = Path(state.artifacts_dir) / "telemetry"
        run_telemetry._dir.mkdir(parents=True, exist_ok=True)
        run_telemetry._flush()

        logger.info("FORGE standalone starting: %s", state.repo_path)
        emit_phase_start(cfg, "orchestrator", "Starting FORGE discovery scan.")

        # Recover stale worktrees
        try:
            from forge.execution.worktree import recover_worktrees
            recovered = recover_worktrees(state.repo_path)
            if recovered:
                logger.info("Recovered %d stale worktrees", len(recovered))
        except Exception as e:
            logger.warning("Worktree recovery failed (non-fatal): %s", e)

        # Check for resumable checkpoint
        from forge.execution.checkpoint import (
            CheckpointPhase, save_checkpoint, get_latest_checkpoint,
            restore_state, clear_checkpoints,
        )
        cp = get_latest_checkpoint(state.repo_path)
        resume_phase = None
        if cp and cp.forge_run_id:
            logger.info("Resuming from checkpoint: %s (phase: %s)", cp.forge_run_id, cp.phase.value)
            restored = restore_state(cp)
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

        # Discovery
        if resume_phase not in (
            CheckpointPhase.DISCOVERY, CheckpointPhase.TRIAGE,
            CheckpointPhase.REMEDIATION, CheckpointPhase.VALIDATION,
        ):
            result = await _run_discovery(dispatcher, state, cfg, resolved)
            state.total_agent_invocations += result["invocations"]
            # Capture v2 metadata from discovery for ForgeResult
            state._v2_findings_delta = result.get("findings_delta")
            state._v2_quality_gate = result.get("quality_gate")
            save_checkpoint(state.repo_path, CheckpointPhase.DISCOVERY, state)

        # Triage
        if resume_phase not in (
            CheckpointPhase.TRIAGE, CheckpointPhase.REMEDIATION,
            CheckpointPhase.VALIDATION,
        ):
            result = await _run_triage(dispatcher, state, cfg, resolved, tier1_findings)
            state.total_agent_invocations += result["invocations"]
            save_checkpoint(state.repo_path, CheckpointPhase.TRIAGE, state)

        # Remediation + Validation (convergence loop or single-pass)
        if cfg.mode in (ForgeMode.FULL, ForgeMode.REMEDIATION) and not cfg.dry_run:
            if resume_phase not in (
                CheckpointPhase.REMEDIATION, CheckpointPhase.VALIDATION,
            ):
                if cfg.convergence_enabled:
                    from forge.execution.convergence import run_convergence_loop
                    conv_result = await run_convergence_loop(
                        dispatcher, state, cfg, resolved, tier1_findings,
                    )
                    logger.info(
                        "Convergence: %s after %d iterations (score=%d)",
                        "converged" if conv_result.converged else "stopped",
                        conv_result.iterations_run, conv_result.final_score,
                    )
                else:
                    result = await _run_remediation(dispatcher, state, cfg, resolved)
                    state.total_agent_invocations += result["invocations"]
                    save_checkpoint(state.repo_path, CheckpointPhase.REMEDIATION, state)

        # Validation (only if convergence is disabled — convergence loop handles its own)
        if cfg.mode in (ForgeMode.FULL, ForgeMode.VALIDATION) and not cfg.dry_run:
            if not cfg.convergence_enabled:
                if resume_phase != CheckpointPhase.VALIDATION:
                    result = await _run_validation(dispatcher, state, cfg, resolved)
                    state.total_agent_invocations += result["invocations"]
                    save_checkpoint(state.repo_path, CheckpointPhase.VALIDATION, state)

        clear_checkpoints(state.repo_path)
        state.success = True
        emit_scan_complete(
            cfg,
            f"FORGE scan complete. {len(state.all_findings)} findings.",
            data={"total_findings": len(state.all_findings)},
        )

    except (CostLimitExceeded, TimeLimitExceeded) as e:
        logger.warning("FORGE run stopped by circuit breaker: %s", e)
        state.success = False
        emit_scan_error(cfg, f"FORGE run stopped: {e}")
    except Exception as e:
        logger.exception("FORGE standalone failed: %s", e)
        state.success = False
        emit_scan_error(cfg, f"FORGE scan failed: {e}")
    finally:
        try:
            from forge.execution.worktree import cleanup_all_worktrees
            cleanup_all_worktrees(state.repo_path)
        except Exception as e:
            logger.warning("Worktree cleanup failed: %s", e)
        # Deactivate telemetry context (only if we created it)
        if _telemetry_ctx is not None:
            _telemetry_ctx.__exit__(None, None, None)
        # Deactivate RunTelemetry contextvar
        if _rt_token is not None:
            _current_run_telemetry.reset(_rt_token)
        # Clean up temp telemetry dir if we never resolved a real one
        import shutil
        if _tmp_telemetry_dir and not state.artifacts_dir:
            shutil.rmtree(_tmp_telemetry_dir, ignore_errors=True)

    elapsed = time.time() - start_time

    # Flush telemetry: invocations are already auto-logged by AgentAI.run()
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

    # Generate discovery report (findings + remediation plan) after telemetry
    # flush so cost_usd is populated. Runs for all modes that produce findings.
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

    # Run pattern extraction pipeline (learning loop)
    if state.all_findings and state.artifacts_dir:
        try:
            from forge.patterns.extractor import (
                append_findings_history,
                update_pattern_prevalence,
            )
            from forge.patterns.loader import PatternLibrary

            library = PatternLibrary.load_default()
            append_findings_history(state.all_findings, state.artifacts_dir)
            prevalence = update_pattern_prevalence(state.all_findings, library)
            if prevalence:
                logger.info("Pattern prevalence: %s", prevalence)
        except Exception as e:
            logger.warning("Pattern extraction failed (non-fatal): %s", e, exc_info=True)

    state.finished_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )

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
        findings_delta=getattr(state, "_v2_findings_delta", None),
        quality_gate=getattr(state, "_v2_quality_gate", None),
        estimated_readiness_score=(
            getattr(state, "_v2_findings_delta", {}) or {}
        ).get("readiness", {}).get("overall_score") if getattr(state, "_v2_findings_delta", None) else None,
    )

    logger.info(
        "FORGE standalone complete: %s — %d findings, %d fixed, %.1fs",
        "SUCCESS" if result.success else "FAILED",
        result.total_findings, result.findings_fixed, elapsed,
    )
    return result
