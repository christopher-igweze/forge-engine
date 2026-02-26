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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    ProductionReadinessReport,
    RemediationPlan,
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
) -> dict[str, str]:
    """Generate a discovery-phase report with all findings and remediation plan.

    Called after discovery+triage (Agents 1-7) even in dry_run mode.
    Returns a dict of {format: file_path} for generated reports.

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
    return paths


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


def _score_color(score: int) -> str:
    """Get a CSS color for a readiness score."""
    if score >= 80:
        return "#22c55e"  # green
    if score >= 60:
        return "#eab308"  # yellow
    if score >= 40:
        return "#f97316"  # orange
    return "#ef4444"  # red


def _score_label(score: int) -> str:
    """Get a human-readable label for a readiness score."""
    if score >= 80:
        return "Production Ready"
    if score >= 60:
        return "Needs Improvement"
    if score >= 40:
        return "Significant Issues"
    return "Not Production Ready"


def _render_discovery_html(
    findings: list[AuditFinding],
    plan: RemediationPlan | None,
    sev_counts: Counter,
    run_id: str,
    duration_seconds: float,
    cost_usd: float,
    codebase_map: CodebaseMap | None = None,
    graph_data: dict | None = None,
) -> str:
    """Render a discovery-phase findings report as HTML."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(findings)

    # Sort findings: critical first, then high, medium, low, info
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_findings = sorted(
        findings,
        key=lambda f: sev_order.get(
            f.severity.value if hasattr(f.severity, "value") else str(f.severity), 5
        ),
    )

    # Severity summary boxes
    sev_boxes = ""
    for sev in ("critical", "high", "medium", "low", "info"):
        count = sev_counts.get(sev, 0)
        if count > 0:
            sev_boxes += f'<div class="sev-box {sev}"><div class="num">{count}</div><div class="lbl">{sev}</div></div>\n'

    # ── Architecture Context section (from CodebaseMap) ──────────────
    arch_html = ""
    if codebase_map:
        arch_html = _render_architecture_context(codebase_map, findings)

    # ── Dependency Graph section (from CodeGraph) ────────────────────
    graph_html = ""
    if graph_data:
        graph_html = _render_dependency_graph(graph_data, findings)

    # ── Analysis Methodology section ──────────────────────────────
    methodology_html = _render_methodology_section(findings)

    # Build findings rows grouped by actionability
    _ACTION_ORDER = ["must_fix", "should_fix", "consider", "informational"]
    _ACTION_LABELS = {
        "must_fix": "Must Fix",
        "should_fix": "Should Fix",
        "consider": "Consider",
        "informational": "Informational",
    }

    def _finding_row(f: AuditFinding) -> str:
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        act = f.actionability or "consider"
        loc = ""
        if f.locations:
            first = f.locations[0]
            loc = _esc(first.file_path)
            if first.line_start:
                loc += f":{first.line_start}"

        # Data flow trace (from new data_flow field)
        data_flow_html = ""
        if f.data_flow:
            data_flow_html = f'<div class="desc"><em>Flow:</em> {_esc(f.data_flow)}</div>'

        # Cross-reference: which data flows touch this finding's file?
        impact_html = ""
        if codebase_map and f.locations:
            affected_files = {l.file_path for l in f.locations}
            related_flows = [
                df for df in codebase_map.data_flows
                if any(af in df.source or af in df.destination for af in affected_files)
            ]
            if related_flows:
                flow_tags = " ".join(
                    f'<span class="flow-tag">{_esc(df.source)} &rarr; {_esc(df.destination)}</span>'
                    for df in related_flows[:3]
                )
                impact_html = f'<div class="impact">Ripple: {flow_tags}</div>'

        return f"""
        <tr>
            <td><span class="severity {sev}">{sev}</span></td>
            <td><span class="actionability {act}">{_ACTION_LABELS.get(act, act)}</span></td>
            <td>
                <strong>{_esc(f.title)}</strong>
                <div class="desc">{_esc(f.description)}</div>
                {data_flow_html}
                {impact_html}
            </td>
            <td class="loc">{loc}</td>
        </tr>"""

    # Group findings by actionability tier
    grouped: dict[str, list[AuditFinding]] = {k: [] for k in _ACTION_ORDER}
    for f in sorted_findings:
        act = f.actionability or "consider"
        if act in grouped:
            grouped[act].append(f)
        else:
            grouped["consider"].append(f)

    findings_html = ""
    for action_tier in _ACTION_ORDER:
        group = grouped[action_tier]
        if not group:
            continue
        label = _ACTION_LABELS[action_tier]
        findings_rows = "".join(_finding_row(f) for f in group)
        findings_html += f"""
    <div class="action-group">
        <div class="action-group-header">
            <span class="actionability {action_tier}">{label}</span>
            <span class="action-count">{len(group)} finding{'s' if len(group) != 1 else ''}</span>
        </div>
        <table>
            <thead><tr><th>Severity</th><th>Action</th><th>Finding</th><th>Location</th></tr></thead>
            <tbody>{findings_rows}</tbody>
        </table>
    </div>"""

    # Remediation plan section
    plan_html = ""
    if plan and plan.items:
        plan_rows = ""
        for item in plan.items:
            tier_val = item.tier.value if hasattr(item.tier, "value") else str(item.tier)
            plan_rows += f"""
            <tr>
                <td>P{item.priority}</td>
                <td>T{tier_val}</td>
                <td><strong>{_esc(item.title)}</strong></td>
                <td>{', '.join(_esc(fp) for fp in item.files_to_modify[:3])}</td>
            </tr>"""
        plan_html = f"""
        <h2>Remediation Plan ({plan.total_items} items across {len(plan.execution_levels)} levels)</h2>
        {f'<p class="plan-summary">{_esc(plan.summary)}</p>' if plan.summary else ''}
        <table>
            <thead><tr><th>Priority</th><th>Tier</th><th>Fix</th><th>Files</th></tr></thead>
            <tbody>{plan_rows}</tbody>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FORGE Discovery Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            color: #1e293b; background: #fff; padding: 2rem; max-width: 960px; margin: 0 auto;
        }}
        .header {{ text-align: center; margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 2px solid #e2e8f0; }}
        .header h1 {{ font-size: 1.5rem; color: #0f172a; margin-bottom: 0.25rem; }}
        .header .subtitle {{ color: #64748b; font-size: 0.9rem; }}
        .meta {{ display: flex; justify-content: center; gap: 2rem; margin-bottom: 1.5rem; color: #64748b; font-size: 0.85rem; }}
        .sev-summary {{ display: flex; justify-content: center; gap: 1rem; margin-bottom: 2rem; }}
        .sev-box {{ text-align: center; padding: 0.75rem 1.25rem; border-radius: 8px; min-width: 80px; }}
        .sev-box .num {{ font-size: 1.5rem; font-weight: 700; }}
        .sev-box .lbl {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }}
        .sev-box.critical {{ background: #fef2f2; color: #991b1b; }}
        .sev-box.high {{ background: #fff7ed; color: #9a3412; }}
        .sev-box.medium {{ background: #fefce8; color: #854d0e; }}
        .sev-box.low {{ background: #f0fdf4; color: #166534; }}
        .sev-box.info {{ background: #f0f9ff; color: #075985; }}
        h2 {{ font-size: 1.2rem; color: #0f172a; margin: 1.5rem 0 0.75rem; padding-bottom: 0.5rem; border-bottom: 1px solid #e2e8f0; }}
        h3 {{ font-size: 1rem; color: #334155; margin: 1rem 0 0.5rem; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 1rem; font-size: 0.85rem; }}
        th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
        th {{ background: #f8fafc; font-weight: 600; }}
        .desc {{ color: #64748b; font-size: 0.8rem; margin-top: 0.25rem; }}
        .loc {{ font-family: monospace; font-size: 0.8rem; color: #475569; max-width: 200px; word-break: break-all; }}
        .severity {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }}
        .severity.critical {{ background: #fef2f2; color: #991b1b; }}
        .severity.high {{ background: #fff7ed; color: #9a3412; }}
        .severity.medium {{ background: #fefce8; color: #854d0e; }}
        .severity.low {{ background: #f0fdf4; color: #166534; }}
        .severity.info {{ background: #f0f9ff; color: #075985; }}
        .tier {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; background: #f1f5f9; color: #475569; }}
        .actionability {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; }}
        .actionability.must_fix {{ background: #fef2f2; color: #991b1b; }}
        .actionability.should_fix {{ background: #fff7ed; color: #9a3412; }}
        .actionability.consider {{ background: #fefce8; color: #854d0e; }}
        .actionability.informational {{ background: #f0f9ff; color: #075985; }}
        .action-group {{ margin-bottom: 1.5rem; }}
        .action-group-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
        .action-count {{ font-size: 0.85rem; color: #64748b; }}
        .plan-summary {{ color: #475569; margin-bottom: 0.75rem; line-height: 1.5; }}
        .arch-summary {{ color: #475569; line-height: 1.6; margin-bottom: 1rem; white-space: pre-line; }}
        .arch-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }}
        .arch-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 0.75rem; }}
        .arch-card h4 {{ font-size: 0.85rem; color: #0f172a; margin-bottom: 0.5rem; }}
        .arch-card .mono {{ font-family: monospace; font-size: 0.8rem; color: #475569; }}
        .flow-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; font-size: 0.8rem; }}
        .flow-arrow {{ color: #94a3b8; flex-shrink: 0; }}
        .flow-src, .flow-dst {{ font-family: monospace; font-size: 0.78rem; color: #334155; }}
        .flow-type {{ font-size: 0.7rem; color: #64748b; background: #f1f5f9; padding: 1px 6px; border-radius: 3px; }}
        .flow-auth {{ font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; }}
        .flow-auth.yes {{ background: #f0fdf4; color: #166534; }}
        .flow-auth.no {{ background: #fef2f2; color: #991b1b; }}
        .auth-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; font-size: 0.8rem; }}
        .auth-path {{ font-family: monospace; font-size: 0.78rem; color: #334155; flex: 1; }}
        .auth-badge {{ font-size: 0.7rem; padding: 1px 6px; border-radius: 3px; font-weight: 600; }}
        .auth-badge.protected {{ background: #f0fdf4; color: #166534; }}
        .auth-badge.unprotected {{ background: #fef2f2; color: #991b1b; }}
        .auth-type {{ font-size: 0.7rem; color: #64748b; background: #f1f5f9; padding: 1px 6px; border-radius: 3px; }}
        .impact {{ margin-top: 0.25rem; }}
        .flow-tag {{ display: inline-block; font-size: 0.7rem; color: #6366f1; background: #eef2ff; padding: 1px 6px; border-radius: 3px; margin-right: 4px; font-family: monospace; }}
        .pattern-tag {{ display: inline-block; font-size: 0.75rem; color: #475569; background: #f1f5f9; padding: 2px 8px; border-radius: 4px; margin: 2px 4px 2px 0; }}
        .dep-tag {{ display: inline-block; font-size: 0.7rem; padding: 2px 7px; border-radius: 3px; margin: 2px 3px 2px 0; font-family: monospace; }}
        .dep-tag.imports {{ background: #eef2ff; color: #4f46e5; }}
        .dep-tag.imported-by {{ background: #fef3c7; color: #92400e; }}
        .dep-tag.direct {{ background: #fef2f2; color: #991b1b; }}
        .dep-tag.downstream {{ background: #fff7ed; color: #9a3412; }}
        .finding-badge {{ display: inline-block; font-size: 0.65rem; padding: 1px 5px; border-radius: 3px; background: #fef2f2; color: #991b1b; font-weight: 600; margin-left: 4px; }}
        .chain-list {{ margin-bottom: 1rem; }}
        .chain-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; margin-bottom: 0.5rem; padding: 0.4rem 0.6rem; background: #f8fafc; border-radius: 6px; border: 1px solid #e2e8f0; font-size: 0.8rem; }}
        .chain-src, .chain-dst {{ font-family: monospace; font-weight: 600; color: #334155; }}
        .chain-arrow {{ color: #94a3b8; }}
        .chain-count {{ font-size: 0.7rem; background: #6366f1; color: #fff; padding: 1px 6px; border-radius: 3px; font-weight: 600; }}
        .chain-files {{ flex-basis: 100%; margin-top: 0.2rem; }}
        .footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 2px solid #e2e8f0; text-align: center; color: #94a3b8; font-size: 0.8rem; }}
        @media print {{
            body {{ padding: 1rem; }}
            .severity, .sev-box {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
            .arch-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>FORGE Discovery Report</h1>
        <div class="subtitle">Run ID: {_esc(run_id)} &bull; Generated: {generated}</div>
    </div>

    <div class="meta">
        <span>{total} findings</span>
        {f'<span>{codebase_map.loc_total:,} LOC</span>' if codebase_map and codebase_map.loc_total else ''}
        {f'<span>{codebase_map.file_count} files</span>' if codebase_map and codebase_map.file_count else ''}
        <span>{duration_seconds:.0f}s runtime</span>
        <span>${cost_usd:.4f} cost</span>
    </div>

    <div class="sev-summary">
        {sev_boxes}
    </div>

    {arch_html}

    {graph_html}

    {methodology_html}

    <h2>All Findings</h2>
    {findings_html}

    {plan_html}

    <div class="footer">
        Generated by FORGE &mdash; Framework for Orchestrated Remediation &amp; Governance Engine<br>
        &copy; {datetime.now().year} Verstand AI
    </div>
</body>
</html>"""


def _render_architecture_context(
    cmap: CodebaseMap,
    findings: list[AuditFinding],
) -> str:
    """Build HTML for the Architecture Context section from CodebaseMap data."""
    sections: list[str] = []

    # Architecture summary
    if cmap.architecture_summary:
        sections.append(
            f'<h2>Architecture Context</h2>\n'
            f'<div class="arch-summary">{_esc(cmap.architecture_summary)}</div>'
        )
    else:
        sections.append('<h2>Architecture Context</h2>')

    # Module map + entry points grid
    cards: list[str] = []
    if cmap.modules:
        mod_items = ""
        for m in cmap.modules[:12]:
            purpose = f" &mdash; {_esc(m.purpose)}" if m.purpose else ""
            loc = f" ({m.loc} LOC)" if m.loc else ""
            mod_items += f'<div class="mono">{_esc(m.path)}{purpose}{loc}</div>\n'
        cards.append(
            f'<div class="arch-card"><h4>Modules ({len(cmap.modules)})</h4>{mod_items}</div>'
        )

    if cmap.entry_points:
        ep_items = ""
        for ep in cmap.entry_points[:8]:
            ep_type = f' <span class="flow-type">{_esc(ep.type)}</span>' if ep.type else ""
            pub = " (public)" if ep.is_public else " (internal)"
            ep_items += f'<div class="mono">{_esc(ep.path)}{ep_type}{pub}</div>\n'
        cards.append(
            f'<div class="arch-card"><h4>Entry Points ({len(cmap.entry_points)})</h4>{ep_items}</div>'
        )

    if cards:
        sections.append(f'<div class="arch-grid">{"".join(cards)}</div>')

    # Key patterns
    if cmap.key_patterns:
        tags = " ".join(f'<span class="pattern-tag">{_esc(p)}</span>' for p in cmap.key_patterns)
        sections.append(f'<h3>Key Patterns</h3><div style="margin-bottom:1rem">{tags}</div>')

    # Data flows — the core relationship context
    if cmap.data_flows:
        flow_items = ""
        for df in cmap.data_flows:
            dtype = f' <span class="flow-type">{_esc(df.data_type)}</span>' if df.data_type else ""
            auth_cls = "yes" if df.is_authenticated else "no"
            auth_lbl = "auth" if df.is_authenticated else "no auth"
            flow_items += (
                f'<div class="flow-row">'
                f'<span class="flow-src">{_esc(df.source)}</span>'
                f'<span class="flow-arrow">&rarr;</span>'
                f'<span class="flow-dst">{_esc(df.destination)}</span>'
                f'{dtype}'
                f' <span class="flow-auth {auth_cls}">{auth_lbl}</span>'
                f'</div>\n'
            )
        sections.append(
            f'<h3>Data Flows ({len(cmap.data_flows)})</h3>\n{flow_items}'
        )

    # Auth boundaries
    if cmap.auth_boundaries:
        auth_items = ""
        for ab in cmap.auth_boundaries:
            badge_cls = "protected" if ab.is_protected else "unprotected"
            badge_lbl = "protected" if ab.is_protected else "unprotected"
            atype = f' <span class="auth-type">{_esc(ab.auth_type)}</span>' if ab.auth_type else ""
            auth_items += (
                f'<div class="auth-row">'
                f'<span class="auth-path">{_esc(ab.path)}</span>'
                f'<span class="auth-badge {badge_cls}">{badge_lbl}</span>'
                f'{atype}'
                f'</div>\n'
            )
        sections.append(
            f'<h3>Auth Boundaries ({len(cmap.auth_boundaries)})</h3>\n{auth_items}'
        )

    # Cross-reference: which modules are most affected by findings?
    if cmap.modules and findings:
        module_finding_counts: dict[str, int] = {}
        for f in findings:
            for loc in f.locations:
                for m in cmap.modules:
                    if loc.file_path.startswith(m.path) or m.path in loc.file_path:
                        module_finding_counts[m.name] = module_finding_counts.get(m.name, 0) + 1
                        break
        if module_finding_counts:
            sorted_mods = sorted(module_finding_counts.items(), key=lambda x: -x[1])
            hotspot_items = ""
            for name, count in sorted_mods[:8]:
                bar_width = min(count * 15, 100)
                hotspot_items += (
                    f'<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.25rem">'
                    f'<span style="width:160px;font-size:0.8rem;font-family:monospace">{_esc(name)}</span>'
                    f'<div style="flex:1;height:16px;background:#e2e8f0;border-radius:4px;overflow:hidden">'
                    f'<div style="width:{bar_width}%;height:100%;background:#6366f1;border-radius:4px"></div></div>'
                    f'<span style="font-size:0.8rem;font-weight:600;width:30px;text-align:right">{count}</span>'
                    f'</div>\n'
                )
            sections.append(
                f'<h3>Finding Hotspots by Module</h3>\n{hotspot_items}'
            )

    return "\n".join(sections)


def _render_html(report: ProductionReadinessReport, run_id: str) -> str:
    """Render the Production Readiness Report as HTML."""
    score = report.overall_score
    color = _score_color(score)
    label = _score_label(score)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Category score bars
    category_html = ""
    for cs in report.category_scores:
        bar_color = _score_color(cs.score)
        category_html += f"""
        <div class="category-row">
            <div class="category-label">{_esc(cs.name)}</div>
            <div class="category-bar-bg">
                <div class="category-bar" style="width: {cs.score}%; background: {bar_color};"></div>
            </div>
            <div class="category-score">{cs.score}/100</div>
        </div>
        """

    # Debt items
    debt_html = ""
    if report.debt_items:
        debt_rows = ""
        for item in report.debt_items:
            sev_class = item.severity.value if hasattr(item.severity, 'value') else str(item.severity)
            debt_rows += f"""
            <tr>
                <td>{_esc(item.title)}</td>
                <td><span class="severity {sev_class}">{sev_class}</span></td>
                <td>{_esc(item.reason_deferred)}</td>
            </tr>
            """
        debt_html = f"""
        <h2>Technical Debt</h2>
        <table>
            <thead><tr><th>Issue</th><th>Severity</th><th>Reason Deferred</th></tr></thead>
            <tbody>{debt_rows}</tbody>
        </table>
        """

    # Recommendations
    recs_html = ""
    if report.recommendations:
        recs_items = "".join(f"<li>{_esc(r)}</li>" for r in report.recommendations)
        recs_html = f"<h2>Recommendations</h2><ol>{recs_items}</ol>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FORGE Production Readiness Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            color: #1e293b;
            background: #fff;
            padding: 2rem;
            max-width: 800px;
            margin: 0 auto;
        }}
        .header {{
            text-align: center;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
            border-bottom: 2px solid #e2e8f0;
        }}
        .header h1 {{ font-size: 1.5rem; color: #0f172a; margin-bottom: 0.25rem; }}
        .header .subtitle {{ color: #64748b; font-size: 0.9rem; }}
        .score-ring {{
            width: 160px;
            height: 160px;
            margin: 1.5rem auto;
            position: relative;
        }}
        .score-ring svg {{ transform: rotate(-90deg); }}
        .score-ring circle {{
            fill: none;
            stroke-width: 12;
            stroke-linecap: round;
        }}
        .score-ring .bg {{ stroke: #e2e8f0; }}
        .score-ring .fg {{ stroke: {color}; transition: stroke-dashoffset 1s ease; }}
        .score-value {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            text-align: center;
        }}
        .score-value .number {{ font-size: 2.5rem; font-weight: 700; color: {color}; }}
        .score-value .label {{ font-size: 0.75rem; color: #64748b; }}
        .score-label {{
            text-align: center;
            font-size: 1.1rem;
            font-weight: 600;
            color: {color};
            margin-bottom: 1.5rem;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 2rem;
            margin-bottom: 2rem;
            padding: 1rem;
            background: #f8fafc;
            border-radius: 8px;
        }}
        .stat {{ text-align: center; }}
        .stat .num {{ font-size: 1.5rem; font-weight: 700; color: #0f172a; }}
        .stat .lbl {{ font-size: 0.8rem; color: #64748b; }}
        h2 {{
            font-size: 1.2rem;
            color: #0f172a;
            margin: 1.5rem 0 0.75rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid #e2e8f0;
        }}
        .category-row {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 0.5rem;
        }}
        .category-label {{ width: 140px; font-size: 0.9rem; font-weight: 500; }}
        .category-bar-bg {{
            flex: 1;
            height: 20px;
            background: #e2e8f0;
            border-radius: 10px;
            overflow: hidden;
        }}
        .category-bar {{ height: 100%; border-radius: 10px; }}
        .category-score {{ width: 60px; text-align: right; font-size: 0.85rem; font-weight: 600; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 1rem;
            font-size: 0.9rem;
        }}
        th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #e2e8f0; }}
        th {{ background: #f8fafc; font-weight: 600; }}
        .severity {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .severity.critical {{ background: #fef2f2; color: #991b1b; }}
        .severity.high {{ background: #fff7ed; color: #9a3412; }}
        .severity.medium {{ background: #fefce8; color: #854d0e; }}
        .severity.low {{ background: #f0fdf4; color: #166534; }}
        .severity.info {{ background: #f0f9ff; color: #075985; }}
        ol {{ padding-left: 1.5rem; }}
        li {{ margin-bottom: 0.5rem; line-height: 1.5; }}
        .footer {{
            margin-top: 2rem;
            padding-top: 1rem;
            border-top: 2px solid #e2e8f0;
            text-align: center;
            color: #94a3b8;
            font-size: 0.8rem;
        }}
        @media print {{
            body {{ padding: 1rem; }}
            .category-bar {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
            .severity {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>FORGE Production Readiness Report</h1>
        <div class="subtitle">Run ID: {_esc(run_id)} &bull; Generated: {generated}</div>
    </div>

    <div class="score-ring">
        <svg width="160" height="160" viewBox="0 0 160 160">
            <circle class="bg" cx="80" cy="80" r="68"></circle>
            <circle class="fg" cx="80" cy="80" r="68"
                stroke-dasharray="{427.26}"
                stroke-dashoffset="{427.26 * (1 - score / 100):.1f}">
            </circle>
        </svg>
        <div class="score-value">
            <div class="number">{score}</div>
            <div class="label">/ 100</div>
        </div>
    </div>
    <div class="score-label">{label}</div>

    <div class="stats">
        <div class="stat"><div class="num">{report.findings_total}</div><div class="lbl">Total Issues</div></div>
        <div class="stat"><div class="num">{report.findings_fixed}</div><div class="lbl">Fixed</div></div>
        <div class="stat"><div class="num">{report.findings_deferred}</div><div class="lbl">Deferred</div></div>
    </div>

    {f'<h2>Category Scores</h2>{category_html}' if category_html else ''}

    {f'<div class="summary"><h2>Summary</h2><p>{_esc(report.summary)}</p></div>' if report.summary else ''}

    {debt_html}

    {recs_html}

    {f'<h2>Investor Summary</h2><p>{_esc(report.investor_summary)}</p>' if report.investor_summary else ''}

    <div class="footer">
        Generated by FORGE &mdash; Framework for Orchestrated Remediation &amp; Governance Engine<br>
        &copy; {datetime.now().year} Verstand AI
    </div>
</body>
</html>"""


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Dependency Graph Helpers ─────────────────────────────────────────


def _load_graph_data(artifacts_dir: str) -> dict | None:
    """Try to load the enriched CodeGraph from hive artifacts."""
    for candidate in (
        os.path.join(artifacts_dir, "hive", "layer1_enriched_graph.json"),
        os.path.join(artifacts_dir, "hive", "layer0_graph.json"),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate) as f:
                    data = json.load(f)
                if isinstance(data, dict) and "nodes" in data:
                    logger.info("Loaded graph data from %s", candidate)
                    return data
            except Exception as e:
                logger.warning("Failed to load graph from %s: %s", candidate, e)
    return None


def _build_graph_report_data(graph_data: dict) -> dict:
    """Extract a serializable summary of the dependency graph for the JSON report."""
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    segments = graph_data.get("segments", [])

    # Build segment summary with inter-segment dependencies
    segment_deps: list[dict] = []
    for seg in segments:
        segment_deps.append({
            "id": seg.get("id", ""),
            "label": seg.get("label", ""),
            "files": seg.get("files", []),
            "loc": seg.get("loc", 0),
            "finding_count": len(seg.get("findings", [])),
            "internal_deps": seg.get("internal_deps", []),
            "external_deps": seg.get("external_deps", [])[:10],
            "entry_points": seg.get("entry_points", [])[:5],
        })

    # Build file dependency edges (DEPENDS_ON only)
    file_deps = [
        {"source": e["source_id"], "target": e["target_id"]}
        for e in edges
        if e.get("kind") == "depends_on"
    ]

    # Build finding-affects edges
    finding_affects = [
        {"finding": e["source_id"], "target": e["target_id"]}
        for e in edges
        if e.get("kind") == "affects"
    ]

    return {
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "total_segments": len(segments),
        "segments": segment_deps,
        "file_dependencies": file_deps[:200],  # cap for report size
        "finding_affects": finding_affects,
    }


def _render_dependency_graph(
    graph_data: dict,
    findings: list[AuditFinding],
) -> str:
    """Build HTML sections for dependency graph visualizations.

    Produces:
    1. Segment Dependency Graph — interactive SVG showing how segments connect
    2. Module Interconnection Matrix — which segments depend on which
    3. Finding Blast Radius — for each finding, what modules are affected
    4. Import Chain Visualization — file-level dependency flows
    """
    if not graph_data:
        return ""

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    segments = graph_data.get("segments", [])

    if not segments:
        return ""

    sections: list[str] = []
    sections.append('<h2>Dependency Graph</h2>')
    sections.append(
        '<p class="arch-summary">Module relationships and downstream connections '
        'extracted from the code graph. Use these to trace the blast radius of any fix.</p>'
    )

    # ── 1. Segment Dependency Network (SVG) ──────────────────────────
    seg_svg = _render_segment_network_svg(segments)
    if seg_svg:
        sections.append('<h3>Segment Dependency Network</h3>')
        sections.append(seg_svg)

    # ── 2. Module Interconnection Table ──────────────────────────────
    inter_html = _render_interconnection_table(segments)
    if inter_html:
        sections.append('<h3>Module Interconnections</h3>')
        sections.append(
            '<p class="desc">Each row shows a segment and which other segments '
            'it depends on (imports from) and which depend on it (imported by).</p>'
        )
        sections.append(inter_html)

    # ── 3. Finding Blast Radius ──────────────────────────────────────
    blast_html = _render_blast_radius(edges, segments, findings, nodes)
    if blast_html:
        sections.append('<h3>Finding Blast Radius</h3>')
        sections.append(
            '<p class="desc">For each high/critical finding, the downstream '
            'modules that could be affected if the finding\'s code changes.</p>'
        )
        sections.append(blast_html)

    # ── 4. Import Chain Flows ────────────────────────────────────────
    chain_html = _render_import_chains(edges, nodes, segments)
    if chain_html:
        sections.append('<h3>Import Chains</h3>')
        sections.append(
            '<p class="desc">File-level dependency flows showing how '
            'imports propagate across segments.</p>'
        )
        sections.append(chain_html)

    return "\n".join(sections)


def _render_segment_network_svg(segments: list[dict]) -> str:
    """Render a force-directed-style SVG of segment dependencies.

    Uses a simple circular layout with bezier curves for edges.
    """
    import math

    if len(segments) < 2:
        return ""

    # Build segment ID → index map
    seg_ids = [s.get("id", f"seg-{i}") for i, s in enumerate(segments)]
    seg_map = {sid: i for i, sid in enumerate(seg_ids)}
    n = len(segments)

    # Circular layout
    cx, cy = 300, 220
    radius = min(180, 40 * n)
    positions: list[tuple[float, float]] = []
    for i in range(n):
        angle = 2 * math.pi * i / n - math.pi / 2
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        positions.append((x, y))

    svg_width = 600
    svg_height = 440

    # Build edges
    edge_lines = []
    for seg in segments:
        src_id = seg.get("id", "")
        src_idx = seg_map.get(src_id)
        if src_idx is None:
            continue
        for dep_id in seg.get("internal_deps", []):
            dst_idx = seg_map.get(dep_id)
            if dst_idx is None:
                continue
            sx, sy = positions[src_idx]
            dx, dy = positions[dst_idx]
            # Bezier control point toward center
            mx = (sx + dx) / 2 + (cy - (sy + dy) / 2) * 0.2
            my = (sy + dy) / 2 + ((sx + dx) / 2 - cx) * 0.2
            edge_lines.append(
                f'<path d="M{sx:.0f},{sy:.0f} Q{mx:.0f},{my:.0f} {dx:.0f},{dy:.0f}" '
                f'fill="none" stroke="#94a3b8" stroke-width="1.5" '
                f'marker-end="url(#arrowhead)"/>'
            )

    # Build nodes
    node_circles = []
    # Color by finding count
    max_findings = max((len(s.get("findings", [])) for s in segments), default=1) or 1
    for i, seg in enumerate(segments):
        x, y = positions[i]
        label = seg.get("label", seg.get("id", "")[:8])
        finding_count = len(seg.get("findings", []))
        loc = seg.get("loc", 0)
        # Color gradient: blue (0 findings) → red (max findings)
        ratio = finding_count / max_findings if max_findings > 0 else 0
        r = int(99 + 150 * ratio)
        g = int(102 - 50 * ratio)
        b = int(241 - 180 * ratio)
        node_r = max(18, min(35, 18 + loc // 200))

        node_circles.append(
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{node_r}" '
            f'fill="rgb({r},{g},{b})" stroke="#fff" stroke-width="2" '
            f'opacity="0.9"/>'
        )
        # Label
        node_circles.append(
            f'<text x="{x:.0f}" y="{y + node_r + 14:.0f}" '
            f'text-anchor="middle" font-size="11" fill="#334155" '
            f'font-family="monospace">{_esc(label[:16])}</text>'
        )
        # Finding count badge
        if finding_count > 0:
            node_circles.append(
                f'<text x="{x:.0f}" y="{y + 4:.0f}" '
                f'text-anchor="middle" font-size="10" fill="#fff" '
                f'font-weight="600">{finding_count}</text>'
            )

    return f"""<div style="overflow-x:auto;margin-bottom:1rem">
<svg width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}"
     style="background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0">
  <defs>
    <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <polygon points="0 0, 8 3, 0 6" fill="#94a3b8"/>
    </marker>
  </defs>
  {"".join(edge_lines)}
  {"".join(node_circles)}
  <text x="{svg_width - 10}" y="{svg_height - 10}" text-anchor="end"
        font-size="9" fill="#94a3b8">Node size = LOC, color = finding density, arrows = depends_on</text>
</svg>
</div>"""


def _render_interconnection_table(segments: list[dict]) -> str:
    """Render a table showing each segment's upstream/downstream connections."""
    if len(segments) < 2:
        return ""

    # Build reverse dependency map
    seg_label_map = {s.get("id", ""): s.get("label", s.get("id", "")[:12]) for s in segments}
    depended_by: dict[str, list[str]] = {}
    for seg in segments:
        for dep_id in seg.get("internal_deps", []):
            depended_by.setdefault(dep_id, []).append(seg.get("id", ""))

    rows = ""
    for seg in segments:
        sid = seg.get("id", "")
        label = seg.get("label", sid[:12])
        loc = seg.get("loc", 0)
        finding_count = len(seg.get("findings", []))

        # What this segment imports from
        deps_on = [
            f'<span class="dep-tag imports">{_esc(seg_label_map.get(d, d[:10]))}</span>'
            for d in seg.get("internal_deps", [])
        ]
        # What imports this segment
        deps_by = [
            f'<span class="dep-tag imported-by">{_esc(seg_label_map.get(d, d[:10]))}</span>'
            for d in depended_by.get(sid, [])
        ]

        deps_on_html = " ".join(deps_on) if deps_on else '<span class="desc">none</span>'
        deps_by_html = " ".join(deps_by) if deps_by else '<span class="desc">none</span>'

        finding_badge = ""
        if finding_count > 0:
            finding_badge = f' <span class="finding-badge">{finding_count}</span>'

        rows += f"""<tr>
            <td><strong>{_esc(label)}</strong>{finding_badge}<br>
                <span class="desc">{loc:,} LOC &middot; {len(seg.get("files", []))} files</span></td>
            <td>{deps_on_html}</td>
            <td>{deps_by_html}</td>
        </tr>"""

    return f"""<table>
        <thead><tr>
            <th>Segment</th>
            <th>Imports From</th>
            <th>Imported By</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def _render_blast_radius(
    edges: list[dict],
    segments: list[dict],
    findings: list[AuditFinding],
    nodes: list[dict],
) -> str:
    """For high/critical findings, show which segments are in the blast radius."""
    if not findings or not edges:
        return ""

    # Build file → segment map
    file_to_seg: dict[str, str] = {}
    seg_label_map: dict[str, str] = {}
    for seg in segments:
        label = seg.get("label", seg.get("id", "")[:12])
        seg_label_map[seg.get("id", "")] = label
        for fp in seg.get("files", []):
            file_to_seg[fp] = label

    # Build node_id → segment_id map
    node_seg: dict[str, str] = {}
    node_file: dict[str, str] = {}
    for n in nodes:
        nid = n.get("id", "")
        node_seg[nid] = n.get("segment_id", "")
        node_file[nid] = n.get("file_path", "")

    # Build dependency adjacency: file → files that depend on it
    # (reverse DEPENDS_ON edges)
    depends_on_reverse: dict[str, set[str]] = {}
    for e in edges:
        if e.get("kind") == "depends_on":
            target = e["target_id"]  # the file being imported
            source = e["source_id"]  # the file that imports it
            depends_on_reverse.setdefault(target, set()).add(source)

    # AFFECTS edges: finding → node
    finding_affects: dict[str, list[str]] = {}
    for e in edges:
        if e.get("kind") == "affects":
            finding_affects.setdefault(e["source_id"], []).append(e["target_id"])

    # Filter to high/critical findings
    severe = [f for f in findings if f.severity.value in ("critical", "high")]
    if not severe:
        return ""

    rows = ""
    for f in severe[:15]:  # cap at 15
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        direct_files: set[str] = set()

        # From finding locations
        for loc in f.locations:
            if loc.file_path:
                direct_files.add(loc.file_path)

        # From AFFECTS edges
        for nid in finding_affects.get(f.id, []):
            fp = node_file.get(nid, "")
            if fp:
                direct_files.add(fp)

        # Compute downstream: files that import any of the direct files
        downstream_files: set[str] = set()
        for df in direct_files:
            # Match by file node ID pattern
            file_nid = f"file:{df}"
            for dep_file_nid in depends_on_reverse.get(file_nid, set()):
                # dep_file_nid is like "file:src/foo.py"
                dep_path = dep_file_nid.replace("file:", "", 1)
                if dep_path not in direct_files:
                    downstream_files.add(dep_path)

        # Map to segments
        direct_segs = {file_to_seg.get(fp, "") for fp in direct_files} - {""}
        downstream_segs = {file_to_seg.get(fp, "") for fp in downstream_files} - {""}
        downstream_segs -= direct_segs  # only show new segments

        direct_tags = " ".join(
            f'<span class="dep-tag direct">{_esc(s)}</span>' for s in sorted(direct_segs)
        ) or '<span class="desc">-</span>'
        downstream_tags = " ".join(
            f'<span class="dep-tag downstream">{_esc(s)}</span>' for s in sorted(downstream_segs)
        ) or '<span class="desc">none</span>'

        rows += f"""<tr>
            <td><span class="severity {sev}">{sev}</span></td>
            <td><strong>{_esc(f.title[:60])}</strong></td>
            <td>{direct_tags}</td>
            <td>{downstream_tags}</td>
            <td class="loc">{len(downstream_files)}</td>
        </tr>"""

    if not rows:
        return ""

    return f"""<table>
        <thead><tr>
            <th>Sev</th>
            <th>Finding</th>
            <th>Direct Modules</th>
            <th>Downstream Modules</th>
            <th>Files Affected</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def _render_import_chains(
    edges: list[dict],
    nodes: list[dict],
    segments: list[dict],
) -> str:
    """Render cross-segment import chains as flow diagrams."""
    if not edges or not segments:
        return ""

    # Build segment label map and file → segment map
    seg_label_map = {s.get("id", ""): s.get("label", s.get("id", "")[:12]) for s in segments}
    node_seg_map: dict[str, str] = {}
    for n in nodes:
        node_seg_map[n.get("id", "")] = n.get("segment_id", "")

    # Find cross-segment DEPENDS_ON edges
    cross_edges: list[tuple[str, str, str, str]] = []
    for e in edges:
        if e.get("kind") != "depends_on":
            continue
        src_seg = node_seg_map.get(e["source_id"], "")
        dst_seg = node_seg_map.get(e["target_id"], "")
        if src_seg and dst_seg and src_seg != dst_seg:
            src_label = seg_label_map.get(src_seg, src_seg[:10])
            dst_label = seg_label_map.get(dst_seg, dst_seg[:10])
            src_file = e["source_id"].replace("file:", "", 1)
            dst_file = e["target_id"].replace("file:", "", 1)
            cross_edges.append((src_label, dst_label, src_file, dst_file))

    if not cross_edges:
        return ""

    # Group by segment pair and show top files
    pair_files: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for src_lbl, dst_lbl, src_file, dst_file in cross_edges:
        pair_files[(src_lbl, dst_lbl)].append((src_file, dst_file))

    # Sort by number of cross-edges (most connected pairs first)
    sorted_pairs = sorted(pair_files.items(), key=lambda x: -len(x[1]))

    rows = ""
    for (src_lbl, dst_lbl), files in sorted_pairs[:12]:
        file_examples = ""
        for sf, df in files[:3]:
            sf_short = sf.rsplit("/", 1)[-1] if "/" in sf else sf
            df_short = df.rsplit("/", 1)[-1] if "/" in df else df
            file_examples += (
                f'<span class="flow-tag">{_esc(sf_short)} &rarr; {_esc(df_short)}</span> '
            )
        if len(files) > 3:
            file_examples += f'<span class="desc">+{len(files) - 3} more</span>'

        rows += f"""<div class="chain-row">
            <span class="chain-src">{_esc(src_lbl)}</span>
            <span class="chain-arrow">&rarr;</span>
            <span class="chain-dst">{_esc(dst_lbl)}</span>
            <span class="chain-count">{len(files)}</span>
            <div class="chain-files">{file_examples}</div>
        </div>"""

    return f'<div class="chain-list">{rows}</div>'


# ── Analysis Methodology section ────────────────────────────────────────


def _build_pattern_library_data(findings: list[AuditFinding]) -> dict | None:
    """Build pattern library summary for JSON report."""
    try:
        from forge.patterns.loader import PatternLibrary

        library = PatternLibrary.load_default()
    except Exception:
        return None

    if not library:
        return None

    pattern_hits: dict[str, int] = {}
    for f in findings:
        if f.pattern_id:
            pattern_hits[f.pattern_id] = pattern_hits.get(f.pattern_id, 0) + 1

    return {
        "patterns_checked": len(library),
        "pattern_hits": pattern_hits,
        "patterns": [
            {
                "id": p.id,
                "name": p.name,
                "severity": p.severity_default,
                "cwe_ids": p.cwe_ids,
                "hits": pattern_hits.get(p.id, 0),
            }
            for p in library.all()
        ],
    }


def _render_methodology_section(findings: list[AuditFinding]) -> str:
    """Render the Analysis Methodology section showing patterns checked."""
    try:
        from forge.patterns.loader import PatternLibrary

        library = PatternLibrary.load_default()
    except Exception:
        return ""

    if not library:
        return ""

    # Count pattern hits in findings
    pattern_hits: dict[str, int] = {}
    for f in findings:
        if f.pattern_id:
            pattern_hits[f.pattern_id] = pattern_hits.get(f.pattern_id, 0) + 1

    rows = ""
    for p in library.all():
        hits = pattern_hits.get(p.id, 0)
        status_cls = "detected" if hits > 0 else "clear"
        cwes = ", ".join(p.cwe_ids) if p.cwe_ids else "&mdash;"
        rows += f"""
            <tr class="{status_cls}">
                <td>{_esc(p.id)}</td>
                <td>{_esc(p.name)}</td>
                <td><span class="sev-badge {p.severity_default}">{p.severity_default}</span></td>
                <td>{cwes}</td>
                <td>{"<strong>" + str(hits) + "</strong>" if hits else "0"}</td>
            </tr>"""

    return f"""
    <div class="section">
        <h2>Analysis Methodology</h2>
        <p>This scan checked for <strong>{len(library)}</strong> known vulnerability
        patterns from the FORGE Pattern Library, in addition to standard agent-driven
        security, quality, and architecture analysis.</p>
        <p>FORGE performs <strong>100% static analysis + LLM reasoning</strong> &mdash;
        no runtime tests, load tests, or UI tests are executed.</p>
        <table>
            <thead>
                <tr><th>ID</th><th>Pattern</th><th>Severity</th><th>CWEs</th><th>Hits</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""
