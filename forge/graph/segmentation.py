"""Graph segmentation via community detection and directory grouping.

Extracted from builder.py to separate the segmentation/clustering
logic from AST parsing and graph construction.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

import networkx as nx

from forge.graph.models import (
    CodeGraph,
    EdgeKind,
    GraphNode,
    NodeKind,
    Segment,
)

logger = logging.getLogger(__name__)


def _segment_by_community_detection(
    graph: CodeGraph,
    nx_graph: nx.Graph,
    target_segments: int = 5,
    min_segment_size: int = 2,
) -> list[Segment]:
    """Use modularity-based community detection to cluster files.

    Falls back to directory-based segmentation if the graph is too sparse.
    """
    file_nodes = [
        n for n in graph.nodes.values()
        if n.kind == NodeKind.FILE
    ]

    if len(file_nodes) <= target_segments:
        # Too few files — single segment
        seg = Segment(
            id="seg-all",
            label="all",
            files=[n.file_path for n in file_nodes],
            node_ids=[n.id for n in file_nodes],
            loc=sum(n.loc for n in file_nodes),
        )
        return [seg]

    # Build undirected graph of file dependencies
    file_graph = nx.Graph()
    file_id_to_path = {n.id: n.file_path for n in file_nodes}
    file_path_to_id = {v: k for k, v in file_id_to_path.items()}

    for fid in file_id_to_path:
        file_graph.add_node(fid)

    # Add edges from import relationships
    for edge in graph.edges:
        if edge.kind in (EdgeKind.IMPORTS, EdgeKind.CALLS, EdgeKind.REFERENCES):
            src_file = None
            tgt_file = None

            src_node = graph.nodes.get(edge.source_id)
            tgt_node = graph.nodes.get(edge.target_id)

            if src_node and src_node.file_path:
                src_file = file_path_to_id.get(f"file:{src_node.file_path}")
            if tgt_node and tgt_node.file_path:
                tgt_file = file_path_to_id.get(f"file:{tgt_node.file_path}")

            if src_file and tgt_file and src_file != tgt_file:
                if file_graph.has_edge(src_file, tgt_file):
                    file_graph[src_file][tgt_file]["weight"] += 1
                else:
                    file_graph.add_edge(src_file, tgt_file, weight=1)

    # Try Louvain community detection
    if file_graph.number_of_edges() >= len(file_nodes) // 2:
        try:
            communities = nx.community.louvain_communities(
                file_graph,
                resolution=1.0,
                seed=42,
            )
            return _communities_to_segments(graph, communities, file_id_to_path)
        except Exception as e:
            logger.warning("Louvain community detection failed: %s, falling back to directory-based", e)

    # Fallback: directory-based segmentation
    return _segment_by_directory(graph, file_nodes, target_segments)


def _communities_to_segments(
    graph: CodeGraph,
    communities: list[set[str]],
    file_id_to_path: dict[str, str],
) -> list[Segment]:
    """Convert networkx communities to Segment objects."""
    segments = []
    for i, community in enumerate(communities):
        file_paths = [file_id_to_path[fid] for fid in community if fid in file_id_to_path]
        if not file_paths:
            continue

        # Derive a label from common directory prefix
        common = os.path.commonpath(file_paths) if len(file_paths) > 1 else str(Path(file_paths[0]).parent)
        label = Path(common).name or f"cluster-{i}"

        # Collect all node IDs in this segment
        node_ids = list(community)
        for fid in community:
            file_path = file_id_to_path.get(fid, "")
            for n in graph.nodes.values():
                if n.file_path == file_path and n.id not in node_ids:
                    node_ids.append(n.id)

        seg = Segment(
            id=f"seg-{label}-{i}",
            label=label,
            files=sorted(file_paths),
            node_ids=node_ids,
            loc=sum(graph.nodes[nid].loc for nid in community if nid in graph.nodes),
        )
        segments.append(seg)

    return segments


def _segment_by_directory(
    graph: CodeGraph,
    file_nodes: list[GraphNode],
    target_segments: int,
) -> list[Segment]:
    """Fallback segmentation based on top-level directory structure."""
    dir_groups: dict[str, list[GraphNode]] = defaultdict(list)

    for node in file_nodes:
        parts = Path(node.file_path).parts
        if len(parts) >= 2:
            top_dir = parts[0]
        else:
            top_dir = "root"
        dir_groups[top_dir].append(node)

    segments = []
    for i, (dir_name, nodes) in enumerate(sorted(dir_groups.items())):
        node_ids = [n.id for n in nodes]
        # Also gather non-file nodes in these files
        file_paths = {n.file_path for n in nodes}
        for n in graph.nodes.values():
            if n.file_path in file_paths and n.id not in node_ids:
                node_ids.append(n.id)

        seg = Segment(
            id=f"seg-{dir_name}-{i}",
            label=dir_name,
            files=sorted(n.file_path for n in nodes),
            node_ids=node_ids,
            loc=sum(n.loc for n in nodes),
        )
        segments.append(seg)

    return segments
