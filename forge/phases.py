"""FORGE pipeline phase orchestrators.

Each function accepts a duck-typed *dispatcher* (anything with a ``.call()``
method) so it works with both the AgentField ``app`` and the standalone
``StandaloneDispatcher``.

Extracted from ``forge/app.py`` for clarity.  All names are re-exported
from ``forge.app`` so existing ``from forge.app import ...`` still works.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from forge.app_helpers import NODE_ID, _filter_execution_levels, _unwrap_to_model
from forge.execution.events import emit_phase_complete, emit_phase_start
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    FixOutcome,
    ForgeExecutionState,
    IntegrationValidationResult,
    ProductionReadinessReport,
    RemediationPlan,
    TriageResult,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)


def _get_run_telemetry():
    """Get the active RunTelemetry instance (if any)."""
    try:
        from forge.execution.run_telemetry import _current_run_telemetry
        return _current_run_telemetry.get(None)
    except Exception:
        return None


# ── Internal Pipeline Stages ─────────────────────────────────────────


async def _run_discovery(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Discovery phase: Agents 1-4 (classic) or Hive Discovery (swarm)."""
    rt = _get_run_telemetry()
    if rt:
        rt.set_phase("discovery")

    # ── Swarm mode: delegate to Hive Discovery ───────────────────────
    if cfg.discovery_mode == "swarm":
        return await _run_swarm_discovery(app, state, cfg, resolved_models)

    # ── Classic mode: sequential Agent 1 → parallel Agents 2-4 ───────
    invocations = 0

    # ── Delta mode: compute changed files since last scan ────────────
    delta_files: list[str] | None = None
    if cfg.delta_mode:
        try:
            from pathlib import Path as _DeltaPath
            from forge.execution.delta import get_changed_files, load_last_head_sha

            _artifacts = state.artifacts_dir or str(
                _DeltaPath(state.repo_path) / ".artifacts"
            )
            last_sha = load_last_head_sha(_artifacts)
            delta_files = get_changed_files(state.repo_path, last_sha)
            if delta_files is not None:
                logger.info(
                    "Delta mode active: %d changed files since %s",
                    len(delta_files),
                    last_sha[:8] if last_sha else "unknown",
                )
            else:
                logger.info("Delta mode: no previous SHA found, running full scan")
        except Exception as e:
            logger.warning("Delta mode computation failed (falling back to full scan): %s", e)

    # Agent 1: Codebase Analyst (always runs first — everything depends on it)
    emit_phase_start(cfg, "discovery", "Running Agent 1 (Codebase Analyst).")
    logger.info("Discovery: Running Agent 1 (Codebase Analyst)")
    codebase_map_dict = await app.call(
        f"{NODE_ID}.run_codebase_analyst",
        repo_path=state.repo_path,
        repo_url=state.repo_url,
        artifacts_dir=state.artifacts_dir,
        model=resolved_models.get("codebase_analyst_model", "minimax/minimax-m2.5"),
        ai_provider=cfg.provider_for_role("codebase_analyst"),
    )
    unwrapped = _unwrap_to_model(codebase_map_dict)
    if isinstance(unwrapped, dict):
        try:
            state.codebase_map = CodebaseMap(**unwrapped)
        except Exception:
            state.codebase_map = unwrapped  # fallback to raw dict
    else:
        state.codebase_map = unwrapped
    invocations += 1
    emit_phase_complete(cfg, "codebase_analyst", "Agent 1 (Codebase Analyst) complete.")

    # Agents 2, 3, 4: Run in parallel (all depend only on CodebaseMap)
    emit_phase_start(cfg, "discovery", "Running Agents 2-4 in parallel (Security, Quality, Architecture).")
    logger.info("Discovery: Running Agents 2-4 in parallel")

    coros = []
    # Build project context string for prompt injection (zero LLM cost)
    project_context_str = ""
    if cfg.project_context:
        try:
            from forge.prompts.project_context import build_project_context_string
            project_context_str = build_project_context_string(cfg.project_context)
        except Exception as e:
            logger.warning("Failed to build project context: %s", e)

    # Auto-detect project conventions (deterministic, zero LLM)
    try:
        from forge.conventions import (
            ConventionsExtractor,
            build_conventions_context_string,
        )
        conventions = ConventionsExtractor(state.repo_path).extract()
        conventions_str = build_conventions_context_string(conventions)
        if conventions_str:
            project_context_str = (
                f"{project_context_str}\n\n{conventions_str}"
                if project_context_str
                else conventions_str
            )
            logger.info(
                "Discovery: Auto-detected conventions from %d config files",
                len(conventions.config_files_found),
            )
    except Exception as e:
        logger.warning("Convention extraction failed (non-fatal): %s", e)

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
        pattern_library_path=cfg.pattern_library_path,
        project_context=project_context_str,
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
        project_context=project_context_str,
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
        project_context=project_context_str,
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
    if rt:
        rt.update_findings_progress(total=len(state.all_findings))
    emit_phase_complete(
        cfg, "discovery",
        f"Agents 2-4 complete. {len(state.all_findings)} total findings.",
    )

    # Intent analysis (LLM-based reasoning about developer intent)
    if state.all_findings:
        try:
            from forge.execution.intent_analyzer import analyze_intent
            _conventions = conventions if "conventions" in dir() else None
            intent_result = await analyze_intent(
                findings=state.all_findings,
                repo_path=state.repo_path,
                conventions=_conventions,
                model=cfg.model_for_role("intent_analyzer"),
                ai_provider=cfg.provider_for_role("intent_analyzer"),
            )
            if intent_result.decisions:
                invocations += 1
                logger.info(
                    "Intent analysis: %d intentional, %d ambiguous, %d unintentional "
                    "(%d deterministic)",
                    intent_result.intentional_count,
                    intent_result.ambiguous_count,
                    intent_result.unintentional_count,
                    intent_result.deterministic_count,
                )
        except Exception as e:
            logger.warning("Intent analysis failed (non-fatal): %s", e)

    # Apply actionability classification (deterministic post-processing)
    if state.all_findings:
        try:
            from forge.execution.actionability import apply_actionability
            apply_actionability(state.all_findings, cfg.project_context)
        except Exception as e:
            logger.warning("Actionability classification failed (non-fatal): %s", e)

    emit_phase_complete(cfg, "intent_analyzer", "Intent analysis complete.")

    # ── Fingerprint, Baseline, Suppression, Severity ─────────────────
    findings_delta: dict = {}
    quality_gate_result: dict = {}
    suppressed_count = 0
    if state.all_findings:
        try:
            from pathlib import Path as _Path

            from forge.execution.baseline import Baseline
            from forge.execution.fingerprint import fingerprint
            from forge.execution.forgeignore import ForgeIgnore
            from forge.execution.severity import calibrate_findings

            # Convert AuditFinding models to dicts for processing
            findings_dicts = [f.model_dump(mode="json") for f in state.all_findings]

            # Generate fingerprints
            for fd in findings_dicts:
                fd["fingerprint"] = fingerprint(fd)

            # Apply severity calibration
            calibrate_findings(findings_dicts)

            # Load .forgeignore and filter suppressed findings
            forgeignore = ForgeIgnore.load(state.repo_path)
            kept_dicts, suppressed_dicts = forgeignore.apply(findings_dicts)
            suppressed_count = len(suppressed_dicts)

            # Load baseline and compare
            artifacts_dir = state.artifacts_dir or str(
                _Path(state.repo_path) / ".artifacts"
            )
            baseline = Baseline.load(artifacts_dir)
            comparison = baseline.update_from_scan(
                state.forge_run_id, kept_dicts,
            )

            # Track HEAD SHA for delta mode
            try:
                from forge.execution.delta import get_head_sha, save_head_sha
                head_sha = get_head_sha(state.repo_path)
                if head_sha:
                    save_head_sha(artifacts_dir, head_sha)
            except Exception as e:
                logger.debug("Could not record HEAD SHA: %s", e)

            baseline.save(artifacts_dir)

            # Build delta metadata
            findings_delta = {
                "new": len(comparison.new_findings),
                "recurring": len(comparison.recurring_findings),
                "fixed": len(comparison.fixed_findings),
                "suppressed": suppressed_count,
                "regressed": len(comparison.regressed_findings),
            }

            # ── Quality Gate evaluation ──────────────────────────────
            try:
                from forge.execution.quality_gate import (
                    QualityGateThreshold,
                    evaluate_gate,
                )
                threshold = QualityGateThreshold(
                    max_new_critical=cfg.quality_gate_max_critical,
                    max_new_high=cfg.quality_gate_max_high,
                    max_new_medium=cfg.quality_gate_max_medium,
                )
                gate_result = evaluate_gate(comparison.new_findings, threshold)
                quality_gate_result = {
                    "passed": gate_result.passed,
                    "reason": gate_result.reason,
                    "new_critical": gate_result.new_critical,
                    "new_high": gate_result.new_high,
                    "new_medium": gate_result.new_medium,
                    "total_new": gate_result.total_new,
                }
                logger.info("Quality gate: %s", gate_result.reason)
            except Exception as e:
                logger.warning("Quality gate evaluation failed (non-fatal): %s", e)

            # Rebuild state.all_findings from kept (non-suppressed) dicts,
            # applying any severity changes back to the AuditFinding objects
            sev_map = {fd.get("fingerprint", ""): fd.get("severity") for fd in kept_dicts}
            orig_sev_map = {fd.get("fingerprint", ""): fd.get("original_severity") for fd in kept_dicts}
            kept_fps = {fd.get("fingerprint", "") for fd in kept_dicts}
            suppressed_fps = {fd.get("fingerprint", "") for fd in suppressed_dicts}

            # Map original findings by their fingerprint for quick lookup
            fp_to_finding: dict[str, AuditFinding] = {}
            for fd, finding in zip(findings_dicts, state.all_findings):
                fp_to_finding[fd.get("fingerprint", "")] = finding

            # Filter suppressed and apply calibrated severity
            kept_findings: list[AuditFinding] = []
            for fp_val in kept_fps:
                finding = fp_to_finding.get(fp_val)
                if finding:
                    new_sev = sev_map.get(fp_val)
                    if new_sev and new_sev != (finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity)):
                        finding.severity = new_sev
                    kept_findings.append(finding)

            state.all_findings = kept_findings
            # Rebuild category lists from filtered findings
            state.security_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "security"]
            state.quality_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "quality"]
            state.architecture_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "architecture"]

            if rt:
                rt.update_findings_progress(total=len(state.all_findings))

            logger.info(
                "Post-processing: %d kept, %d suppressed, delta: %s",
                len(kept_findings), suppressed_count, findings_delta,
            )

            # --- Feedback tracking ---
            try:
                from forge.execution.feedback import FeedbackTracker

                feedback = FeedbackTracker.load(artifacts_dir)
                fp_rates = feedback.update_from_scan(findings_dicts, suppressed_dicts)
                feedback.save(artifacts_dir)
                findings_delta["fp_rates"] = fp_rates
            except Exception as _fb_err:
                logger.warning("Feedback tracking failed (non-fatal): %s", _fb_err)

            # --- Readiness score ---
            try:
                from forge.execution.readiness_score import readiness_breakdown

                readiness = readiness_breakdown(
                    [f.model_dump(mode="json") for f in kept_findings]
                )
                findings_delta["readiness"] = readiness
            except Exception as _rs_err:
                logger.warning("Readiness score failed (non-fatal): %s", _rs_err)

        except Exception as e:
            logger.warning("Fingerprint/baseline/severity processing failed (non-fatal): %s", e)

    logger.info(
        "Discovery complete: %d security, %d quality, %d architecture findings",
        len(state.security_findings),
        len(state.quality_findings),
        len(state.architecture_findings),
    )

    return {
        "invocations": invocations,
        "findings_delta": findings_delta,
        "quality_gate": quality_gate_result,
        "delta_files": delta_files,
    }


async def _run_swarm_discovery(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Discovery via Hive Discovery (swarm mode).

    Replaces Agents 1-6 with Layer 0 (deterministic graph) + Layer 1
    (parallel swarm workers) + Layer 2 (sonnet synthesis).
    """
    logger.info("Discovery [swarm]: Running Hive Discovery pipeline")

    hive_result = await app.call(
        f"{NODE_ID}.run_hive_discovery",
        repo_path=state.repo_path,
        repo_url=state.repo_url,
        artifacts_dir=state.artifacts_dir,
        worker_model=resolved_models.get("swarm_worker_model", "minimax/minimax-m2.5"),
        synthesis_model=resolved_models.get("synthesizer_model", "anthropic/claude-sonnet-4.6"),
        ai_provider=cfg.provider_for_role("swarm_worker"),
        target_segments=cfg.swarm_target_segments,
        enable_wave2=cfg.swarm_enable_wave2,
        worker_types=cfg.swarm_worker_types,
        pattern_library_path=cfg.pattern_library_path,
        project_context=cfg.project_context,
    )

    if not isinstance(hive_result, dict):
        hive_result = {}

    # Map hive result into ForgeExecutionState
    cm_data = hive_result.get("codebase_map", {})
    if isinstance(cm_data, dict):
        # Ensure required fields exist
        cm_data.setdefault("files", [])
        cm_data.setdefault("loc_total", 0)
        cm_data.setdefault("file_count", 0)
        cm_data.setdefault("primary_language", "")
        cm_data.setdefault("languages", [])
        state.codebase_map = CodebaseMap(**cm_data)

    # Parse findings
    from forge.reasoners.discovery import _normalize_finding
    findings_data = hive_result.get("findings", [])
    all_findings = []
    for f_data in findings_data:
        if isinstance(f_data, dict):
            f_data.setdefault("category", "quality")
            f_data.setdefault("severity", "medium")
            f_data.setdefault("title", "Untitled finding")
            f_data.setdefault("description", "")
            _normalize_finding(f_data)
            try:
                all_findings.append(AuditFinding(**f_data))
            except Exception as e:
                logger.warning("Failed to parse hive finding: %s", e)

    # Categorize findings
    state.security_findings = [f for f in all_findings if f.category.value == "security"]
    state.quality_findings = [f for f in all_findings if f.category.value == "quality"]
    state.architecture_findings = [f for f in all_findings if f.category.value == "architecture"]
    state.all_findings = all_findings

    # Intent analysis (LLM-based reasoning about developer intent)
    if state.all_findings:
        try:
            from forge.execution.intent_analyzer import analyze_intent
            intent_result = await analyze_intent(
                findings=state.all_findings,
                repo_path=state.repo_path,
                conventions=None,  # swarm doesn't extract conventions separately
                model=cfg.model_for_role("intent_analyzer"),
                ai_provider=cfg.provider_for_role("intent_analyzer"),
            )
            if intent_result.decisions:
                logger.info(
                    "Intent analysis: %d intentional, %d ambiguous, %d unintentional",
                    intent_result.intentional_count,
                    intent_result.ambiguous_count,
                    intent_result.unintentional_count,
                )
        except Exception as e:
            logger.warning("Intent analysis failed (non-fatal): %s", e)

    # Apply actionability classification (deterministic post-processing)
    if state.all_findings:
        try:
            from forge.execution.actionability import apply_actionability
            apply_actionability(state.all_findings, cfg.project_context)
        except Exception as e:
            logger.warning("Actionability classification failed (non-fatal): %s", e)

    # ── Fingerprint, Baseline, Suppression, Severity (swarm path) ───
    findings_delta: dict = {}
    quality_gate_result: dict = {}
    if state.all_findings:
        try:
            from pathlib import Path as _Path

            from forge.execution.baseline import Baseline
            from forge.execution.fingerprint import fingerprint as _fingerprint
            from forge.execution.forgeignore import ForgeIgnore
            from forge.execution.severity import calibrate_findings

            findings_dicts = [f.model_dump(mode="json") for f in state.all_findings]
            for fd in findings_dicts:
                fd["fingerprint"] = _fingerprint(fd)
            calibrate_findings(findings_dicts)

            forgeignore = ForgeIgnore.load(state.repo_path)
            kept_dicts, suppressed_dicts = forgeignore.apply(findings_dicts)

            artifacts_dir = state.artifacts_dir or str(
                _Path(state.repo_path) / ".artifacts"
            )
            baseline = Baseline.load(artifacts_dir)
            comparison = baseline.update_from_scan(state.forge_run_id, kept_dicts)

            # Track HEAD SHA for delta mode
            try:
                from forge.execution.delta import get_head_sha, save_head_sha
                head_sha = get_head_sha(state.repo_path)
                if head_sha:
                    save_head_sha(artifacts_dir, head_sha)
            except Exception as e:
                logger.debug("Could not record HEAD SHA: %s", e)

            baseline.save(artifacts_dir)

            findings_delta = {
                "new": len(comparison.new_findings),
                "recurring": len(comparison.recurring_findings),
                "fixed": len(comparison.fixed_findings),
                "suppressed": len(suppressed_dicts),
                "regressed": len(comparison.regressed_findings),
            }

            # ── Quality Gate evaluation ──────────────────────────────
            try:
                from forge.execution.quality_gate import (
                    QualityGateThreshold,
                    evaluate_gate,
                )
                threshold = QualityGateThreshold(
                    max_new_critical=cfg.quality_gate_max_critical,
                    max_new_high=cfg.quality_gate_max_high,
                    max_new_medium=cfg.quality_gate_max_medium,
                )
                gate_result = evaluate_gate(comparison.new_findings, threshold)
                quality_gate_result = {
                    "passed": gate_result.passed,
                    "reason": gate_result.reason,
                    "new_critical": gate_result.new_critical,
                    "new_high": gate_result.new_high,
                    "new_medium": gate_result.new_medium,
                    "total_new": gate_result.total_new,
                }
                logger.info("Quality gate: %s", gate_result.reason)
            except Exception as e:
                logger.warning("Quality gate evaluation failed (non-fatal): %s", e)

            # Apply severity changes and filter suppressed
            sev_map = {fd.get("fingerprint", ""): fd.get("severity") for fd in kept_dicts}
            kept_fps = {fd.get("fingerprint", "") for fd in kept_dicts}
            fp_to_finding: dict[str, AuditFinding] = {}
            for fd, finding in zip(findings_dicts, state.all_findings):
                fp_to_finding[fd.get("fingerprint", "")] = finding

            kept_findings: list[AuditFinding] = []
            for fp_val in kept_fps:
                finding = fp_to_finding.get(fp_val)
                if finding:
                    new_sev = sev_map.get(fp_val)
                    if new_sev and new_sev != (finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity)):
                        finding.severity = new_sev
                    kept_findings.append(finding)

            state.all_findings = kept_findings
            state.security_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "security"]
            state.quality_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "quality"]
            state.architecture_findings = [f for f in kept_findings if (f.category.value if hasattr(f.category, "value") else str(f.category)) == "architecture"]

            logger.info(
                "Post-processing [swarm]: %d kept, %d suppressed, delta: %s",
                len(kept_findings), len(suppressed_dicts), findings_delta,
            )
        except Exception as e:
            logger.warning("Fingerprint/baseline/severity processing failed (non-fatal): %s", e)

    # Parse triage result
    triage_data = hive_result.get("triage_result", {})
    if isinstance(triage_data, dict) and triage_data.get("decisions"):
        state.triage_result = TriageResult(**triage_data)

    # Parse remediation plan
    plan_data = hive_result.get("remediation_plan", {})
    if isinstance(plan_data, dict) and plan_data.get("items"):
        state.remediation_plan = RemediationPlan(**plan_data)

    invocations = hive_result.get("stats", {}).get("total_invocations", 1)

    logger.info(
        "Discovery [swarm]: complete — %d security, %d quality, %d architecture findings",
        len(state.security_findings),
        len(state.quality_findings),
        len(state.architecture_findings),
    )

    return {
        "invocations": invocations,
        "findings_delta": findings_delta,
        "quality_gate": quality_gate_result,
    }


async def _run_triage(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    tier1_findings: list[dict] | None = None,
    convergence_context: str = "",
) -> dict:
    """Run Triage phase: Agents 5-6.

    In swarm mode, triage and planning are already done by the synthesis
    agent in Layer 2. Skip if state already has triage + plan.
    """
    rt = _get_run_telemetry()
    if rt:
        rt.set_phase("triage")
    invocations = 0

    if not state.all_findings:
        logger.info("Triage: No findings to triage")
        return {"invocations": 0}

    # Swarm mode: synthesis already produced triage + plan
    if (
        cfg.discovery_mode == "swarm"
        and state.triage_result is not None
        and state.remediation_plan is not None
    ):
        logger.info("Triage: skipping — swarm synthesis already produced triage + plan")
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

    if state.codebase_map is None:
        codebase_map_dict = {}
    elif isinstance(state.codebase_map, dict):
        codebase_map_dict = state.codebase_map
    else:
        codebase_map_dict = state.codebase_map.model_dump()

    # Agent 6: Triage Classifier
    emit_phase_start(cfg, "triage", "Running Agent 6 (Triage Classifier).")
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
        convergence_context=convergence_context,
    )
    if isinstance(plan_dict, dict):
        state.remediation_plan = RemediationPlan(**plan_dict)
    invocations += 1

    # Log strategist dropout
    if state.remediation_plan and state.remediation_plan.items:
        planned_ids = {item.finding_id for item in state.remediation_plan.items}
        all_ids = {f.id for f in state.all_findings}
        dropped = all_ids - planned_ids
        if dropped:
            logger.warning(
                "Fix Strategist dropped %d/%d findings from plan: %s",
                len(dropped), len(all_ids),
                ", ".join(list(dropped)[:5]) + ("..." if len(dropped) > 5 else ""),
            )

    # Safety net: ensure all Tier 2/3 findings have plan items
    if state.triage_result and state.remediation_plan:
        from forge.schemas import RemediationItem, RemediationTier
        tier_map = {}
        for d in state.triage_result.decisions:
            tier_map[d.finding_id] = d.tier
        planned_ids = {item.finding_id for item in state.remediation_plan.items}

        added = 0
        for finding in state.all_findings:
            if finding.id not in planned_ids:
                tier = tier_map.get(finding.id, RemediationTier.TIER_2)
                if tier in (RemediationTier.TIER_2, RemediationTier.TIER_3):
                    state.remediation_plan.items.append(RemediationItem(
                        finding_id=finding.id,
                        title=finding.title,
                        tier=tier,
                        priority=99,  # Low priority — strategist didn't include it
                        estimated_files=1,
                    ))
                    # Add to last execution level
                    if state.remediation_plan.execution_levels:
                        state.remediation_plan.execution_levels[-1].append(finding.id)
                    else:
                        state.remediation_plan.execution_levels.append([finding.id])
                    added += 1

        if added:
            state.remediation_plan.total_items = len(state.remediation_plan.items)
            logger.info(
                "Safety net: added %d dropped Tier 2/3 findings back to plan (total: %d)",
                added, state.remediation_plan.total_items,
            )

    # Write tier assignments back to the finding objects so reports show them
    if state.triage_result and state.triage_result.decisions:
        tier_map = {d.finding_id: d.tier for d in state.triage_result.decisions}
        for finding in state.all_findings:
            if finding.id in tier_map:
                finding.tier = tier_map[finding.id]

    emit_phase_complete(
        cfg, "triage",
        f"Triage complete. {state.remediation_plan.total_items if state.remediation_plan else 0} items in remediation plan.",
    )

    logger.info(
        "Triage complete: %d items in remediation plan",
        state.remediation_plan.total_items if state.remediation_plan else 0,
    )

    return {"invocations": invocations}


async def _run_remediation(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Remediation phase: Tier routing + SWE-AF execution for all AI items."""
    from forge.execution.tier_router import route_plan_items

    rt = _get_run_telemetry()
    if rt:
        rt.set_phase("remediation")
    invocations = 0

    if not state.remediation_plan or not state.remediation_plan.items:
        logger.info("Remediation: no plan items to execute")
        return {"invocations": 0}

    # ── Step 3a: Tier 0/1 — deterministic fixes ──────────────────────
    logger.info("Remediation: routing %d items through tier router", len(state.remediation_plan.items))
    handled, sweaf_items = route_plan_items(
        state.remediation_plan,
        state.all_findings,
        state,
        state.repo_path,
        cfg,
    )
    invocations += len(handled)  # Tier 0/1 count as 1 invocation each

    if not sweaf_items:
        logger.info("Remediation: all items handled by Tier 0/1 — skipping AI pipeline")
        return {"invocations": invocations}

    # ── Step 3b: ALL AI items (Tier 2 + Tier 3) → SWE-AF ────────────
    try:
        from forge.execution.sweaf_bridge import execute_tier3_via_sweaf
        logger.info("Remediation: routing %d AI items to SWE-AF", len(sweaf_items))
        results = await execute_tier3_via_sweaf(
            sweaf_items, state.all_findings, state, cfg,
        )
        state.completed_fixes.extend(results)
        invocations += len(sweaf_items)
    except Exception as e:
        logger.error("SWE-AF dispatch failed: %s", e)
        if cfg.sweaf_fallback_to_forge:
            logger.info("Falling back to FORGE executor for %d items", len(sweaf_items))
            await _run_sweaf_fallback_via_forge(
                app, state, cfg, resolved_models, sweaf_items,
            )
            invocations += state.total_agent_invocations

    if rt:
        actually_fixed = [
            f for f in state.completed_fixes
            if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
        ]
        rt.update_findings_progress(
            fixed=len(actually_fixed),
            deferred=len(state.outer_loop.deferred_findings),
        )

    logger.info(
        "Remediation complete: %d fixed, %d deferred",
        len(state.completed_fixes), len(state.outer_loop.deferred_findings),
    )

    return {"invocations": invocations}


async def _run_sweaf_fallback_via_forge(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    sweaf_items: list,
) -> None:
    """Fall back to FORGE executor when SWE-AF is unavailable."""
    from forge.execution.forge_executor import execute_remediation

    ai_plan = RemediationPlan(
        items=sweaf_items,
        execution_levels=_filter_execution_levels(
            state.remediation_plan.execution_levels if state.remediation_plan else [],
            {item.finding_id for item in sweaf_items},
        ),
        total_items=len(sweaf_items),
    )
    state.remediation_plan = ai_plan
    await execute_remediation(app, NODE_ID, state, cfg, resolved_models)


async def _run_validation(
    app,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> dict:
    """Run Validation phase: Agents 11-12."""
    rt = _get_run_telemetry()
    if rt:
        rt.set_phase("validation")
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
            completed_fixes=all_fixes_json,
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
