"""FORGE report generation — discovery and production readiness reports.

Two report types:
- **Discovery Report**: Generated after discovery+triage (Agents 1-7).
  Contains all findings, severity breakdown, and remediation plan.
- **Production Readiness Report**: Generated after full pipeline (Agents 1-12).
  Contains readiness score, category scores, debt items, recommendations.

Both are rendered as JSON + HTML (print-to-PDF ready).
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    ProductionReadinessReport,
    RemediationPlan,
)

# Re-export all private helpers for backward compatibility.
# Tests and other consumers import these names from this module.
from forge.execution.report_helpers import (  # noqa: F401
    _score_color, _score_label, _esc,
)
from forge.execution.report_dependency_graph import (  # noqa: F401
    _load_graph_data, _build_graph_report_data, _render_dependency_graph,
    _render_segment_network_svg, _render_interconnection_table,
    _render_blast_radius, _render_import_chains,
    _build_pattern_library_data, _render_methodology_section,
)
from forge.execution.report_rendering import (  # noqa: F401
    _render_discovery_html, _render_architecture_context, _render_html,
)

logger = logging.getLogger(__name__)


def generate_discovery_report(
    findings: list[AuditFinding],
    plan: RemediationPlan | None,
    artifacts_dir: str,
    run_id: str = "",
    duration_seconds: float = 0.0,
    cost_usd: float = 0.0,
    codebase_map: CodebaseMap | None = None,
    graph_data: dict | None = None,
) -> tuple[dict[str, str], dict]:
    """Generate a discovery-phase report with all findings and remediation plan.

    Called after discovery+triage to produce the final scan report.
    Returns a tuple of (paths dict, report_data dict).
    The paths dict maps {format: file_path} for generated reports.
    The report_data dict contains the full structured discovery report.

    Args:
        graph_data: Enriched CodeGraph dict from hive discovery. If None,
            attempts to load from artifacts_dir/hive/layer1_enriched_graph.json.
    """
    report_dir = os.path.join(artifacts_dir, "report")
    os.makedirs(report_dir, exist_ok=True)

    paths: dict[str, str] = {}

    # Auto-load graph data from hive artifacts if not provided
    if graph_data is None:
        graph_data = _load_graph_data(artifacts_dir)

    # Severity breakdown
    sev_counts = Counter(
        f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        for f in findings
    )
    cat_counts = Counter(
        f.category.value if hasattr(f.category, "value") else str(f.category)
        for f in findings
    )

    # Actionability breakdown
    action_groups: dict[str, list[dict]] = {
        "must_fix": [], "should_fix": [], "consider": [], "informational": [],
    }
    findings_dicts = [f.model_dump(mode="json") for f in findings]
    for fd in findings_dicts:
        tier = fd.get("actionability", "") or "consider"
        if tier in action_groups:
            action_groups[tier].append(fd)
        else:
            action_groups["consider"].append(fd)

    must_should = len(action_groups["must_fix"]) + len(action_groups["should_fix"])
    signal_ratio = round(must_should / len(findings), 2) if findings else 0.0

    report_data = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "discovery",
        "duration_seconds": round(duration_seconds, 1),
        "cost_usd": round(cost_usd, 4),
        "loc_total": codebase_map.loc_total if codebase_map else 0,
        "file_count": codebase_map.file_count if codebase_map else 0,
        "primary_language": codebase_map.primary_language if codebase_map else "",
        "total_findings": len(findings),
        "severity_breakdown": dict(sev_counts),
        "category_breakdown": dict(cat_counts),
        "findings_by_actionability": {
            k: v for k, v in action_groups.items()
        },
        "actionability_summary": {
            "must_fix_count": len(action_groups["must_fix"]),
            "should_fix_count": len(action_groups["should_fix"]),
            "consider_count": len(action_groups["consider"]),
            "informational_count": len(action_groups["informational"]),
            "signal_to_noise_ratio": signal_ratio,
        },
        "findings": findings_dicts,
        "remediation_plan": plan.model_dump(mode="json") if plan else None,
        "codebase_map": codebase_map.model_dump(mode="json") if codebase_map else None,
        "dependency_graph": _build_graph_report_data(graph_data) if graph_data else None,
        "pattern_library": _build_pattern_library_data(findings),
    }

    # JSON report
    json_path = os.path.join(report_dir, "discovery_report.json")
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    paths["json"] = json_path

    # HTML report
    html_path = os.path.join(report_dir, "discovery_report.html")
    html_content = _render_discovery_html(
        findings, plan, sev_counts, run_id,
        duration_seconds, cost_usd, codebase_map, graph_data,
    )
    with open(html_path, "w") as f:
        f.write(html_content)
    paths["html"] = html_path

    logger.info(
        "Discovery report generated: %d findings → %s",
        len(findings), ", ".join(paths.keys()),
    )
    return paths, report_data


def generate_reports(
    report: ProductionReadinessReport,
    artifacts_dir: str,
    run_id: str = "",
) -> dict[str, str]:
    """Generate JSON and HTML reports from the Production Readiness Report.

    Returns a dict of {format: file_path} for generated reports.
    """
    report_dir = os.path.join(artifacts_dir, "report")
    os.makedirs(report_dir, exist_ok=True)

    paths: dict[str, str] = {}

    # JSON report
    json_path = os.path.join(report_dir, "production_readiness.json")
    with open(json_path, "w") as f:
        json.dump(report.model_dump(), f, indent=2, default=str)
    paths["json"] = json_path

    # HTML report (print-to-PDF ready)
    html_path = os.path.join(report_dir, "production_readiness.html")
    html_content = _render_html(report, run_id)
    with open(html_path, "w") as f:
        f.write(html_content)
    paths["html"] = html_path

    # Try PDF generation if weasyprint is available
    try:
        from weasyprint import HTML
        pdf_path = os.path.join(report_dir, "production_readiness.pdf")
        HTML(string=html_content).write_pdf(pdf_path)
        paths["pdf"] = pdf_path
        logger.info("PDF report generated: %s", pdf_path)
    except ImportError:
        logger.info("weasyprint not installed — skipping PDF generation (HTML report available)")
    except Exception as e:
        logger.warning("PDF generation failed: %s (HTML report available)", e)

    logger.info("Reports generated: %s", ", ".join(paths.keys()))
    return paths
