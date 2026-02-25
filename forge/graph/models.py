"""Data models for the Code Knowledge Graph.

The CodeGraph is the shared memory bus for the Hive Discovery architecture.
It stores structural information from AST parsing (Layer 0) and is enriched
with findings from swarm workers (Layer 1). The synthesis agent (Layer 2)
reads the full enriched graph to produce final outputs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class NodeKind(str, Enum):
    """Types of nodes in the code graph."""

    MODULE = "module"
    FILE = "file"
    CLASS = "class"
    FUNCTION = "function"
    IMPORT = "import"
    VARIABLE = "variable"
    FINDING = "finding"
    OBSERVATION = "observation"


class EdgeKind(str, Enum):
    """Types of edges in the code graph."""

    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    REFERENCES = "references"
    CONTAINS = "contains"  # file contains function/class
    DEPENDS_ON = "depends_on"  # module depends on module
    AFFECTS = "affects"  # finding affects node
    RELATED_TO = "related_to"  # cross-segment relationship


class GraphNode(BaseModel):
    """A node in the code graph."""

    id: str = Field(default_factory=lambda: f"n-{uuid4().hex[:8]}")
    kind: NodeKind
    name: str
    file_path: str = ""
    line_start: int | None = None
    line_end: int | None = None
    language: str = ""
    loc: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    segment_id: str = ""  # which segment this node belongs to


class GraphEdge(BaseModel):
    """An edge in the code graph."""

    source_id: str
    target_id: str
    kind: EdgeKind
    metadata: dict[str, Any] = Field(default_factory=dict)


class Segment(BaseModel):
    """A cluster of tightly-coupled files for parallel analysis.

    Community detection groups files into segments based on their
    import/call graph connectivity. Each segment gets its own set
    of swarm workers in Layer 1.
    """

    id: str = Field(default_factory=lambda: f"seg-{uuid4().hex[:8]}")
    label: str = ""
    files: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    external_deps: list[str] = Field(default_factory=list)
    internal_deps: list[str] = Field(default_factory=list)  # other segment IDs
    loc: int = 0

    # Enriched during Layer 1
    findings: list[dict] = Field(default_factory=list)


class SegmentContext(BaseModel):
    """Context provided to a swarm worker for its segment."""

    segment: Segment
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    file_contents: dict[str, str] = Field(default_factory=dict)
    neighbor_findings: list[dict] = Field(default_factory=list)


class CodeGraph(BaseModel):
    """Shared context bus for swarm workers.

    The code graph stores structural information from Layer 0 (AST parsing)
    and is enriched with findings from Layer 1 (swarm workers). Layer 2
    (synthesis) reads the full enriched graph.
    """

    nodes: dict[str, GraphNode] = Field(default_factory=dict)
    edges: list[GraphEdge] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def add_node(self, node: GraphNode) -> str:
        """Add a node to the graph. Returns the node ID."""
        self.nodes[node.id] = node
        return node.id

    def add_edge(self, edge: GraphEdge) -> None:
        """Add an edge to the graph."""
        self.edges.append(edge)

    def get_segment(self, segment_id: str) -> Segment | None:
        """Get a segment by ID."""
        for seg in self.segments:
            if seg.id == segment_id:
                return seg
        return None

    def query_segment(self, segment_id: str) -> SegmentContext:
        """Get all nodes/edges for a segment + its graph neighbors."""
        segment = self.get_segment(segment_id)
        if segment is None:
            return SegmentContext(segment=Segment(id=segment_id))

        # Get all nodes in this segment
        segment_nodes = [
            self.nodes[nid] for nid in segment.node_ids
            if nid in self.nodes
        ]

        # Get all edges involving this segment's nodes
        node_id_set = set(segment.node_ids)
        segment_edges = [
            e for e in self.edges
            if e.source_id in node_id_set or e.target_id in node_id_set
        ]

        return SegmentContext(
            segment=segment,
            nodes=segment_nodes,
            edges=segment_edges,
        )

    def query_neighbors(self, segment_id: str, depth: int = 1) -> list[dict]:
        """Get findings from neighboring segments (for Wave 2)."""
        segment = self.get_segment(segment_id)
        if segment is None:
            return []

        neighbor_ids = set(segment.internal_deps)
        # Also find segments that depend on us
        for seg in self.segments:
            if segment_id in seg.internal_deps:
                neighbor_ids.add(seg.id)

        neighbor_findings = []
        for seg in self.segments:
            if seg.id in neighbor_ids:
                neighbor_findings.extend(seg.findings)

        return neighbor_findings

    def add_finding(
        self,
        finding: dict,
        segment_id: str,
        affected_node_ids: list[str] | None = None,
    ) -> None:
        """Worker writes a finding, linked to graph nodes and segment."""
        segment = self.get_segment(segment_id)
        if segment is not None:
            segment.findings.append(finding)

        # Create edges from finding to affected nodes
        if affected_node_ids:
            finding_id = finding.get("id", f"finding-{uuid4().hex[:8]}")
            finding_node = GraphNode(
                id=finding_id,
                kind=NodeKind.FINDING,
                name=finding.get("title", ""),
                metadata=finding,
                segment_id=segment_id,
            )
            self.add_node(finding_node)
            for nid in affected_node_ids:
                self.add_edge(GraphEdge(
                    source_id=finding_id,
                    target_id=nid,
                    kind=EdgeKind.AFFECTS,
                ))

    def get_enriched_graph(self) -> dict:
        """Synthesis agent reads the full graph as structured JSON."""
        return {
            "nodes": [n.model_dump() for n in self.nodes.values()],
            "edges": [e.model_dump() for e in self.edges],
            "segments": [s.model_dump() for s in self.segments],
            "stats": self.stats,
            "total_findings": sum(len(s.findings) for s in self.segments),
        }

    def file_node_ids(self) -> dict[str, str]:
        """Map file paths to their node IDs."""
        return {
            n.file_path: n.id
            for n in self.nodes.values()
            if n.kind == NodeKind.FILE and n.file_path
        }
