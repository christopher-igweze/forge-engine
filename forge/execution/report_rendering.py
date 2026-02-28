"""HTML rendering functions for FORGE reports.

Contains the large HTML template rendering functions for discovery reports
and production readiness reports.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from forge.execution.report_helpers import _esc, _score_color, _score_label
from forge.execution.report_dependency_graph import (
    _render_dependency_graph,
    _render_methodology_section,
)
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    ProductionReadinessReport,
    RemediationPlan,
)


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
