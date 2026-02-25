"""Tests for Hive Discovery synthesis agent (Layer 2).

Covers:
- SynthesisAgent.synthesize() with mocked LLM — full result structure
- Findings enrichment (agent tag, auto-generated IDs)
- Remediation plan total_items computation
- Synthesis prompt building (_build_synthesis_task)
- Fallback behavior when LLM returns garbage
- Default codebase_map from graph
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

from forge.graph.models import (
    CodeGraph,
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    Segment,
)
from forge.swarm.synthesizer import (
    SynthesisAgent,
    _build_synthesis_task,
    _parse_json_response,
    SYNTHESIS_SYSTEM_PROMPT,
)


# ── Mock helpers ────────────────────────────────────────────────────


@contextmanager
def mock_agent_ai(run_side_effect=None, run_return_value=None):
    """Context manager that injects a mock forge.vendor.agent_ai into sys.modules.

    Avoids importing the real module which uses Python 3.12+ syntax.
    """
    mock_instance = MagicMock()
    if run_side_effect is not None:
        mock_instance.run = AsyncMock(side_effect=run_side_effect)
    elif run_return_value is not None:
        mock_instance.run = AsyncMock(return_value=run_return_value)
    else:
        mock_instance.run = AsyncMock(return_value=MagicMock(parsed=None, text="{}"))

    mock_ai_cls = MagicMock(return_value=mock_instance)
    mock_config_cls = MagicMock()

    fake_mod = ModuleType("forge.vendor.agent_ai")
    fake_mod.AgentAI = mock_ai_cls
    fake_mod.AgentAIConfig = mock_config_cls

    saved = sys.modules.get("forge.vendor.agent_ai")
    sys.modules["forge.vendor.agent_ai"] = fake_mod
    try:
        yield mock_ai_cls, mock_instance
    finally:
        if saved is None:
            sys.modules.pop("forge.vendor.agent_ai", None)
        else:
            sys.modules["forge.vendor.agent_ai"] = saved


# ── Fixtures ────────────────────────────────────────────────────────


def _make_enriched_graph(
    num_segments: int = 2,
    findings_per_segment: int = 3,
) -> CodeGraph:
    """Build a CodeGraph with multiple segments and findings for synthesis."""
    segments = []
    nodes = {}
    edges = []

    for s in range(num_segments):
        seg_id = f"seg-{s}"
        files = [f"src/module{s}/file{f}.py" for f in range(3)]
        node_ids = []

        for fp in files:
            nid = f"file:{fp}"
            node_ids.append(nid)
            nodes[nid] = GraphNode(
                id=nid,
                kind=NodeKind.FILE,
                name=fp.split("/")[-1],
                file_path=fp,
                language="python",
                loc=50,
                segment_id=seg_id,
            )

        findings = []
        for f in range(findings_per_segment):
            findings.append({
                "id": f"FIND-{s}-{f}",
                "title": f"Finding {s}-{f}",
                "description": f"Description for finding {s}-{f}",
                "category": ["security", "quality", "architecture"][f % 3],
                "severity": "high",
                "worker_type": ["security", "quality", "architecture"][f % 3],
                "segment_id": seg_id,
                "wave": 1,
                "confidence": 0.8,
            })

        seg = Segment(
            id=seg_id,
            label=f"module{s}",
            files=files,
            node_ids=node_ids,
            loc=150,
            entry_points=[f"src/module{s}/file0.py:main"],
            external_deps=["fastapi", "pydantic"],
            internal_deps=[f"seg-{s+1}"] if s < num_segments - 1 else [],
            findings=findings,
        )
        segments.append(seg)

        if s > 0:
            edges.append(GraphEdge(
                source_id=f"file:src/module{s}/file0.py",
                target_id=f"file:src/module{s-1}/file0.py",
                kind=EdgeKind.DEPENDS_ON,
            ))

    graph = CodeGraph(
        nodes=nodes,
        edges=edges,
        segments=segments,
        stats={
            "total_files": num_segments * 3,
            "total_loc": num_segments * 150,
            "languages": {"python": 1.0},
            "total_segments": num_segments,
        },
    )
    return graph


def _make_synthesis_response(
    num_findings: int = 3,
    include_codebase_map: bool = True,
) -> MagicMock:
    """Create a mock AgentResponse with a full synthesis JSON."""
    findings = []
    decisions = []
    items = []

    for i in range(num_findings):
        fid = f"F-synth{i:03d}"
        findings.append({
            "id": fid,
            "title": f"Synthesized finding {i}",
            "description": f"Cross-referenced issue {i}",
            "category": ["security", "quality", "architecture"][i % 3],
            "severity": ["critical", "high", "medium"][i % 3],
            "locations": [{"file_path": f"src/module0/file{i}.py", "line_start": 10, "line_end": 20, "snippet": "code"}],
            "suggested_fix": f"Fix {i}",
            "confidence": 0.85,
            "agent": "synthesis",
            "tier": (i % 3) + 1,
        })
        decisions.append({
            "finding_id": fid,
            "tier": (i % 3) + 1,
            "confidence": 0.9,
            "rationale": f"Rationale for {fid}",
        })
        items.append({
            "finding_id": fid,
            "title": f"Fix {fid}",
            "tier": (i % 3) + 1,
            "priority": i + 1,
            "estimated_files": 1,
            "files_to_modify": [f"src/module0/file{i}.py"],
            "depends_on": [],
            "acceptance_criteria": ["Tests pass"],
            "approach": f"Approach for {fid}",
        })

    data = {
        "findings": findings,
        "triage_result": {
            "decisions": decisions,
            "tier_0_count": 0,
            "tier_1_count": sum(1 for d in decisions if d["tier"] == 1),
            "tier_2_count": sum(1 for d in decisions if d["tier"] == 2),
            "tier_3_count": sum(1 for d in decisions if d["tier"] == 3),
        },
        "remediation_plan": {
            "items": items,
            "dependencies": [],
            "execution_levels": [[i["finding_id"] for i in items]],
            "deferred_finding_ids": [],
            "total_items": len(items),
            "summary": "Fix everything",
        },
    }

    if include_codebase_map:
        data["codebase_map"] = {
            "modules": [{"name": "module0", "path": "src/module0", "purpose": "Main module", "files": ["file0.py"], "loc": 150}],
            "dependencies": [{"name": "fastapi", "version": "0.100", "ecosystem": "python", "dev_only": False}],
            "data_flows": [],
            "auth_boundaries": [],
            "entry_points": [{"path": "src/module0/file0.py", "type": "route", "is_public": True}],
            "tech_stack": {"backend": "python", "packages": ["fastapi"]},
            "architecture_summary": "Monolithic FastAPI app",
            "key_patterns": ["REST API"],
        }

    resp = MagicMock()
    resp.parsed = None
    resp.is_error = False
    resp.text = json.dumps(data)
    return resp


def _make_garbage_response() -> MagicMock:
    """Create a mock response with invalid JSON."""
    resp = MagicMock()
    resp.parsed = None
    resp.is_error = False
    resp.text = "I couldn't analyze the codebase properly. Here are some thoughts..."
    return resp


# ── TestSynthesisAgent ──────────────────────────────────────────────


class TestSynthesisAgent:
    """Test SynthesisAgent.synthesize() with mocked LLM."""

    async def test_synthesize_returns_all_keys(self):
        graph = _make_enriched_graph()
        response = _make_synthesis_response(num_findings=3)

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        assert "codebase_map" in result
        assert "findings" in result
        assert "triage_result" in result
        assert "remediation_plan" in result

    async def test_findings_have_agent_tag(self):
        graph = _make_enriched_graph()
        response = _make_synthesis_response(num_findings=3)

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        for f in result["findings"]:
            assert f["agent"] == "synthesis"

    async def test_findings_get_auto_id_if_missing(self):
        """Findings without an ID get an auto-generated one."""
        graph = _make_enriched_graph()

        # Build response with findings missing IDs
        data = {
            "findings": [
                {"title": "No ID finding", "description": "desc", "category": "security", "severity": "high"},
                {"id": "F-existing", "title": "Has ID", "description": "desc", "category": "quality", "severity": "medium"},
            ],
            "triage_result": {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0},
            "remediation_plan": {"items": [], "total_items": 0, "summary": ""},
        }
        resp = MagicMock()
        resp.parsed = None
        resp.is_error = False
        resp.text = json.dumps(data)

        with mock_agent_ai(run_return_value=resp):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        # First finding should have auto-generated ID
        assert result["findings"][0]["id"].startswith("F-")
        # Second finding should keep its existing ID
        assert result["findings"][1]["id"] == "F-existing"

    async def test_remediation_plan_total_items_computed(self):
        graph = _make_enriched_graph()
        response = _make_synthesis_response(num_findings=5)

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        plan = result["remediation_plan"]
        assert plan["total_items"] == len(plan["items"])
        assert plan["total_items"] == 5

    async def test_remediation_plan_total_items_overridden(self):
        """total_items is recomputed from items even if LLM returns wrong count."""
        graph = _make_enriched_graph()

        data = {
            "findings": [],
            "triage_result": {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0},
            "remediation_plan": {
                "items": [
                    {"finding_id": "F-001", "title": "Fix 1", "tier": 2, "priority": 1, "estimated_files": 1},
                    {"finding_id": "F-002", "title": "Fix 2", "tier": 2, "priority": 2, "estimated_files": 1},
                ],
                "total_items": 999,  # Wrong count — should be overridden
                "summary": "Fix things",
            },
        }
        resp = MagicMock()
        resp.parsed = None
        resp.is_error = False
        resp.text = json.dumps(data)

        with mock_agent_ai(run_return_value=resp):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        assert result["remediation_plan"]["total_items"] == 2

    async def test_codebase_map_from_llm(self):
        graph = _make_enriched_graph()
        response = _make_synthesis_response(num_findings=1, include_codebase_map=True)

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        cmap = result["codebase_map"]
        assert "modules" in cmap
        assert len(cmap["modules"]) > 0
        assert cmap["modules"][0]["name"] == "module0"

    async def test_triage_result_structure(self):
        graph = _make_enriched_graph()
        response = _make_synthesis_response(num_findings=3)

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        triage = result["triage_result"]
        assert "decisions" in triage
        assert isinstance(triage["decisions"], list)
        assert "tier_0_count" in triage
        assert "tier_1_count" in triage
        assert "tier_2_count" in triage
        assert "tier_3_count" in triage


# ── TestSynthesisPromptBuilding ─────────────────────────────────────


class TestSynthesisPromptBuilding:
    """Test _build_synthesis_task produces correct prompt content."""

    def test_includes_graph_stats(self):
        graph = _make_enriched_graph()
        task = _build_synthesis_task(graph)
        assert "graph_stats" in task
        assert "total_files" in task

    def test_includes_segment_summaries(self):
        graph = _make_enriched_graph(num_segments=3)
        task = _build_synthesis_task(graph)
        assert "seg-0" in task
        assert "seg-1" in task
        assert "seg-2" in task
        assert "module0" in task
        assert "module1" in task

    def test_includes_worker_findings(self):
        graph = _make_enriched_graph(num_segments=2, findings_per_segment=3)
        task = _build_synthesis_task(graph)
        assert "all_worker_findings" in task
        assert "FIND-0-0" in task

    def test_worker_findings_capped_at_100(self):
        # Create a graph with more than 100 findings total
        graph = _make_enriched_graph(num_segments=10, findings_per_segment=20)
        task = _build_synthesis_task(graph)
        # Parse the embedded JSON to verify
        json_start = task.index("```json\n") + 8
        json_end = task.index("\n```", json_start)
        embedded = json.loads(task[json_start:json_end])
        assert len(embedded["all_worker_findings"]) <= 100

    def test_includes_cross_segment_edges(self):
        graph = _make_enriched_graph(num_segments=2)
        task = _build_synthesis_task(graph)
        assert "cross_segment_edges" in task

    def test_includes_finding_count_in_instructions(self):
        graph = _make_enriched_graph(num_segments=2, findings_per_segment=5)
        task = _build_synthesis_task(graph)
        # Should mention the count of worker findings
        assert "10 worker findings" in task

    def test_synthesis_system_prompt_structure(self):
        """SYNTHESIS_SYSTEM_PROMPT has the right output schema."""
        assert "codebase_map" in SYNTHESIS_SYSTEM_PROMPT
        assert "findings" in SYNTHESIS_SYSTEM_PROMPT
        assert "triage_result" in SYNTHESIS_SYSTEM_PROMPT
        assert "remediation_plan" in SYNTHESIS_SYSTEM_PROMPT
        assert "Tier 0" in SYNTHESIS_SYSTEM_PROMPT
        assert "Tier 3" in SYNTHESIS_SYSTEM_PROMPT


# ── TestSynthesisFallback ───────────────────────────────────────────


class TestSynthesisFallback:
    """When LLM returns garbage, defaults are used."""

    async def test_garbage_response_produces_defaults(self):
        graph = _make_enriched_graph()
        response = _make_garbage_response()

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        # Should still have all required keys
        assert "codebase_map" in result
        assert "findings" in result
        assert "triage_result" in result
        assert "remediation_plan" in result

        # Default codebase_map built from graph
        cmap = result["codebase_map"]
        assert "modules" in cmap
        assert len(cmap["modules"]) == 2  # 2 segments = 2 modules

        # Findings should be empty (LLM returned none)
        assert result["findings"] == []

        # Remediation plan should be empty
        assert result["remediation_plan"]["total_items"] == 0

    async def test_fallback_triage_result_structure(self):
        graph = _make_enriched_graph()
        response = _make_garbage_response()

        with mock_agent_ai(run_return_value=response):
            agent = SynthesisAgent()
            result = await agent.synthesize(graph, "/tmp/repo")

        triage = result["triage_result"]
        assert triage["decisions"] == []
        assert triage["tier_0_count"] == 0


# ── TestDefaultCodebaseMap ──────────────────────────────────────────


class TestDefaultCodebaseMap:
    """Test _build_default_codebase_map produces valid structure."""

    def test_modules_from_segments(self):
        graph = _make_enriched_graph(num_segments=3)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert "modules" in cmap
        assert len(cmap["modules"]) == 3
        for mod in cmap["modules"]:
            assert "name" in mod
            assert "path" in mod
            assert "files" in mod
            assert "loc" in mod

    def test_module_names_from_labels(self):
        graph = _make_enriched_graph(num_segments=2)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        names = [m["name"] for m in cmap["modules"]]
        assert "module0" in names
        assert "module1" in names

    def test_module_path_from_first_file(self):
        graph = _make_enriched_graph(num_segments=1)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["modules"][0]["path"] == "src/module0/file0.py"

    def test_loc_total_from_stats(self):
        graph = _make_enriched_graph(num_segments=2)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["loc_total"] == 300  # 2 segments * 150 LOC

    def test_file_count_from_stats(self):
        graph = _make_enriched_graph(num_segments=2)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["file_count"] == 6  # 2 segments * 3 files

    def test_primary_language(self):
        graph = _make_enriched_graph(num_segments=1)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["primary_language"] == "python"
        assert "python" in cmap["languages"]

    def test_empty_collections(self):
        graph = _make_enriched_graph(num_segments=1)
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["dependencies"] == []
        assert cmap["data_flows"] == []
        assert cmap["auth_boundaries"] == []
        assert cmap["entry_points"] == []

    def test_empty_graph_produces_valid_map(self):
        graph = CodeGraph(stats={"total_loc": 0, "total_files": 0, "languages": {}})
        agent = SynthesisAgent()
        cmap = agent._build_default_codebase_map(graph)

        assert cmap["modules"] == []
        assert cmap["loc_total"] == 0
        assert cmap["file_count"] == 0
        assert cmap["primary_language"] == ""


# ── TestSynthesisAgentInit ──────────────────────────────────────────


class TestSynthesisAgentInit:
    def test_default_model(self):
        agent = SynthesisAgent()
        assert agent.model == "anthropic/claude-sonnet-4.6"

    def test_custom_model(self):
        agent = SynthesisAgent(model="custom/model")
        assert agent.model == "custom/model"

    def test_default_provider(self):
        agent = SynthesisAgent()
        assert agent.ai_provider == "openrouter_direct"

    def test_custom_provider(self):
        agent = SynthesisAgent(ai_provider="claude")
        assert agent.ai_provider == "claude"


# ── TestParseJsonResponse ───────────────────────────────────────────


class TestSynthesizerParseJsonResponse:
    def test_plain_json(self):
        result = _parse_json_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_code_block(self):
        raw = '```json\n{"key": "value"}\n```'
        result = _parse_json_response(raw)
        assert result == {"key": "value"}

    def test_invalid_json_returns_empty_dict(self):
        result = _parse_json_response("totally not json")
        assert result == {}

    def test_empty_string(self):
        result = _parse_json_response("")
        assert result == {}
