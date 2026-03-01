"""Dependency graph and methodology rendering for FORGE reports.

Contains all functions for loading, processing, and rendering dependency graph
visualizations and the analysis methodology section.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

from forge.execution.report_helpers import _esc
from forge.schemas import AuditFinding

logger = logging.getLogger(__name__)


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
