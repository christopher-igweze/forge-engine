"""Tests for dependency graph visualization in discovery reports."""

import json
import os

import pytest

from forge.execution.report import (
    _build_graph_report_data,
    _load_graph_data,
    _render_blast_radius,
    _render_dependency_graph,
    _render_import_chains,
    _render_interconnection_table,
    _render_segment_network_svg,
    generate_discovery_report,
)
from forge.schemas import (
    AuditFinding,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
)


# ── Fixtures ─────────────────────────────────────────────────────────


def _make_graph_data(
    num_segments: int = 3,
    files_per_segment: int = 4,
    cross_segment_deps: bool = True,
    with_findings: bool = True,
) -> dict:
    """Build a realistic enriched CodeGraph dict for testing."""
    segments = []
    nodes = []
    edges = []

    seg_ids = [f"seg-{i:04d}" for i in range(num_segments)]
    labels = ["auth", "api", "models", "utils", "ui"][:num_segments]

    for si, sid in enumerate(seg_ids):
        label = labels[si] if si < len(labels) else f"module-{si}"
        files = [f"src/{label}/file_{j}.py" for j in range(files_per_segment)]
        node_ids = []

        for fp in files:
            nid = f"file:{fp}"
            nodes.append({
                "id": nid,
                "kind": "file",
                "name": fp.rsplit("/", 1)[-1],
                "file_path": fp,
                "language": "python",
                "loc": 100 + si * 50,
                "segment_id": sid,
                "metadata": {},
            })
            node_ids.append(nid)

            # Add a function node
            fn_id = f"fn:{fp}:main"
            nodes.append({
                "id": fn_id,
                "kind": "function",
                "name": "main",
                "file_path": fp,
                "line_start": 1,
                "line_end": 20,
                "language": "python",
                "loc": 20,
                "segment_id": sid,
                "metadata": {},
            })
            node_ids.append(fn_id)
            edges.append({
                "source_id": nid,
                "target_id": fn_id,
                "kind": "contains",
                "metadata": {},
            })

        # Internal file dependencies
        for j in range(1, len(files)):
            edges.append({
                "source_id": f"file:{files[j]}",
                "target_id": f"file:{files[j - 1]}",
                "kind": "depends_on",
                "metadata": {},
            })

        findings = []
        if with_findings and si < 2:
            for k in range(2 + si):
                fid = f"F-{sid}-{k}"
                findings.append({
                    "id": fid,
                    "title": f"Finding {k} in {label}",
                    "severity": "high" if k == 0 else "medium",
                    "category": "security",
                })
                # Add AFFECTS edge
                target_nid = f"file:{files[0]}"
                edges.append({
                    "source_id": fid,
                    "target_id": target_nid,
                    "kind": "affects",
                    "metadata": {},
                })
                nodes.append({
                    "id": fid,
                    "kind": "finding",
                    "name": f"Finding {k} in {label}",
                    "file_path": "",
                    "segment_id": sid,
                    "metadata": findings[-1],
                })

        internal_deps = []
        if cross_segment_deps and si > 0:
            internal_deps = [seg_ids[si - 1]]
            # Cross-segment dependency edge
            edges.append({
                "source_id": f"file:{files[0]}",
                "target_id": f"file:src/{labels[si - 1]}/file_0.py",
                "kind": "depends_on",
                "metadata": {},
            })

        segments.append({
            "id": sid,
            "label": label,
            "files": files,
            "node_ids": node_ids,
            "entry_points": [f"{files[0]}:main"],
            "external_deps": ["requests", "pydantic"][:si + 1],
            "internal_deps": internal_deps,
            "loc": files_per_segment * (100 + si * 50),
            "findings": findings,
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "segments": segments,
        "stats": {
            "total_files": num_segments * files_per_segment,
            "total_loc": sum(s["loc"] for s in segments),
        },
        "total_findings": sum(len(s["findings"]) for s in segments),
    }


def _make_findings(graph_data: dict) -> list[AuditFinding]:
    """Build AuditFinding objects that match the graph's findings."""
    findings = []
    for seg in graph_data["segments"]:
        for fd in seg["findings"]:
            findings.append(AuditFinding(
                id=fd["id"],
                title=fd["title"],
                description=f"Description for {fd['title']}",
                category=FindingCategory(fd.get("category", "security")),
                severity=FindingSeverity(fd.get("severity", "medium")),
                locations=[FindingLocation(
                    file_path=seg["files"][0],
                    line_start=1,
                    line_end=10,
                    snippet="# vulnerable code",
                )],
                suggested_fix="Fix it",
            ))
    return findings


# ── _load_graph_data ─────────────────────────────────────────────────


class TestLoadGraphData:
    def test_loads_layer1_graph(self, tmp_path):
        hive_dir = tmp_path / "hive"
        hive_dir.mkdir()
        graph = _make_graph_data(num_segments=2)
        (hive_dir / "layer1_enriched_graph.json").write_text(json.dumps(graph))

        result = _load_graph_data(str(tmp_path))
        assert result is not None
        assert "nodes" in result
        assert len(result["segments"]) == 2

    def test_falls_back_to_layer0(self, tmp_path):
        hive_dir = tmp_path / "hive"
        hive_dir.mkdir()
        graph = _make_graph_data(num_segments=1)
        (hive_dir / "layer0_graph.json").write_text(json.dumps(graph))

        result = _load_graph_data(str(tmp_path))
        assert result is not None

    def test_returns_none_when_no_artifacts(self, tmp_path):
        result = _load_graph_data(str(tmp_path))
        assert result is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        hive_dir = tmp_path / "hive"
        hive_dir.mkdir()
        (hive_dir / "layer1_enriched_graph.json").write_text("not json")

        result = _load_graph_data(str(tmp_path))
        assert result is None

    def test_returns_none_for_missing_nodes_key(self, tmp_path):
        hive_dir = tmp_path / "hive"
        hive_dir.mkdir()
        (hive_dir / "layer1_enriched_graph.json").write_text('{"edges": []}')

        # Falls back to layer0, which doesn't exist
        result = _load_graph_data(str(tmp_path))
        assert result is None


# ── _build_graph_report_data ─────────────────────────────────────────


class TestBuildGraphReportData:
    def test_basic_structure(self):
        graph = _make_graph_data()
        result = _build_graph_report_data(graph)

        assert result["total_segments"] == 3
        assert result["total_nodes"] > 0
        assert result["total_edges"] > 0
        assert len(result["segments"]) == 3

    def test_segment_data(self):
        graph = _make_graph_data()
        result = _build_graph_report_data(graph)

        seg = result["segments"][0]
        assert "id" in seg
        assert "label" in seg
        assert "files" in seg
        assert "loc" in seg
        assert "finding_count" in seg
        assert "internal_deps" in seg

    def test_file_dependencies_extracted(self):
        graph = _make_graph_data(cross_segment_deps=True)
        result = _build_graph_report_data(graph)

        assert len(result["file_dependencies"]) > 0
        dep = result["file_dependencies"][0]
        assert "source" in dep
        assert "target" in dep

    def test_finding_affects_extracted(self):
        graph = _make_graph_data(with_findings=True)
        result = _build_graph_report_data(graph)

        assert len(result["finding_affects"]) > 0

    def test_empty_graph(self):
        result = _build_graph_report_data({"nodes": [], "edges": [], "segments": []})
        assert result["total_nodes"] == 0
        assert result["total_segments"] == 0


# ── _render_segment_network_svg ──────────────────────────────────────


class TestRenderSegmentNetworkSVG:
    def test_renders_svg(self):
        graph = _make_graph_data()
        svg = _render_segment_network_svg(graph["segments"])

        assert "<svg" in svg
        assert "</svg>" in svg
        assert "arrowhead" in svg  # marker definition

    def test_contains_segment_labels(self):
        graph = _make_graph_data()
        svg = _render_segment_network_svg(graph["segments"])

        assert "auth" in svg
        assert "api" in svg
        assert "models" in svg

    def test_single_segment_returns_empty(self):
        graph = _make_graph_data(num_segments=1)
        svg = _render_segment_network_svg(graph["segments"])
        assert svg == ""

    def test_shows_finding_counts(self):
        graph = _make_graph_data(with_findings=True)
        svg = _render_segment_network_svg(graph["segments"])
        # auth has 2 findings, api has 3
        assert ">2<" in svg or ">3<" in svg

    def test_renders_edge_paths(self):
        graph = _make_graph_data(cross_segment_deps=True)
        svg = _render_segment_network_svg(graph["segments"])
        assert "<path" in svg


# ── _render_interconnection_table ────────────────────────────────────


class TestRenderInterconnectionTable:
    def test_renders_table(self):
        graph = _make_graph_data()
        html = _render_interconnection_table(graph["segments"])

        assert "<table>" in html
        assert "Imports From" in html
        assert "Imported By" in html

    def test_shows_dependency_tags(self):
        graph = _make_graph_data(cross_segment_deps=True)
        html = _render_interconnection_table(graph["segments"])

        # api depends on auth, so auth should appear in api's "Imports From"
        assert "auth" in html
        assert 'class="dep-tag imports"' in html

    def test_shows_reverse_dependencies(self):
        graph = _make_graph_data(cross_segment_deps=True)
        html = _render_interconnection_table(graph["segments"])

        # auth is imported by api, so api should appear in auth's "Imported By"
        assert 'class="dep-tag imported-by"' in html

    def test_shows_loc_and_file_count(self):
        graph = _make_graph_data()
        html = _render_interconnection_table(graph["segments"])
        assert "LOC" in html
        assert "files" in html

    def test_single_segment_returns_empty(self):
        graph = _make_graph_data(num_segments=1)
        html = _render_interconnection_table(graph["segments"])
        assert html == ""


# ── _render_blast_radius ─────────────────────────────────────────────


class TestRenderBlastRadius:
    def test_renders_for_high_findings(self):
        graph = _make_graph_data(cross_segment_deps=True, with_findings=True)
        findings = _make_findings(graph)

        html = _render_blast_radius(
            graph["edges"], graph["segments"], findings, graph["nodes"],
        )
        assert "<table>" in html
        assert "Finding" in html
        assert "Direct Modules" in html
        assert "Downstream Modules" in html

    def test_shows_direct_module_tags(self):
        graph = _make_graph_data(with_findings=True)
        findings = _make_findings(graph)

        html = _render_blast_radius(
            graph["edges"], graph["segments"], findings, graph["nodes"],
        )
        assert 'class="dep-tag direct"' in html

    def test_empty_findings_returns_empty(self):
        graph = _make_graph_data()
        html = _render_blast_radius(
            graph["edges"], graph["segments"], [], graph["nodes"],
        )
        assert html == ""

    def test_no_high_findings_returns_empty(self):
        graph = _make_graph_data(with_findings=False)
        low_findings = [AuditFinding(
            id="F-low",
            title="Low issue",
            description="Not important",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.LOW,
            locations=[],
            suggested_fix="",
        )]
        html = _render_blast_radius(
            graph["edges"], graph["segments"], low_findings, graph["nodes"],
        )
        assert html == ""


# ── _render_import_chains ────────────────────────────────────────────


class TestRenderImportChains:
    def test_renders_cross_segment_chains(self):
        graph = _make_graph_data(cross_segment_deps=True)
        html = _render_import_chains(
            graph["edges"], graph["nodes"], graph["segments"],
        )
        assert "chain-row" in html
        assert "chain-src" in html
        assert "chain-dst" in html

    def test_no_cross_deps_returns_empty(self):
        graph = _make_graph_data(cross_segment_deps=False, num_segments=1)
        html = _render_import_chains(
            graph["edges"], graph["nodes"], graph["segments"],
        )
        assert html == ""

    def test_shows_file_examples(self):
        graph = _make_graph_data(cross_segment_deps=True)
        html = _render_import_chains(
            graph["edges"], graph["nodes"], graph["segments"],
        )
        assert "flow-tag" in html


# ── _render_dependency_graph (integration) ───────────────────────────


class TestRenderDependencyGraph:
    def test_renders_all_sections(self):
        graph = _make_graph_data(cross_segment_deps=True, with_findings=True)
        findings = _make_findings(graph)

        html = _render_dependency_graph(graph, findings)

        assert "Dependency Graph" in html
        assert "Segment Dependency Network" in html
        assert "Module Interconnections" in html
        assert "Finding Blast Radius" in html
        assert "Import Chains" in html

    def test_empty_graph_returns_empty(self):
        html = _render_dependency_graph({}, [])
        assert html == ""

    def test_no_segments_returns_empty(self):
        html = _render_dependency_graph(
            {"nodes": [], "edges": [], "segments": []}, [],
        )
        assert html == ""


# ── generate_discovery_report with graph ─────────────────────────────


class TestDiscoveryReportWithGraph:
    def test_json_includes_dependency_graph(self, tmp_path):
        graph = _make_graph_data()
        findings = _make_findings(graph)

        paths = generate_discovery_report(
            findings=findings,
            plan=None,
            artifacts_dir=str(tmp_path),
            run_id="test-graph",
            graph_data=graph,
        )

        with open(paths["json"]) as f:
            data = json.load(f)

        assert "dependency_graph" in data
        assert data["dependency_graph"] is not None
        assert data["dependency_graph"]["total_segments"] == 3

    def test_html_includes_graph_sections(self, tmp_path):
        graph = _make_graph_data(cross_segment_deps=True, with_findings=True)
        findings = _make_findings(graph)

        paths = generate_discovery_report(
            findings=findings,
            plan=None,
            artifacts_dir=str(tmp_path),
            run_id="test-graph",
            graph_data=graph,
        )

        with open(paths["html"]) as f:
            html = f.read()

        assert "Dependency Graph" in html
        assert "<svg" in html
        assert "Module Interconnections" in html

    def test_no_graph_data_gracefully_skipped(self, tmp_path):
        findings = [AuditFinding(
            id="F-001",
            title="Test finding",
            description="Test",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[],
            suggested_fix="Fix it",
        )]

        paths = generate_discovery_report(
            findings=findings,
            plan=None,
            artifacts_dir=str(tmp_path),
            run_id="test-no-graph",
        )

        with open(paths["json"]) as f:
            data = json.load(f)
        assert data["dependency_graph"] is None

        with open(paths["html"]) as f:
            html = f.read()
        assert "Dependency Graph" not in html

    def test_auto_loads_from_artifacts(self, tmp_path):
        """Graph is auto-loaded from hive artifacts when not passed explicitly."""
        graph = _make_graph_data(num_segments=2)

        # Save graph to hive artifacts location
        hive_dir = tmp_path / "hive"
        hive_dir.mkdir()
        (hive_dir / "layer1_enriched_graph.json").write_text(json.dumps(graph))

        findings = _make_findings(graph)

        paths = generate_discovery_report(
            findings=findings,
            plan=None,
            artifacts_dir=str(tmp_path),
            run_id="test-autoload",
        )

        with open(paths["json"]) as f:
            data = json.load(f)

        assert data["dependency_graph"] is not None
        assert data["dependency_graph"]["total_segments"] == 2
