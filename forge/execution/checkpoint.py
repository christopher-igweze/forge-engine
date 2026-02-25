"""Checkpoint/resume system for FORGE crash recovery.

Saves execution state at key phase boundaries so a long-running
hardening operation can be resumed after a crash or timeout.

Checkpoint files live in .forge-checkpoints/ within the repo.
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = ".forge-checkpoints"


class CheckpointPhase(str, Enum):
    """Phases where checkpoints are saved."""

    DISCOVERY = "discovery_complete"
    TRIAGE = "triage_complete"
    REMEDIATION = "fix_progress"
    VALIDATION = "validation_complete"


class ForgeCheckpoint(BaseModel):
    """Serializable checkpoint of FORGE execution state."""

    forge_run_id: str
    phase: CheckpointPhase
    repo_path: str
    artifacts_dir: str = ""

    # Serialized state — stored as JSON-safe dicts
    codebase_map: dict | None = None
    security_findings: list[dict] = []
    quality_findings: list[dict] = []
    architecture_findings: list[dict] = []
    all_findings: list[dict] = []

    triage_result: dict | None = None
    remediation_plan: dict | None = None

    completed_fix_ids: list[str] = []
    deferred_finding_ids: list[str] = []
    completed_fixes: list[dict] = []

    integration_result: dict | None = None
    readiness_report: dict | None = None

    total_agent_invocations: int = 0
    estimated_cost_usd: float = 0.0


def _checkpoint_path(repo_path: str, phase: CheckpointPhase) -> str:
    """Get the checkpoint file path for a given phase."""
    cp_dir = os.path.join(repo_path, CHECKPOINT_DIR)
    os.makedirs(cp_dir, exist_ok=True)
    return os.path.join(cp_dir, f"{phase.value}.json")


def save_checkpoint(
    repo_path: str,
    phase: CheckpointPhase,
    state: Any,
) -> None:
    """Save a checkpoint after completing a pipeline phase.

    Args:
        repo_path: Path to the repository.
        phase: Which phase just completed.
        state: ForgeExecutionState instance.
    """
    try:
        cp = ForgeCheckpoint(
            forge_run_id=state.forge_run_id,
            phase=phase,
            repo_path=state.repo_path,
            artifacts_dir=state.artifacts_dir,
            codebase_map=state.codebase_map.model_dump() if state.codebase_map else None,
            security_findings=[f.model_dump() for f in state.security_findings],
            quality_findings=[f.model_dump() for f in state.quality_findings],
            architecture_findings=[f.model_dump() for f in state.architecture_findings],
            all_findings=[f.model_dump() for f in state.all_findings],
            triage_result=state.triage_result.model_dump() if state.triage_result else None,
            remediation_plan=state.remediation_plan.model_dump() if state.remediation_plan else None,
            completed_fix_ids=[f.finding_id for f in state.completed_fixes],
            deferred_finding_ids=list(state.outer_loop.deferred_findings),
            completed_fixes=[f.model_dump() for f in state.completed_fixes],
            integration_result=(
                state.integration_result.model_dump() if state.integration_result else None
            ),
            readiness_report=(
                state.readiness_report.model_dump() if state.readiness_report else None
            ),
            total_agent_invocations=state.total_agent_invocations,
            estimated_cost_usd=state.estimated_cost_usd,
        )

        path = _checkpoint_path(repo_path, phase)
        with open(path, "w") as f:
            json.dump(cp.model_dump(), f, indent=2, default=str)

        logger.info("Checkpoint saved: %s → %s", phase.value, path)

    except Exception as e:
        logger.error("Failed to save checkpoint for %s: %s", phase.value, e)


def load_checkpoint(
    repo_path: str,
    phase: CheckpointPhase,
) -> ForgeCheckpoint | None:
    """Load a checkpoint for a given phase.

    Returns None if no checkpoint exists.
    """
    path = _checkpoint_path(repo_path, phase)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        cp = ForgeCheckpoint(**data)
        logger.info("Checkpoint loaded: %s", phase.value)
        return cp
    except Exception as e:
        logger.error("Failed to load checkpoint %s: %s", phase.value, e)
        return None


def get_latest_checkpoint(repo_path: str) -> ForgeCheckpoint | None:
    """Find the most recent checkpoint across all phases.

    Checks phases in reverse order (validation → remediation → triage → discovery).
    """
    for phase in reversed(CheckpointPhase):
        cp = load_checkpoint(repo_path, phase)
        if cp is not None:
            return cp
    return None


def restore_state(
    cp: ForgeCheckpoint,
) -> Any:
    """Restore ForgeExecutionState from a checkpoint.

    Returns a populated ForgeExecutionState ready to resume.
    """
    from forge.schemas import (
        AuditFinding,
        CodebaseMap,
        CoderFixResult,
        ForgeExecutionState,
        ForgeMode,
        IntegrationValidationResult,
        ProductionReadinessReport,
        RemediationPlan,
        TriageResult,
    )

    state = ForgeExecutionState(
        forge_run_id=cp.forge_run_id,
        mode=ForgeMode.FULL,
        repo_path=cp.repo_path,
        artifacts_dir=cp.artifacts_dir,
    )

    # Restore discovery outputs
    if cp.codebase_map:
        state.codebase_map = CodebaseMap(**cp.codebase_map)
    state.security_findings = [AuditFinding(**f) for f in cp.security_findings]
    state.quality_findings = [AuditFinding(**f) for f in cp.quality_findings]
    state.architecture_findings = [AuditFinding(**f) for f in cp.architecture_findings]
    state.all_findings = [AuditFinding(**f) for f in cp.all_findings]

    # Restore triage outputs
    if cp.triage_result:
        state.triage_result = TriageResult(**cp.triage_result)
    if cp.remediation_plan:
        state.remediation_plan = RemediationPlan(**cp.remediation_plan)

    # Restore remediation state
    state.completed_fixes = [CoderFixResult(**f) for f in cp.completed_fixes]
    state.outer_loop.deferred_findings = list(cp.deferred_finding_ids)

    # Restore validation outputs
    if cp.integration_result:
        state.integration_result = IntegrationValidationResult(**cp.integration_result)
    if cp.readiness_report:
        state.readiness_report = ProductionReadinessReport(**cp.readiness_report)

    state.total_agent_invocations = cp.total_agent_invocations
    state.estimated_cost_usd = cp.estimated_cost_usd

    return state


def clear_checkpoints(repo_path: str) -> None:
    """Remove all checkpoint files for a repo."""
    import shutil

    cp_dir = os.path.join(repo_path, CHECKPOINT_DIR)
    if os.path.isdir(cp_dir):
        shutil.rmtree(cp_dir, ignore_errors=True)
        logger.info("Cleared all checkpoints: %s", cp_dir)
