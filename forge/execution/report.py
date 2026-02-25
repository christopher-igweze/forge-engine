"""Production Readiness Report generation.

Generates both JSON and HTML reports from the ProductionReadinessReport.
The HTML report is designed to be printed to PDF via the browser or
converted with weasyprint if available.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from forge.schemas import ProductionReadinessReport

logger = logging.getLogger(__name__)


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
