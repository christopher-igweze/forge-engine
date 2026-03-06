"""FORGE compliance layer — NIST SP 800-218A (SSDF) mapping and attestation.

Usage::

    from forge.compliance import generate_full_compliance

    result = generate_full_compliance(
        forge_run_id="abc-123",
        findings=[...],
        fixes=[...],
        validation={...},
        readiness_report={...},
        repo_url="https://github.com/org/repo",
        output_dir=Path("./artifacts/compliance"),
    )
    # result["compliance_report"]  — ComplianceReport dataclass
    # result["attestation"]        — AttestationData dataclass
    # result["attestation_md"]     — rendered Markdown string
    # result["attestation_json"]   — rendered JSON dict
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.compliance.attestation import (
    AttestationData,
    generate_attestation_json,
    generate_attestation_markdown,
    save_attestation,
)
from forge.compliance.nist_ssdf import ComplianceReport, generate_compliance_report

__all__ = ["ComplianceReport", "generate_full_compliance"]

logger = logging.getLogger(__name__)


def generate_full_compliance(
    forge_run_id: str,
    findings: list[dict[str, Any]],
    fixes: list[dict[str, Any]] | None = None,
    validation: dict[str, Any] | None = None,
    readiness_report: dict[str, Any] | None = None,
    repo_url: str = "",
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate compliance report + attestation from FORGE run data.

    This is a post-processing step called after a FORGE run completes.
    It does not modify the validation reasoner — it consumes its output.

    Returns dict with keys:
        - ``compliance_report``: :class:`ComplianceReport`
        - ``attestation``: :class:`AttestationData`
        - ``attestation_md``: rendered Markdown string
        - ``attestation_json``: rendered JSON dict

    Optionally saves to *output_dir* as ``attestation.md`` and
    ``attestation.json``.
    """
    # ── 1. Generate SSDF compliance report ────────────────────────────
    compliance = generate_compliance_report(
        forge_run_id=forge_run_id,
        findings=findings,
        fixes=fixes,
        validation=validation,
    )

    # ── 2. Extract data from readiness report (if available) ──────────
    report = readiness_report or {}
    overall_score = report.get("overall_score", 0)
    category_scores = report.get("category_scores", [])
    findings_total = report.get("findings_total", len(findings))
    findings_fixed = report.get("findings_fixed", len(fixes or []))
    findings_deferred = report.get("findings_deferred", 0)

    # ── 3. Extract scope from findings (best-effort) ──────────────────
    loc_total = 0
    file_count = 0
    # If any finding has codebase_map data, use it
    for f in findings:
        if f.get("loc_total"):
            loc_total = f["loc_total"]
        if f.get("file_count"):
            file_count = f["file_count"]

    # ── 4. Extract validation data ────────────────────────────────────
    val = validation or {}
    tests_run = val.get("tests_run", 0)
    tests_passed = val.get("tests_passed", 0)
    regressions = len(val.get("regressions_detected", []))

    # ── 5. Build attestation ──────────────────────────────────────────
    attestation = AttestationData(
        forge_run_id=forge_run_id,
        repo_url=repo_url,
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        loc_total=loc_total,
        file_count=file_count,
        agent_invocations=0,  # Caller can enrich from telemetry
        total_findings=findings_total,
        findings_fixed=findings_fixed,
        findings_deferred=findings_deferred,
        overall_score=overall_score,
        category_scores=category_scores,
        compliance=compliance,
        validation_tests_run=tests_run,
        validation_tests_passed=tests_passed,
        validation_regressions=regressions,
    )

    attestation_md = generate_attestation_markdown(attestation)
    attestation_json = generate_attestation_json(attestation)

    # ── 6. Persist if output_dir provided ─────────────────────────────
    if output_dir:
        save_attestation(attestation, output_dir)

    logger.info(
        "Full compliance generated for run %s: %d SSDF practices, score %d/100",
        forge_run_id,
        len(compliance.practices),
        overall_score,
    )

    return {
        "compliance_report": compliance,
        "attestation": attestation,
        "attestation_md": attestation_md,
        "attestation_json": attestation_json,
    }
