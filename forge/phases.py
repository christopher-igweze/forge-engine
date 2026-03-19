"""FORGE pipeline phase orchestrators.

Each function accepts a duck-typed *dispatcher* (anything with a ``.call()``
method) so it works with the standalone ``StandaloneDispatcher``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from forge.execution.events import emit_phase_complete, emit_phase_start
from forge.execution.json_utils import safe_parse_agent_response
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    FindingSeverity,
    FixOutcome,
    ForgeExecutionState,
    RemediationPlan,
    TriageResult,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)

# ── Utilities (moved from app_helpers.py) ────────────────────────────

NODE_ID = os.getenv("FORGE_NODE_ID", "forge-engine")


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
        actually_fixed = [
            f for f in state.completed_fixes
            if f.outcome in (FixOutcome.COMPLETED, FixOutcome.COMPLETED_WITH_DEBT)
        ]
        if actually_fixed:
            parts.append(f"Fixed: {len(actually_fixed)}")

    if state.outer_loop.deferred_findings:
        parts.append(f"Deferred: {len(state.outer_loop.deferred_findings)}")

    return ". ".join(parts) + "."


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
    """Run Discovery phase: Agents 1-2 (codebase analyst + security auditor)."""
    rt = _get_run_telemetry()
    if rt:
        rt.set_phase("discovery")

    # ── Classic mode: sequential Agent 1 → Agent 2 ───────────────────
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

    # ── Opengrep Deterministic Scan ────────────────────────────────────
    opengrep_findings: list[dict] = []
    if cfg.opengrep_enabled:
        try:
            from forge.execution.opengrep_runner import (
                OpengrepRunner,
                opengrep_available,
                to_audit_finding,
            )

            if opengrep_available():
                rules_dir = cfg.opengrep_rules_dir or None  # None = use built-in
                runner = OpengrepRunner(
                    rules_dirs=[rules_dir] if rules_dir else None,
                    use_community_rules=cfg.opengrep_community_rules,
                    timeout=cfg.opengrep_timeout,
                )
                raw_og_findings = runner.scan(state.repo_path)

                # Convert to AuditFinding-compatible dicts and tag as deterministic
                for og in raw_og_findings:
                    af_dict = to_audit_finding(og)
                    af_dict["source"] = "deterministic"
                    af_dict["agent"] = "opengrep"
                    opengrep_findings.append(af_dict)

                logger.info(
                    "Opengrep found %d deterministic findings",
                    len(opengrep_findings),
                )
            else:
                logger.info("Opengrep not installed — skipping deterministic scan")
        except Exception as e:
            logger.warning("Opengrep scan failed (non-fatal): %s", e)

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

    # Agent 2: Security Auditor (only LLM auditor in v3 — quality + architecture covered by deterministic checks)
    emit_phase_start(cfg, "discovery", "Running Agent 2 (Security Auditor).")
    logger.info("Discovery: Running Agent 2 (Security Auditor)")

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

    security_result = await app.call(
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
    )
    invocations += 1  # Security auditor only

    # Parse security results
    result_dict = security_result if isinstance(security_result, dict) else {}
    state.security_findings = [
        AuditFinding(**f) for f in result_dict.get("findings", [])
    ]
    # DEPRECATED: Quality auditor and architecture reviewer removed in v3
    # Covered by deterministic checks in forge/evaluation/checks/
    state.quality_findings = []
    state.architecture_findings = []

    # Merge all findings
    state.all_findings = (
        state.security_findings +
        state.quality_findings +
        state.architecture_findings
    )

    # Merge Opengrep deterministic findings with LLM findings
    if opengrep_findings:
        from forge.schemas import FindingCategory, FindingLocation
        for og_dict in opengrep_findings:
            try:
                cat_map = {
                    "security": FindingCategory.SECURITY,
                    "quality": FindingCategory.QUALITY,
                    "performance": FindingCategory.PERFORMANCE,
                    "reliability": FindingCategory.RELIABILITY,
                    "architecture": FindingCategory.ARCHITECTURE,
                }
                cat = cat_map.get(
                    og_dict.get("category", "security"), FindingCategory.SECURITY
                )
                sev_map = {
                    "critical": FindingSeverity.CRITICAL,
                    "high": FindingSeverity.HIGH,
                    "medium": FindingSeverity.MEDIUM,
                    "low": FindingSeverity.LOW,
                    "info": FindingSeverity.INFO,
                }
                sev = sev_map.get(
                    og_dict.get("severity", "medium"), FindingSeverity.MEDIUM
                )

                locs = []
                for loc in og_dict.get("locations", []):
                    locs.append(FindingLocation(
                        file_path=loc.get("file_path", ""),
                        line_start=loc.get("line_start"),
                        line_end=loc.get("line_end"),
                        snippet=loc.get("snippet", ""),
                    ))

                af = AuditFinding(
                    title=og_dict.get("title", ""),
                    description=og_dict.get("description", ""),
                    category=cat,
                    severity=sev,
                    locations=locs,
                    confidence=og_dict.get("confidence", 0.9),
                    cwe_id=og_dict.get("cwe_id", ""),
                    owasp_ref=og_dict.get("owasp_ref", ""),
                    agent="opengrep",
                    data_flow=og_dict.get("data_flow", ""),
                    suggested_fix=og_dict.get("suggested_fix", ""),
                    intent_signal="unintentional",
                )
                state.all_findings.append(af)
            except Exception as e:
                logger.warning("Failed to convert Opengrep finding: %s", e)

        logger.info(
            "Merged %d Opengrep findings into %d total findings",
            len(opengrep_findings),
            len(state.all_findings),
        )
    if rt:
        rt.update_findings_progress(total=len(state.all_findings))
    emit_phase_complete(
        cfg, "discovery",
        f"Agent 2 complete. {len(state.all_findings)} total findings.",
    )

    # DEPRECATED: Intent analysis removed in v3
    # Covered by .forgeignore rules + <intent_detection> block in security auditor prompt

    # Apply actionability classification (deterministic post-processing)
    if state.all_findings:
        try:
            from forge.execution.actionability import apply_actionability
            apply_actionability(state.all_findings, cfg.project_context)
        except Exception as e:
            logger.warning("Actionability classification failed (non-fatal): %s", e)

    emit_phase_complete(cfg, "actionability", "Actionability classification complete.")

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
                        finding.severity = FindingSeverity(new_sev)
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
            logger.warning("Fingerprint/baseline/severity processing failed (non-fatal): %s", e, exc_info=True)

    # ── v3 Deterministic Evaluation ────────────────────────────────────
    evaluation_result = None
    try:
        from forge.evaluation import run_evaluation
        eval_weights = cfg.evaluation_weights
        gate_profile = cfg.quality_gate_profile

        # Build baseline comparison dict for quality gate (from existing v2 data)
        baseline_comp = None
        if findings_delta:
            baseline_comp = {
                "new_critical": sum(
                    1 for f in state.all_findings
                    if getattr(f, "severity", None) == FindingSeverity.CRITICAL
                ) if findings_delta.get("new", 0) > 0 else 0,
                "new_high": sum(
                    1 for f in state.all_findings
                    if getattr(f, "severity", None) == FindingSeverity.HIGH
                ) if findings_delta.get("new", 0) > 0 else 0,
                "new_medium": sum(
                    1 for f in state.all_findings
                    if getattr(f, "severity", None) == FindingSeverity.MEDIUM
                ) if findings_delta.get("new", 0) > 0 else 0,
            }

        evaluation_result = run_evaluation(
            state.repo_path,
            gate_profile=gate_profile,
            weights=eval_weights,
            baseline_comparison=baseline_comp,
            opengrep_findings=opengrep_findings if opengrep_findings else None,
        )
    except Exception as e:
        logger.warning("v3 evaluation failed (non-fatal): %s", e)

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
        "evaluation": evaluation_result,
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

    # DEPRECATED: Triage Classifier (Agent 6) removed in v3
    # Tier assignment now handled by fix strategist directly
    triage_dict = None

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


