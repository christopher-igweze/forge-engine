"""Tests for Hive Discovery Code Graph models.

Verifies CodeGraph CRUD, segment queries, neighbor lookups,
finding creation, enriched graph output, and SegmentContext.
"""

from __future__ import annotations

import pytest

from forge.graph.models import (
    CodeGraph,
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    Segment,
    SegmentContext,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_file_node(
    path: str,
    *,
    segment_id: str = "",
    loc: int = 10,
    language: str = "python",
) -> GraphNode:
    """Create a FILE node with a deterministic ID."""
    return GraphNode(
        id=f"file:{path}",
        kind=NodeKind.FILE,
        name=path.rsplit("/", 1)[-1],
        file_path=path,
        language=language,
        loc=loc,
        segment_id=segment_id,
    )


def _make_fn_node(
    path: str,
    name: str,
    *,
    segment_id: str = "",
    line_start: int = 1,
    line_end: int = 10,
) -> GraphNode:
    """Create a FUNCTION node with a deterministic ID."""
    return GraphNode(
        id=f"fn:{path}:{name}",
        kind=NodeKind.FUNCTION,
        name=name,
        file_path=path,
        language="python",
        line_start=line_start,
        line_end=line_end,
        segment_id=segment_id,
    )


def _make_class_node(
    path: str,
    name: str,
    *,
    segment_id: str = "",
) -> GraphNode:
    return GraphNode(
        id=f"cls:{path}:{name}",
        kind=NodeKind.CLASS,
        name=name,
        file_path=path,
        language="python",
        segment_id=segment_id,
    )


def _make_import_node(path: str, module: str) -> GraphNode:
    return GraphNode(
        id=f"import:{path}:{module}",
        kind=NodeKind.IMPORT,
        name=module,
        file_path=path,
        language="python",
        metadata={"module": module},
    )


def _build_two_segment_graph() -> CodeGraph:
    """Build a graph with two segments that depend on each other.

    Segment A: file_a.py (function greet)
    Segment B: file_b.py (function run, imports file_a)
    """
    graph = CodeGraph()

    # Files
    fa = _make_file_node("file_a.py", segment_id="seg-a")
    fb = _make_file_node("file_b.py", segment_id="seg-b")

    # Functions
    fn_greet = _make_fn_node("file_a.py", "greet", segment_id="seg-a")
    fn_run = _make_fn_node("file_b.py", "run", segment_id="seg-b")

    for n in [fa, fb, fn_greet, fn_run]:
        graph.add_node(n)

    # Edges: file contains function
    graph.add_edge(GraphEdge(source_id=fa.id, target_id=fn_greet.id, kind=EdgeKind.CONTAINS))
    graph.add_edge(GraphEdge(source_id=fb.id, target_id=fn_run.id, kind=EdgeKind.CONTAINS))

    # Cross-segment dependency: B depends on A
    graph.add_edge(GraphEdge(source_id=fb.id, target_id=fa.id, kind=EdgeKind.DEPENDS_ON))

    # Segments
    seg_a = Segment(
        id="seg-a",
        label="a",
        files=["file_a.py"],
        node_ids=[fa.id, fn_greet.id],
        internal_deps=[],
    )
    seg_b = Segment(
        id="seg-b",
        label="b",
        files=["file_b.py"],
        node_ids=[fb.id, fn_run.id],
        internal_deps=["seg-a"],
    )
    graph.segments = [seg_a, seg_b]
    return graph


# ── GraphNode Tests ──────────────────────────────────────────────────


class TestGraphNode:
    """Test GraphNode creation for all node kinds."""

    def test_auto_generated_id(self):
        node = GraphNode(kind=NodeKind.FILE, name="test.py")
        assert node.id.startswith("n-")
        assert len(node.id) == 10  # "n-" + 8 hex chars

    def test_unique_auto_ids(self):
        ids = {GraphNode(kind=NodeKind.FILE, name="f").id for _ in range(50)}
        assert len(ids) == 50

    def test_explicit_id_is_preserved(self):
        node = GraphNode(id="custom-id", kind=NodeKind.FILE, name="test.py")
        assert node.id == "custom-id"

    @pytest.mark.parametrize(
        "kind",
        [
            NodeKind.FILE,
            NodeKind.FUNCTION,
            NodeKind.CLASS,
            NodeKind.IMPORT,
            NodeKind.FINDING,
            NodeKind.OBSERVATION,
            NodeKind.MODULE,
            NodeKind.VARIABLE,
        ],
    )
    def test_all_node_kinds_create_successfully(self, kind: NodeKind):
        node = GraphNode(kind=kind, name="test")
        assert node.kind == kind

    def test_file_node_defaults(self):
        node = GraphNode(kind=NodeKind.FILE, name="app.py")
        assert node.file_path == ""
        assert node.line_start is None
        assert node.line_end is None
        assert node.language == ""
        assert node.loc == 0
        assert node.metadata == {}
        assert node.segment_id == ""

    def test_function_node_with_positions(self):
        node = GraphNode(
            kind=NodeKind.FUNCTION,
            name="do_stuff",
            file_path="src/app.py",
            line_start=10,
            line_end=25,
            language="python",
            loc=15,
        )
        assert node.line_start == 10
        assert node.line_end == 25
        assert node.loc == 15

    def test_metadata_stores_arbitrary_data(self):
        node = GraphNode(
            kind=NodeKind.IMPORT,
            name="os",
            metadata={"raw": "import os", "module": "os", "stdlib": True},
        )
        assert node.metadata["stdlib"] is True
        assert node.metadata["module"] == "os"

    def test_finding_node(self):
        node = GraphNode(
            kind=NodeKind.FINDING,
            name="SQL Injection",
            metadata={"severity": "high", "category": "security"},
            segment_id="seg-0",
        )
        assert node.kind == NodeKind.FINDING
        assert node.segment_id == "seg-0"

    def test_observation_node(self):
        node = GraphNode(
            kind=NodeKind.OBSERVATION,
            name="High coupling between modules",
            metadata={"source": "architecture_reviewer"},
        )
        assert node.kind == NodeKind.OBSERVATION


# ── GraphEdge Tests ──────────────────────────────────────────────────


class TestGraphEdge:
    """Test GraphEdge creation for all edge kinds."""

    @pytest.mark.parametrize(
        "kind",
        [
            EdgeKind.CALLS,
            EdgeKind.IMPORTS,
            EdgeKind.INHERITS,
            EdgeKind.REFERENCES,
            EdgeKind.CONTAINS,
            EdgeKind.DEPENDS_ON,
            EdgeKind.AFFECTS,
            EdgeKind.RELATED_TO,
        ],
    )
    def test_all_edge_kinds(self, kind: EdgeKind):
        edge = GraphEdge(source_id="a", target_id="b", kind=kind)
        assert edge.kind == kind
        assert edge.source_id == "a"
        assert edge.target_id == "b"

    def test_edge_metadata_default_empty(self):
        edge = GraphEdge(source_id="a", target_id="b", kind=EdgeKind.CALLS)
        assert edge.metadata == {}

    def test_edge_with_metadata(self):
        edge = GraphEdge(
            source_id="a",
            target_id="b",
            kind=EdgeKind.CALLS,
            metadata={"confidence": 0.9, "inferred": True},
        )
        assert edge.metadata["confidence"] == 0.9


# ── Segment Tests ────────────────────────────────────────────────────


class TestSegment:
    """Test Segment creation with files, node_ids, deps."""

    def test_auto_generated_id(self):
        seg = Segment()
        assert seg.id.startswith("seg-")
        assert len(seg.id) == 12  # "seg-" + 8 hex chars

    def test_explicit_id(self):
        seg = Segment(id="seg-auth-0")
        assert seg.id == "seg-auth-0"

    def test_segment_with_files_and_nodes(self):
        seg = Segment(
            id="seg-core-0",
            label="core",
            files=["src/main.py", "src/utils.py"],
            node_ids=["file:src/main.py", "file:src/utils.py", "fn:src/main.py:main"],
            loc=250,
        )
        assert len(seg.files) == 2
        assert len(seg.node_ids) == 3
        assert seg.loc == 250
        assert seg.label == "core"

    def test_segment_with_deps(self):
        seg = Segment(
            id="seg-api-0",
            entry_points=["api/routes.py:handle_request"],
            external_deps=["flask", "sqlalchemy"],
            internal_deps=["seg-core-0", "seg-db-0"],
        )
        assert seg.entry_points == ["api/routes.py:handle_request"]
        assert "flask" in seg.external_deps
        assert "seg-core-0" in seg.internal_deps

    def test_segment_defaults_are_empty(self):
        seg = Segment()
        assert seg.files == []
        assert seg.node_ids == []
        assert seg.entry_points == []
        assert seg.external_deps == []
        assert seg.internal_deps == []
        assert seg.findings == []
        assert seg.loc == 0

    def test_segment_findings_mutable(self):
        seg = Segment(id="seg-test")
        seg.findings.append({"id": "F-001", "title": "Bug"})
        assert len(seg.findings) == 1
        assert seg.findings[0]["title"] == "Bug"


# ── CodeGraph CRUD Tests ─────────────────────────────────────────────


class TestCodeGraphCRUD:
    """Test basic add_node / add_edge / get_segment operations."""

    def test_add_node_returns_id(self):
        graph = CodeGraph()
        node = GraphNode(id="n-abc", kind=NodeKind.FILE, name="test.py")
        returned_id = graph.add_node(node)
        assert returned_id == "n-abc"
        assert "n-abc" in graph.nodes

    def test_add_multiple_nodes(self):
        graph = CodeGraph()
        for i in range(5):
            graph.add_node(GraphNode(id=f"n-{i}", kind=NodeKind.FILE, name=f"f{i}.py"))
        assert len(graph.nodes) == 5

    def test_add_node_overwrites_same_id(self):
        graph = CodeGraph()
        graph.add_node(GraphNode(id="n-dup", kind=NodeKind.FILE, name="old.py"))
        graph.add_node(GraphNode(id="n-dup", kind=NodeKind.FILE, name="new.py"))
        assert graph.nodes["n-dup"].name == "new.py"
        assert len(graph.nodes) == 1

    def test_add_edge(self):
        graph = CodeGraph()
        edge = GraphEdge(source_id="a", target_id="b", kind=EdgeKind.CONTAINS)
        graph.add_edge(edge)
        assert len(graph.edges) == 1
        assert graph.edges[0].source_id == "a"

    def test_add_multiple_edges(self):
        graph = CodeGraph()
        for i in range(3):
            graph.add_edge(GraphEdge(source_id=f"s{i}", target_id=f"t{i}", kind=EdgeKind.CALLS))
        assert len(graph.edges) == 3

    def test_get_segment_found(self):
        graph = CodeGraph()
        graph.segments = [Segment(id="seg-a"), Segment(id="seg-b")]
        result = graph.get_segment("seg-a")
        assert result is not None
        assert result.id == "seg-a"

    def test_get_segment_not_found(self):
        graph = CodeGraph()
        graph.segments = [Segment(id="seg-a")]
        result = graph.get_segment("seg-missing")
        assert result is None

    def test_empty_graph_defaults(self):
        graph = CodeGraph()
        assert graph.nodes == {}
        assert graph.edges == []
        assert graph.segments == []
        assert graph.stats == {}


# ── CodeGraph.query_segment Tests ────────────────────────────────────


class TestQuerySegment:
    """Test query_segment returns correct nodes/edges for a segment."""

    def test_returns_segment_nodes(self):
        graph = _build_two_segment_graph()
        ctx = graph.query_segment("seg-a")
        assert ctx.segment.id == "seg-a"
        node_ids = {n.id for n in ctx.nodes}
        assert "file:file_a.py" in node_ids
        assert "fn:file_a.py:greet" in node_ids
        # Should NOT include nodes from seg-b
        assert "file:file_b.py" not in node_ids

    def test_returns_segment_edges(self):
        graph = _build_two_segment_graph()
        ctx = graph.query_segment("seg-a")
        # Segment A has: CONTAINS(file_a -> greet) and DEPENDS_ON(file_b -> file_a)
        # The DEPENDS_ON edge involves file_a (target), so it should be included
        edge_kinds = {e.kind for e in ctx.edges}
        assert EdgeKind.CONTAINS in edge_kinds

    def test_cross_segment_edges_included(self):
        """Edges where one end is in the segment should appear."""
        graph = _build_two_segment_graph()
        ctx = graph.query_segment("seg-a")
        # The DEPENDS_ON edge from seg-b -> seg-a involves file_a (a node in seg-a)
        depends_edges = [e for e in ctx.edges if e.kind == EdgeKind.DEPENDS_ON]
        assert len(depends_edges) == 1
        assert depends_edges[0].target_id == "file:file_a.py"

    def test_missing_segment_returns_empty_context(self):
        graph = _build_two_segment_graph()
        ctx = graph.query_segment("seg-nonexistent")
        assert ctx.segment.id == "seg-nonexistent"
        assert ctx.nodes == []
        assert ctx.edges == []

    def test_segment_with_stale_node_ids_handled(self):
        """If a node ID in segment.node_ids doesn't exist in graph.nodes, skip it."""
        graph = CodeGraph()
        seg = Segment(id="seg-stale", node_ids=["n-gone", "n-exists"])
        graph.segments = [seg]
        graph.add_node(GraphNode(id="n-exists", kind=NodeKind.FILE, name="ok.py"))
        ctx = graph.query_segment("seg-stale")
        assert len(ctx.nodes) == 1
        assert ctx.nodes[0].id == "n-exists"


# ── CodeGraph.query_neighbors Tests ─────────────────────────────────


class TestQueryNeighbors:
    """Test query_neighbors returns findings from neighboring segments."""

    def test_returns_findings_from_deps(self):
        graph = _build_two_segment_graph()
        # Add a finding to seg-a
        seg_a = graph.get_segment("seg-a")
        seg_a.findings.append({"id": "F-001", "title": "SQL Injection"})

        # seg-b depends on seg-a, so neighbors of seg-b should include seg-a findings
        findings = graph.query_neighbors("seg-b")
        assert len(findings) == 1
        assert findings[0]["id"] == "F-001"

    def test_returns_findings_from_reverse_deps(self):
        """Segments that depend on US are also neighbors."""
        graph = _build_two_segment_graph()
        # Add a finding to seg-b (which depends on seg-a)
        seg_b = graph.get_segment("seg-b")
        seg_b.findings.append({"id": "F-002", "title": "XSS"})

        # seg-a is depended on by seg-b, so neighbors of seg-a include seg-b
        findings = graph.query_neighbors("seg-a")
        assert len(findings) == 1
        assert findings[0]["id"] == "F-002"

    def test_no_neighbors_returns_empty(self):
        graph = CodeGraph()
        seg = Segment(id="seg-isolated", internal_deps=[])
        graph.segments = [seg]
        findings = graph.query_neighbors("seg-isolated")
        assert findings == []

    def test_missing_segment_returns_empty(self):
        graph = _build_two_segment_graph()
        findings = graph.query_neighbors("seg-nonexistent")
        assert findings == []

    def test_multiple_neighbor_findings_aggregated(self):
        """All findings from all neighboring segments are collected."""
        graph = CodeGraph()
        seg_a = Segment(id="seg-a", internal_deps=[], findings=[{"id": "F-a1"}, {"id": "F-a2"}])
        seg_b = Segment(id="seg-b", internal_deps=["seg-a"], findings=[{"id": "F-b1"}])
        seg_c = Segment(id="seg-c", internal_deps=["seg-a"], findings=[])
        graph.segments = [seg_a, seg_b, seg_c]

        # Neighbors of seg-a: seg-b (depends on seg-a) and seg-c (depends on seg-a)
        findings = graph.query_neighbors("seg-a")
        finding_ids = {f["id"] for f in findings}
        assert "F-b1" in finding_ids
        # seg-c has no findings, but the lookup should still work
        assert len(findings) == 1

    def test_does_not_include_own_findings(self):
        """query_neighbors should NOT include the target segment's own findings."""
        graph = _build_two_segment_graph()
        seg_a = graph.get_segment("seg-a")
        seg_a.findings.append({"id": "F-own"})

        seg_b = graph.get_segment("seg-b")
        seg_b.findings.append({"id": "F-neighbor"})

        # Neighbors of seg-a include seg-b, but NOT seg-a itself
        findings = graph.query_neighbors("seg-a")
        finding_ids = {f["id"] for f in findings}
        assert "F-neighbor" in finding_ids
        assert "F-own" not in finding_ids


# ── CodeGraph.add_finding Tests ──────────────────────────────────────


class TestAddFinding:
    """Test add_finding writes finding to segment and creates AFFECTS edges."""

    def test_finding_added_to_segment(self):
        graph = _build_two_segment_graph()
        finding = {"id": "F-100", "title": "Missing auth", "severity": "critical"}
        graph.add_finding(finding, segment_id="seg-a")

        seg = graph.get_segment("seg-a")
        assert len(seg.findings) == 1
        assert seg.findings[0]["id"] == "F-100"

    def test_finding_creates_node_and_affects_edges(self):
        graph = _build_two_segment_graph()
        finding = {"id": "F-200", "title": "XSS"}
        affected = ["file:file_a.py", "fn:file_a.py:greet"]

        graph.add_finding(finding, segment_id="seg-a", affected_node_ids=affected)

        # A FINDING node should be created
        assert "F-200" in graph.nodes
        finding_node = graph.nodes["F-200"]
        assert finding_node.kind == NodeKind.FINDING
        assert finding_node.name == "XSS"
        assert finding_node.segment_id == "seg-a"

        # Two AFFECTS edges should be created
        affects_edges = [e for e in graph.edges if e.kind == EdgeKind.AFFECTS]
        assert len(affects_edges) == 2
        targets = {e.target_id for e in affects_edges}
        assert "file:file_a.py" in targets
        assert "fn:file_a.py:greet" in targets

    def test_finding_without_affected_nodes_no_edges(self):
        graph = _build_two_segment_graph()
        finding = {"id": "F-300", "title": "Code smell"}
        graph.add_finding(finding, segment_id="seg-a")

        # No new AFFECTS edges
        affects_edges = [e for e in graph.edges if e.kind == EdgeKind.AFFECTS]
        assert len(affects_edges) == 0

        # No finding node either (only created when affected_node_ids is provided)
        assert "F-300" not in graph.nodes

    def test_finding_on_nonexistent_segment_still_creates_edges(self):
        """If segment doesn't exist, findings list can't be written,
        but nodes/edges are still created."""
        graph = CodeGraph()
        graph.add_node(GraphNode(id="n-target", kind=NodeKind.FILE, name="test.py"))

        finding = {"id": "F-orphan", "title": "Orphan finding"}
        graph.add_finding(finding, segment_id="seg-ghost", affected_node_ids=["n-target"])

        # The finding node and edge should still exist
        assert "F-orphan" in graph.nodes
        affects_edges = [e for e in graph.edges if e.kind == EdgeKind.AFFECTS]
        assert len(affects_edges) == 1

    def test_finding_uses_auto_id_if_no_id_in_dict(self):
        graph = _build_two_segment_graph()
        finding = {"title": "No explicit ID"}
        graph.add_finding(finding, segment_id="seg-a", affected_node_ids=["file:file_a.py"])

        # Should have a finding node with auto-generated ID
        finding_nodes = [
            n for n in graph.nodes.values() if n.kind == NodeKind.FINDING
        ]
        assert len(finding_nodes) == 1
        assert finding_nodes[0].id.startswith("finding-")

    def test_finding_metadata_stored_on_node(self):
        graph = _build_two_segment_graph()
        finding = {"id": "F-meta", "title": "Test", "severity": "high", "extra": "data"}
        graph.add_finding(finding, segment_id="seg-a", affected_node_ids=["file:file_a.py"])

        node = graph.nodes["F-meta"]
        assert node.metadata["severity"] == "high"
        assert node.metadata["extra"] == "data"


# ── CodeGraph.get_enriched_graph Tests ───────────────────────────────


class TestGetEnrichedGraph:
    """Test get_enriched_graph returns full graph as dict with correct structure."""

    def test_enriched_graph_structure(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()

        assert "nodes" in result
        assert "edges" in result
        assert "segments" in result
        assert "stats" in result
        assert "total_findings" in result

    def test_enriched_graph_node_count(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        assert len(result["nodes"]) == 4  # 2 files + 2 functions

    def test_enriched_graph_edge_count(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        assert len(result["edges"]) == 3  # 2 CONTAINS + 1 DEPENDS_ON

    def test_enriched_graph_segment_count(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        assert len(result["segments"]) == 2

    def test_total_findings_sum(self):
        graph = _build_two_segment_graph()
        seg_a = graph.get_segment("seg-a")
        seg_a.findings.append({"id": "F-1"})
        seg_a.findings.append({"id": "F-2"})

        seg_b = graph.get_segment("seg-b")
        seg_b.findings.append({"id": "F-3"})

        result = graph.get_enriched_graph()
        assert result["total_findings"] == 3

    def test_total_findings_zero_when_no_findings(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        assert result["total_findings"] == 0

    def test_enriched_graph_nodes_are_dicts(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        for node_dict in result["nodes"]:
            assert isinstance(node_dict, dict)
            assert "id" in node_dict
            assert "kind" in node_dict
            assert "name" in node_dict

    def test_enriched_graph_edges_are_dicts(self):
        graph = _build_two_segment_graph()
        result = graph.get_enriched_graph()
        for edge_dict in result["edges"]:
            assert isinstance(edge_dict, dict)
            assert "source_id" in edge_dict
            assert "target_id" in edge_dict
            assert "kind" in edge_dict

    def test_enriched_graph_includes_stats(self):
        graph = _build_two_segment_graph()
        graph.stats = {"total_files": 2, "total_loc": 100}
        result = graph.get_enriched_graph()
        assert result["stats"]["total_files"] == 2

    def test_empty_graph_enriched(self):
        graph = CodeGraph()
        result = graph.get_enriched_graph()
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["segments"] == []
        assert result["total_findings"] == 0


# ── CodeGraph.file_node_ids Tests ────────────────────────────────────


class TestFileNodeIds:
    """Test file_node_ids maps file paths to their node IDs."""

    def test_returns_file_path_to_id_map(self):
        graph = _build_two_segment_graph()
        mapping = graph.file_node_ids()
        assert mapping["file_a.py"] == "file:file_a.py"
        assert mapping["file_b.py"] == "file:file_b.py"

    def test_only_includes_file_nodes(self):
        graph = _build_two_segment_graph()
        mapping = graph.file_node_ids()
        # Should have exactly 2 entries (only FILE nodes)
        assert len(mapping) == 2
        # Function nodes should NOT appear
        assert "greet" not in mapping.values()

    def test_excludes_file_nodes_with_empty_path(self):
        graph = CodeGraph()
        node = GraphNode(id="n-empty", kind=NodeKind.FILE, name="nopath", file_path="")
        graph.add_node(node)
        mapping = graph.file_node_ids()
        assert len(mapping) == 0

    def test_empty_graph_returns_empty_dict(self):
        graph = CodeGraph()
        assert graph.file_node_ids() == {}


# ── SegmentContext Tests ─────────────────────────────────────────────


class TestSegmentContext:
    """Test SegmentContext model contains all expected fields."""

    def test_segment_context_from_query(self):
        graph = _build_two_segment_graph()
        ctx = graph.query_segment("seg-a")

        assert isinstance(ctx, SegmentContext)
        assert ctx.segment.id == "seg-a"
        assert len(ctx.nodes) == 2  # file + function
        assert len(ctx.edges) >= 1  # at least the CONTAINS edge

    def test_segment_context_has_file_contents(self):
        ctx = SegmentContext(
            segment=Segment(id="seg-test"),
            file_contents={"src/app.py": "print('hello')"},
        )
        assert "src/app.py" in ctx.file_contents
        assert ctx.file_contents["src/app.py"] == "print('hello')"

    def test_segment_context_has_neighbor_findings(self):
        ctx = SegmentContext(
            segment=Segment(id="seg-test"),
            neighbor_findings=[{"id": "F-neighbor", "title": "From neighbor"}],
        )
        assert len(ctx.neighbor_findings) == 1

    def test_segment_context_defaults_empty(self):
        ctx = SegmentContext(segment=Segment(id="seg-empty"))
        assert ctx.nodes == []
        assert ctx.edges == []
        assert ctx.file_contents == {}
        assert ctx.neighbor_findings == []

    def test_segment_context_full_assembly(self):
        """Simulate full SegmentContext that would be passed to a swarm worker."""
        seg = Segment(
            id="seg-api-0",
            label="api",
            files=["api/routes.py", "api/auth.py"],
            node_ids=["file:api/routes.py", "file:api/auth.py", "fn:api/routes.py:handle"],
            entry_points=["api/routes.py:handle"],
            external_deps=["flask"],
            internal_deps=["seg-core-0"],
        )
        nodes = [
            _make_file_node("api/routes.py"),
            _make_file_node("api/auth.py"),
            _make_fn_node("api/routes.py", "handle"),
        ]
        edges = [
            GraphEdge(source_id="file:api/routes.py", target_id="fn:api/routes.py:handle", kind=EdgeKind.CONTAINS),
            GraphEdge(source_id="file:api/routes.py", target_id="file:api/auth.py", kind=EdgeKind.DEPENDS_ON),
        ]
        ctx = SegmentContext(
            segment=seg,
            nodes=nodes,
            edges=edges,
            file_contents={
                "api/routes.py": "from .auth import verify\ndef handle(): pass",
                "api/auth.py": "def verify(): pass",
            },
            neighbor_findings=[{"id": "F-core-01", "title": "Core issue"}],
        )

        assert ctx.segment.label == "api"
        assert len(ctx.nodes) == 3
        assert len(ctx.edges) == 2
        assert len(ctx.file_contents) == 2
        assert len(ctx.neighbor_findings) == 1


# ── Enum Value Tests ─────────────────────────────────────────────────


class TestEnumValues:
    """Verify enum string values match expected constants."""

    def test_node_kind_values(self):
        assert NodeKind.MODULE.value == "module"
        assert NodeKind.FILE.value == "file"
        assert NodeKind.CLASS.value == "class"
        assert NodeKind.FUNCTION.value == "function"
        assert NodeKind.IMPORT.value == "import"
        assert NodeKind.VARIABLE.value == "variable"
        assert NodeKind.FINDING.value == "finding"
        assert NodeKind.OBSERVATION.value == "observation"

    def test_edge_kind_values(self):
        assert EdgeKind.CALLS.value == "calls"
        assert EdgeKind.IMPORTS.value == "imports"
        assert EdgeKind.INHERITS.value == "inherits"
        assert EdgeKind.REFERENCES.value == "references"
        assert EdgeKind.CONTAINS.value == "contains"
        assert EdgeKind.DEPENDS_ON.value == "depends_on"
        assert EdgeKind.AFFECTS.value == "affects"
        assert EdgeKind.RELATED_TO.value == "related_to"

    def test_node_kind_is_str_enum(self):
        """NodeKind inherits from str, so values can be compared as strings."""
        assert NodeKind.FILE == "file"
        assert NodeKind.FILE.value == "file"

    def test_edge_kind_is_str_enum(self):
        assert EdgeKind.CALLS == "calls"
        assert EdgeKind.CALLS.value == "calls"
