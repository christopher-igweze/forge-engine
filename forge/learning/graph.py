"""Computation graph representation of the FORGE agent pipeline.

Models FORGE pipeline runs as a directed graph: each agent invocation becomes
a node, data flow between phases becomes edges.  Built from telemetry
``invocations.jsonl`` files so we can trace which agents succeeded/failed
and feed that into the backward LLM for textual gradient generation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Phase ordering used to infer edges between nodes
PHASE_ORDER = ("discovery", "triage", "remediation", "validation")

# Agent name -> phase mapping
AGENT_PHASE_MAP: dict[str, str] = {
    "codebase_analyst": "discovery",
    "security_auditor": "discovery",
    "quality_auditor": "discovery",
    "architecture_reviewer": "discovery",
    "swarm_worker": "discovery",
    "synthesizer": "discovery",
    "intent_analyzer": "discovery",
    "triage_classifier": "triage",
    "fix_strategist": "triage",
    "coder_tier2": "remediation",
    "coder_tier3": "remediation",
    "test_generator": "remediation",
    "code_reviewer": "remediation",
    "escalation_agent": "remediation",
    "integration_validator": "validation",
    "debt_tracker": "validation",
}

# Data types flowing between phases
PHASE_EDGE_DATA: dict[tuple[str, str], str] = {
    ("discovery", "triage"): "findings",
    ("triage", "remediation"): "RemediationPlan",
    ("remediation", "validation"): "FixResults",
}


@dataclass
class GraphNode:
    """A node in the FORGE computation graph representing an agent invocation."""

    node_id: str  # e.g., "security_auditor_pass_1"
    agent_name: str  # e.g., "security_auditor"
    phase: str  # discovery | triage | remediation | validation
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""  # current system prompt
    metrics: dict[str, Any] = field(default_factory=dict)  # cost, latency, tokens
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "agent_name": self.agent_name,
            "phase": self.phase,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "prompt_template": self.prompt_template,
            "metrics": self.metrics,
            "success": self.success,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphNode:
        return cls(
            node_id=data["node_id"],
            agent_name=data["agent_name"],
            phase=data.get("phase", ""),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            prompt_template=data.get("prompt_template", ""),
            metrics=data.get("metrics", {}),
            success=data.get("success", True),
            error=data.get("error"),
        )


@dataclass
class GraphEdge:
    """An edge representing data flow between agents."""

    source: str  # node_id
    target: str  # node_id
    data_type: str  # e.g., "CodebaseMap", "findings", "RemediationPlan"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "data_type": self.data_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphEdge:
        return cls(
            source=data["source"],
            target=data["target"],
            data_type=data.get("data_type", ""),
        )


@dataclass
class ForgeGraph:
    """Directed computation graph of a FORGE pipeline run."""

    run_id: str
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        """Add a node to the graph. Overwrites if node_id already exists."""
        self.nodes[node.node_id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        """Add an edge to the graph."""
        self.edges.append(edge)

    def get_failed_nodes(self) -> list[GraphNode]:
        """Return all nodes that failed during execution."""
        return [n for n in self.nodes.values() if not n.success]

    def get_subgraph(self, phase: str) -> ForgeGraph:
        """Extract a subgraph containing only nodes from the given phase."""
        sub = ForgeGraph(run_id=f"{self.run_id}:{phase}")
        for node in self.nodes.values():
            if node.phase == phase:
                sub.add_node(node)
        for edge in self.edges:
            if edge.source in sub.nodes and edge.target in sub.nodes:
                sub.add_edge(edge)
        return sub

    def get_nodes_by_phase(self) -> dict[str, list[GraphNode]]:
        """Group nodes by phase."""
        by_phase: dict[str, list[GraphNode]] = {}
        for node in self.nodes.values():
            by_phase.setdefault(node.phase, []).append(node)
        return by_phase

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForgeGraph:
        graph = cls(run_id=data.get("run_id", ""))
        for nid, ndata in data.get("nodes", {}).items():
            graph.nodes[nid] = GraphNode.from_dict(ndata)
        for edata in data.get("edges", []):
            graph.edges.append(GraphEdge.from_dict(edata))
        return graph

    @classmethod
    def from_telemetry(cls, telemetry_path: Path) -> ForgeGraph:
        """Build graph from an invocations.jsonl telemetry file.

        Each invocation becomes a node.  Edges are inferred from the
        pipeline phase ordering: discovery -> triage -> remediation -> validation.
        """
        if not telemetry_path.exists():
            logger.warning("Telemetry file not found: %s", telemetry_path)
            return cls(run_id="unknown")

        invocations = _load_invocations(telemetry_path)
        if not invocations:
            logger.info("No invocations found in %s", telemetry_path)
            return cls(run_id="unknown")

        # Infer run_id from directory name or first timestamp
        run_id = telemetry_path.parent.name
        graph = cls(run_id=run_id)

        # Build nodes — deduplicate by assigning sequential IDs per agent
        agent_counts: dict[str, int] = {}
        for inv in invocations:
            agent = inv.get("agent_name", "unknown")
            count = agent_counts.get(agent, 0) + 1
            agent_counts[agent] = count

            node_id = f"{agent}_pass_{count}"
            phase = AGENT_PHASE_MAP.get(agent, _infer_phase(agent))

            graph.add_node(GraphNode(
                node_id=node_id,
                agent_name=agent,
                phase=phase,
                metrics={
                    "cost_usd": inv.get("cost_usd", 0.0),
                    "latency_ms": inv.get("latency_ms", 0),
                    "input_tokens": inv.get("input_tokens", 0),
                    "output_tokens": inv.get("output_tokens", 0),
                    "model": inv.get("model", ""),
                },
                success=inv.get("success", True),
                error=inv.get("error") or None,
            ))

        # Infer edges from phase ordering
        _infer_edges(graph)

        logger.info(
            "Built computation graph: %d nodes, %d edges from %s",
            len(graph.nodes), len(graph.edges), telemetry_path,
        )
        return graph

    def save(self, path: Path) -> None:
        """Serialize graph to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        logger.info("Saved computation graph to %s", path)

    @classmethod
    def load(cls, path: Path) -> ForgeGraph:
        """Load graph from JSON file."""
        data = json.loads(path.read_text())
        return cls.from_dict(data)


def _load_invocations(path: Path) -> list[dict[str, Any]]:
    """Load invocation records from a JSONL file."""
    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in %s", path)
    return entries


def _infer_phase(agent_name: str) -> str:
    """Best-effort phase inference for unknown agent names."""
    name = agent_name.lower()
    if any(k in name for k in ("audit", "analyst", "reviewer", "worker", "synth")):
        return "discovery"
    if any(k in name for k in ("classifier", "strateg", "triage")):
        return "triage"
    if any(k in name for k in ("coder", "test_gen", "code_review", "escalat")):
        return "remediation"
    if any(k in name for k in ("valid", "debt", "integrat")):
        return "validation"
    return "unknown"


def _infer_edges(graph: ForgeGraph) -> None:
    """Infer edges based on pipeline phase ordering.

    Connects the last node of each phase to the first node of the next phase.
    Within a phase, connects nodes sequentially by their node_id ordering.
    """
    by_phase = graph.get_nodes_by_phase()

    # Connect nodes within each phase sequentially
    for phase, nodes in by_phase.items():
        sorted_nodes = sorted(nodes, key=lambda n: n.node_id)
        for i in range(len(sorted_nodes) - 1):
            graph.add_edge(GraphEdge(
                source=sorted_nodes[i].node_id,
                target=sorted_nodes[i + 1].node_id,
                data_type="intra_phase",
            ))

    # Connect last node of each phase to first node of next phase
    for i in range(len(PHASE_ORDER) - 1):
        src_phase = PHASE_ORDER[i]
        tgt_phase = PHASE_ORDER[i + 1]

        src_nodes = by_phase.get(src_phase, [])
        tgt_nodes = by_phase.get(tgt_phase, [])

        if not src_nodes or not tgt_nodes:
            continue

        last_src = sorted(src_nodes, key=lambda n: n.node_id)[-1]
        first_tgt = sorted(tgt_nodes, key=lambda n: n.node_id)[0]
        data_type = PHASE_EDGE_DATA.get((src_phase, tgt_phase), "pipeline_data")

        graph.add_edge(GraphEdge(
            source=last_src.node_id,
            target=first_tgt.node_id,
            data_type=data_type,
        ))
