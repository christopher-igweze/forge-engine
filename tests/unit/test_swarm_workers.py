"""Tests for Hive Discovery swarm workers (Layer 1).

Covers:
- SwarmWorker ABC cannot be instantiated directly
- SecurityWorker, QualityWorker, ArchitectureWorker prompt content
- Worker.analyze() with mocked LLM — findings parsed, written to graph
- Wave 1 vs Wave 2 neighbor findings behavior
- Error handling for invalid LLM JSON
- Helper functions: _format_file_contents, _format_graph_context, _format_neighbor_findings
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

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
from forge.swarm.worker import (
    ArchitectureWorker,
    QualityWorker,
    SecurityWorker,
    SwarmWorker,
    _format_file_contents,
    _format_graph_context,
    _format_neighbor_findings,
    _parse_json_response,
    _read_file_safe,
    _truncate_contents,
)


# ── Mock helpers ────────────────────────────────────────────────────


@contextmanager
def mock_agent_ai(run_side_effect=None, run_return_value=None):
    """Context manager that injects a mock forge.vendor.agent_ai into sys.modules.

    The lazy ``from forge.vendor.agent_ai import AgentAI, AgentAIConfig``
    inside worker.analyze() and synthesizer.synthesize() will resolve to
    mock classes provided by this helper.

    This avoids importing the real module which uses Python 3.12+ syntax.
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

    # Build a fake module
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


def _make_graph_with_segment(
    segment_id: str = "seg-test",
    files: list[str] | None = None,
    neighbor_segment_id: str | None = None,
) -> CodeGraph:
    """Build a minimal CodeGraph with one segment (and optionally a neighbor)."""
    files = files or ["src/app.py", "src/utils.py"]
    node_ids = []
    nodes = {}
    edges = []

    for f in files:
        nid = f"file:{f}"
        node_ids.append(nid)
        nodes[nid] = GraphNode(
            id=nid,
            kind=NodeKind.FILE,
            name=f.split("/")[-1],
            file_path=f,
            language="python",
            loc=50,
            segment_id=segment_id,
        )

    seg = Segment(
        id=segment_id,
        label="test-segment",
        files=files,
        node_ids=node_ids,
        loc=100,
        entry_points=["src/app.py:main"],
        external_deps=["fastapi", "pydantic"],
        internal_deps=[neighbor_segment_id] if neighbor_segment_id else [],
    )

    segments = [seg]

    if neighbor_segment_id:
        neighbor_files = ["lib/helper.py"]
        neighbor_node_ids = []
        for f in neighbor_files:
            nid = f"file:{f}"
            neighbor_node_ids.append(nid)
            nodes[nid] = GraphNode(
                id=nid,
                kind=NodeKind.FILE,
                name=f.split("/")[-1],
                file_path=f,
                language="python",
                loc=30,
                segment_id=neighbor_segment_id,
            )

        neighbor_seg = Segment(
            id=neighbor_segment_id,
            label="neighbor-segment",
            files=neighbor_files,
            node_ids=neighbor_node_ids,
            loc=30,
            findings=[
                {
                    "id": "NEIGH-001",
                    "title": "Neighbor issue",
                    "description": "Found in neighbor segment",
                    "category": "security",
                    "severity": "high",
                    "worker_type": "security",
                    "segment_id": neighbor_segment_id,
                    "wave": 1,
                }
            ],
        )
        segments.append(neighbor_seg)

        edges.append(GraphEdge(
            source_id=f"file:{files[0]}",
            target_id=f"file:{neighbor_files[0]}",
            kind=EdgeKind.DEPENDS_ON,
        ))

    graph = CodeGraph(
        nodes=nodes,
        edges=edges,
        segments=segments,
        stats={"total_files": len(files), "total_loc": 100},
    )
    return graph


def _make_llm_response(findings_json: dict | None = None, raw_text: str | None = None):
    """Create a mock AgentResponse."""
    resp = MagicMock()
    resp.parsed = None
    resp.is_error = False
    if raw_text is not None:
        resp.text = raw_text
    elif findings_json is not None:
        resp.text = json.dumps(findings_json)
    else:
        resp.text = json.dumps({"findings": [], "summary": "No issues found"})
    return resp


# ── TestSwarmWorkerABC ──────────────────────────────────────────────


class TestSwarmWorkerABC:
    """SwarmWorker is abstract and cannot be instantiated directly."""

    def test_cannot_instantiate_base_class(self):
        with pytest.raises(TypeError):
            SwarmWorker(segment_id="seg-test")

    def test_worker_type_default(self):
        assert SwarmWorker.worker_type == "base"


# ── TestSecurityWorker ──────────────────────────────────────────────


class TestSecurityWorker:
    def test_worker_type(self):
        w = SecurityWorker(segment_id="seg-test")
        assert w.worker_type == "security"

    def test_system_prompt_contains_security_focus(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "security" in prompt.lower()
        assert "authentication" in prompt.lower()
        assert "injection" in prompt.lower()
        assert "XSS" in prompt
        assert "secrets" in prompt.lower()

    def test_system_prompt_requires_json_output(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "JSON" in prompt
        assert '"findings"' in prompt

    def test_task_prompt_includes_file_contents(self):
        w = SecurityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "import os\nprint('hello')"},
        )
        prompt = w.build_task_prompt(ctx, wave=1, repo_path="/tmp/repo")
        assert "app.py" in prompt
        assert "import os" in prompt

    def test_task_prompt_includes_graph_context(self):
        w = SecurityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(
                id="seg-test",
                label="test-segment",
                files=["app.py"],
                loc=100,
                entry_points=["app.py:main"],
                external_deps=["fastapi"],
            ),
            file_contents={"app.py": "code"},
        )
        prompt = w.build_task_prompt(ctx, wave=1, repo_path="/tmp/repo")
        assert "test-segment" in prompt
        assert "Entry Points" in prompt
        assert "app.py:main" in prompt

    def test_task_prompt_wave1_no_neighbor_findings(self):
        w = SecurityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "code"},
            neighbor_findings=[
                {"category": "security", "title": "Neighbor vuln", "description": "desc"},
            ],
        )
        # Wave 1 should NOT include neighbor findings even if they are present
        prompt = w.build_task_prompt(ctx, wave=1, repo_path="/tmp/repo")
        assert "Neighbor vuln" not in prompt
        assert "Neighboring Segments" not in prompt

    def test_task_prompt_wave2_includes_neighbor_findings(self):
        w = SecurityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "code"},
            neighbor_findings=[
                {"category": "security", "title": "Neighbor vuln", "description": "found issue"},
            ],
        )
        prompt = w.build_task_prompt(ctx, wave=2, repo_path="/tmp/repo")
        assert "Neighbor vuln" in prompt
        assert "Neighboring Segments" in prompt
        assert "vulnerability chains" in prompt


# ── TestQualityWorker ───────────────────────────────────────────────


class TestQualityWorker:
    def test_worker_type(self):
        w = QualityWorker(segment_id="seg-test")
        assert w.worker_type == "quality"

    def test_system_prompt_contains_quality_focus(self):
        w = QualityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "quality" in prompt.lower()
        assert "error handling" in prompt.lower()
        assert "duplication" in prompt.lower()
        assert "cyclomatic complexity" in prompt.lower()
        assert "performance" in prompt.lower()

    def test_system_prompt_requires_json_output(self):
        w = QualityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "JSON" in prompt
        assert '"findings"' in prompt

    def test_task_prompt_wave2_includes_neighbor_findings(self):
        w = QualityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "code"},
            neighbor_findings=[
                {"category": "quality", "title": "DRY violation", "description": "repeated code"},
            ],
        )
        prompt = w.build_task_prompt(ctx, wave=2, repo_path="/tmp/repo")
        assert "DRY violation" in prompt
        assert "quality patterns" in prompt


# ── TestArchitectureWorker ──────────────────────────────────────────


class TestArchitectureWorker:
    def test_worker_type(self):
        w = ArchitectureWorker(segment_id="seg-test")
        assert w.worker_type == "architecture"

    def test_system_prompt_contains_architecture_focus(self):
        w = ArchitectureWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "architecture" in prompt.lower()
        assert "coupling" in prompt.lower()
        assert "layering" in prompt.lower()
        assert "scalability" in prompt.lower()
        assert "dependency management" in prompt.lower()

    def test_system_prompt_requires_json_output(self):
        w = ArchitectureWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "JSON" in prompt
        assert '"findings"' in prompt

    def test_task_prompt_wave2_includes_neighbor_findings(self):
        w = ArchitectureWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "code"},
            neighbor_findings=[
                {"category": "architecture", "title": "Tight coupling", "description": "modules coupled"},
            ],
        )
        prompt = w.build_task_prompt(ctx, wave=2, repo_path="/tmp/repo")
        assert "Tight coupling" in prompt
        assert "cross-cutting" in prompt


# ── TestWorkerAnalyze ───────────────────────────────────────────────


class TestWorkerAnalyze:
    """Test SwarmWorker.analyze() with mocked LLM."""

    async def test_analyze_parses_findings(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response({
            "findings": [
                {
                    "id": "SEC-001",
                    "title": "SQL Injection",
                    "description": "Unsanitized input in query",
                    "category": "security",
                    "severity": "critical",
                    "confidence": 0.9,
                }
            ],
            "summary": "Found SQL injection",
        })

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert len(findings) == 1
        assert findings[0]["id"] == "SEC-001"
        assert findings[0]["title"] == "SQL Injection"
        assert findings[0]["worker_type"] == "security"
        assert findings[0]["segment_id"] == "seg-test"
        assert findings[0]["wave"] == 1

    async def test_analyze_writes_findings_to_graph(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response({
            "findings": [
                {"id": "SEC-001", "title": "Vuln", "description": "desc", "category": "security", "severity": "high"},
                {"id": "SEC-002", "title": "Vuln2", "description": "desc2", "category": "security", "severity": "medium"},
            ],
        })

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        segment = graph.get_segment("seg-test")
        assert len(segment.findings) == 2
        assert segment.findings[0]["id"] == "SEC-001"
        assert segment.findings[1]["id"] == "SEC-002"

    async def test_wave1_does_not_include_neighbor_findings_in_prompt(self):
        graph = _make_graph_with_segment(neighbor_segment_id="seg-neighbor")
        response = _make_llm_response({"findings": []})

        captured_prompt = {}

        async def capture_run(prompt, system_prompt=None):
            captured_prompt["task"] = prompt
            return response

        with mock_agent_ai(run_side_effect=capture_run):
            worker = SecurityWorker(segment_id="seg-test")
            await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert "Neighbor issue" not in captured_prompt["task"]
        assert "Neighboring Segments" not in captured_prompt["task"]

    async def test_wave2_includes_neighbor_findings_in_prompt(self):
        graph = _make_graph_with_segment(neighbor_segment_id="seg-neighbor")
        response = _make_llm_response({"findings": []})

        captured_prompt = {}

        async def capture_run(prompt, system_prompt=None):
            captured_prompt["task"] = prompt
            return response

        with mock_agent_ai(run_side_effect=capture_run):
            worker = SecurityWorker(segment_id="seg-test")
            await worker.analyze(graph, wave=2, repo_path="/tmp/repo")

        assert "Neighbor issue" in captured_prompt["task"]

    async def test_analyze_quality_worker(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response({
            "findings": [
                {"id": "QUAL-001", "title": "Missing error handling", "description": "No try-catch", "category": "quality", "severity": "medium"},
            ],
        })

        with mock_agent_ai(run_return_value=response):
            worker = QualityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert len(findings) == 1
        assert findings[0]["worker_type"] == "quality"

    async def test_analyze_architecture_worker(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response({
            "findings": [
                {"id": "ARCH-001", "title": "Tight coupling", "description": "Modules tightly coupled", "category": "architecture", "severity": "high"},
            ],
        })

        with mock_agent_ai(run_return_value=response):
            worker = ArchitectureWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert len(findings) == 1
        assert findings[0]["worker_type"] == "architecture"


# ── TestWorkerErrorHandling ─────────────────────────────────────────


class TestWorkerErrorHandling:
    """When LLM returns invalid JSON, worker returns empty findings."""

    async def test_invalid_json_returns_empty_findings(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response(raw_text="This is not valid JSON at all!")

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert findings == []

    async def test_json_with_no_findings_key_returns_empty(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response(raw_text=json.dumps({"summary": "nothing found"}))

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert findings == []

    async def test_findings_not_a_list_returns_empty(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response(raw_text=json.dumps({"findings": "not a list"}))

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert findings == []

    async def test_json_in_code_block_parsed(self):
        graph = _make_graph_with_segment()
        json_content = json.dumps({
            "findings": [{"id": "SEC-001", "title": "Found it", "description": "desc", "category": "security", "severity": "high"}],
        })
        raw_text = f"```json\n{json_content}\n```"
        response = _make_llm_response(raw_text=raw_text)

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert len(findings) == 1
        assert findings[0]["id"] == "SEC-001"

    async def test_empty_response_text(self):
        graph = _make_graph_with_segment()
        response = _make_llm_response(raw_text="")

        with mock_agent_ai(run_return_value=response):
            worker = SecurityWorker(segment_id="seg-test")
            findings = await worker.analyze(graph, wave=1, repo_path="/tmp/repo")

        assert findings == []


# ── TestParseJsonResponse ───────────────────────────────────────────


class TestParseJsonResponse:
    def test_plain_json(self):
        result = _parse_json_response('{"findings": [{"id": "F-1"}]}')
        assert result["findings"][0]["id"] == "F-1"

    def test_json_in_code_block(self):
        raw = '```json\n{"findings": [{"id": "F-1"}]}\n```'
        result = _parse_json_response(raw)
        assert result["findings"][0]["id"] == "F-1"

    def test_json_in_code_block_no_language(self):
        raw = '```\n{"key": "value"}\n```'
        result = _parse_json_response(raw)
        assert result["key"] == "value"

    def test_invalid_json_returns_empty_dict(self):
        result = _parse_json_response("not json at all")
        assert result == {}

    def test_empty_string_returns_empty_dict(self):
        result = _parse_json_response("")
        assert result == {}


# ── TestFormatFileContents ──────────────────────────────────────────


class TestFormatFileContents:
    def test_formats_files(self):
        contents = {"app.py": "import os", "utils.py": "def helper(): pass"}
        result = _format_file_contents(contents)
        assert "### app.py" in result
        assert "### utils.py" in result
        assert "import os" in result
        assert "def helper(): pass" in result

    def test_truncates_at_max_total(self):
        contents = {
            "big.py": "x" * 100,
            "small.py": "y" * 10,
        }
        result = _format_file_contents(contents, max_total=50)
        # big.py is sorted first, has 100 chars > 50, so break immediately
        assert result == "(no files)"

    def test_includes_files_up_to_limit(self):
        contents = {
            "a.py": "aaa",      # 3 chars, fits
            "b.py": "bbb",      # 3 chars, fits
            "c.py": "x" * 100,  # won't fit
        }
        result = _format_file_contents(contents, max_total=10)
        assert "### a.py" in result
        assert "### b.py" in result
        assert "### c.py" not in result

    def test_empty_contents(self):
        assert _format_file_contents({}) == "(no files)"

    def test_files_sorted_by_path(self):
        contents = {"z.py": "z", "a.py": "a", "m.py": "m"}
        result = _format_file_contents(contents)
        a_pos = result.index("### a.py")
        m_pos = result.index("### m.py")
        z_pos = result.index("### z.py")
        assert a_pos < m_pos < z_pos


# ── TestFormatGraphContext ──────────────────────────────────────────


class TestFormatGraphContext:
    def test_basic_format(self):
        ctx = SegmentContext(
            segment=Segment(
                id="seg-test",
                label="test-segment",
                files=["a.py", "b.py"],
                loc=150,
            ),
        )
        result = _format_graph_context(ctx)
        assert "seg-test" in result
        assert "test-segment" in result
        assert "Files: 2" in result
        assert "LOC: 150" in result

    def test_includes_entry_points(self):
        ctx = SegmentContext(
            segment=Segment(
                id="seg-test",
                label="test",
                entry_points=["app.py:main", "app.py:handler"],
            ),
        )
        result = _format_graph_context(ctx)
        assert "Entry Points:" in result
        assert "app.py:main" in result

    def test_includes_external_deps(self):
        ctx = SegmentContext(
            segment=Segment(
                id="seg-test",
                label="test",
                external_deps=["fastapi", "pydantic", "sqlalchemy"],
            ),
        )
        result = _format_graph_context(ctx)
        assert "External Dependencies:" in result
        assert "fastapi" in result

    def test_includes_internal_deps(self):
        ctx = SegmentContext(
            segment=Segment(
                id="seg-test",
                label="test",
                internal_deps=["seg-auth", "seg-core"],
            ),
        )
        result = _format_graph_context(ctx)
        assert "Depends on segments:" in result
        assert "seg-auth" in result

    def test_includes_edge_summary(self):
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test"),
            edges=[
                GraphEdge(source_id="a", target_id="b", kind=EdgeKind.IMPORTS),
                GraphEdge(source_id="c", target_id="d", kind=EdgeKind.IMPORTS),
                GraphEdge(source_id="e", target_id="f", kind=EdgeKind.CALLS),
            ],
        )
        result = _format_graph_context(ctx)
        assert "Edges:" in result
        assert "imports=2" in result
        assert "calls=1" in result


# ── TestFormatNeighborFindings ──────────────────────────────────────


class TestFormatNeighborFindings:
    def test_empty_returns_no_findings_message(self):
        result = _format_neighbor_findings([])
        assert "no findings" in result

    def test_formats_findings(self):
        findings = [
            {"category": "security", "title": "SQL Injection", "description": "Input not sanitized"},
            {"category": "quality", "title": "Missing error handling", "description": "No try-catch"},
        ]
        result = _format_neighbor_findings(findings)
        assert "SQL Injection" in result
        assert "Missing error handling" in result
        assert "[security]" in result
        assert "[quality]" in result

    def test_caps_at_20(self):
        findings = [
            {"category": "security", "title": f"Finding-{i}", "description": f"desc-{i}"}
            for i in range(30)
        ]
        result = _format_neighbor_findings(findings)
        assert "Finding-19" in result
        assert "Finding-20" not in result

    def test_handles_missing_keys(self):
        findings = [{"some_key": "some_value"}]
        result = _format_neighbor_findings(findings)
        assert "[?]" in result
        assert "?" in result

    def test_truncates_long_descriptions(self):
        findings = [{"category": "sec", "title": "Vuln", "description": "x" * 500}]
        result = _format_neighbor_findings(findings)
        # Description is truncated to 200 chars in the formatter
        assert len(result) < 500


# ── TestReadFileSafe ────────────────────────────────────────────────


class TestReadFileSafe:
    def test_reads_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world")
        content = _read_file_safe(str(f))
        assert content == "hello world"

    def test_truncates_large_files(self, tmp_path):
        f = tmp_path / "large.py"
        f.write_text("x" * 20000)
        content = _read_file_safe(str(f), max_chars=100)
        assert len(content) < 200
        assert "truncated" in content

    def test_nonexistent_file(self):
        content = _read_file_safe("/does/not/exist.py")
        assert content == ""

    def test_exact_max_chars(self, tmp_path):
        f = tmp_path / "exact.py"
        f.write_text("x" * 100)
        content = _read_file_safe(str(f), max_chars=100)
        assert content == "x" * 100
        assert "truncated" not in content


# ── TestWorkerInitialization ────────────────────────────────────────


class TestWorkerInitialization:
    def test_default_model(self):
        w = SecurityWorker(segment_id="seg-test")
        assert w.model == "minimax/minimax-m2.5"

    def test_custom_model(self):
        w = SecurityWorker(segment_id="seg-test", model="custom/model")
        assert w.model == "custom/model"

    def test_default_ai_provider(self):
        w = SecurityWorker(segment_id="seg-test")
        assert w.ai_provider == "openrouter_direct"

    def test_custom_ai_provider(self):
        w = SecurityWorker(segment_id="seg-test", ai_provider="claude")
        assert w.ai_provider == "claude"

    def test_segment_id_stored(self):
        w = QualityWorker(segment_id="seg-quality-test")
        assert w.segment_id == "seg-quality-test"


# ── TestParseJsonResponseHardened ──────────────────────────────────


class TestParseJsonResponseHardened:
    """Tests for M2.5-specific JSON parser hardening."""

    def test_strips_think_tags(self):
        raw = '<think>Let me analyze this code...</think>{"findings": [], "summary": "clean"}'
        result = _parse_json_response(raw)
        assert result["findings"] == []
        assert result["summary"] == "clean"

    def test_strips_multiline_think_tags(self):
        raw = (
            "<think>\nI need to check for SQL injection.\n"
            "Step 1: Look at queries...\n"
            "Step 2: Check sanitization...\n"
            "</think>\n"
            '{"findings": [{"id": "SEC-001"}], "summary": "found one"}'
        )
        result = _parse_json_response(raw)
        assert len(result["findings"]) == 1
        assert result["findings"][0]["id"] == "SEC-001"

    def test_handles_preamble_text(self):
        raw = 'Here is my analysis:\n\n{"findings": [], "summary": "no issues"}'
        result = _parse_json_response(raw)
        assert result["findings"] == []

    def test_handles_postamble_text(self):
        raw = '{"findings": [], "summary": "clean"}\n\nI hope this helps!'
        result = _parse_json_response(raw)
        assert result["findings"] == []

    def test_handles_think_tags_plus_code_fence(self):
        raw = (
            "<think>reasoning here</think>\n"
            "```json\n"
            '{"findings": [{"id": "F-1"}]}\n'
            "```"
        )
        result = _parse_json_response(raw)
        assert result["findings"][0]["id"] == "F-1"

    def test_handles_think_tags_plus_preamble(self):
        raw = (
            "<think>thinking...</think>\n"
            "Based on my analysis:\n"
            '{"findings": [], "summary": "clean"}'
        )
        result = _parse_json_response(raw)
        assert result["findings"] == []

    def test_nested_braces_in_json(self):
        raw = '{"findings": [{"locations": [{"file_path": "a.py"}]}]}'
        result = _parse_json_response(raw)
        assert result["findings"][0]["locations"][0]["file_path"] == "a.py"


# ── TestTruncateContents ──────────────────────────────────────────


class TestTruncateContents:
    """Tests for the M2.5 context budget truncation helper."""

    def test_all_fit(self):
        contents = {"a.py": "aaa", "b.py": "bbb"}
        result = _truncate_contents(contents, max_chars=100)
        assert result == contents

    def test_truncates_last_file(self):
        contents = {"a.py": "aaa", "b.py": "x" * 500}
        result = _truncate_contents(contents, max_chars=300)
        assert "a.py" in result
        assert "b.py" in result
        assert "truncated for context budget" in result["b.py"]

    def test_drops_file_when_budget_too_small(self):
        contents = {"a.py": "aaa", "b.py": "x" * 500}
        result = _truncate_contents(contents, max_chars=10)
        assert "a.py" in result
        assert "b.py" not in result

    def test_empty_contents(self):
        result = _truncate_contents({}, max_chars=100)
        assert result == {}


# ── TestWorkerPromptStructure ─────────────────────────────────────


class TestWorkerPromptStructure:
    """Tests that worker prompts contain required XML sections and content."""

    def test_security_has_xml_sections(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<role>" in prompt
        assert "</role>" in prompt
        assert "<methodology>" in prompt
        assert "</methodology>" in prompt
        assert "<evidence_requirements>" in prompt
        assert "<hard_exclusions>" in prompt
        assert "<severity_calibration>" in prompt
        assert "<output_format>" in prompt

    def test_security_has_sequential_steps(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "Step 1:" in prompt
        assert "Step 2:" in prompt
        assert "Step 3:" in prompt
        assert "Step 4:" in prompt
        assert "Step 5:" in prompt
        assert "Step 6:" in prompt

    def test_security_has_m25_json_instructions(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "first character must be {" in prompt
        assert "Do NOT wrap" in prompt

    def test_security_has_data_flow_field(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert '"data_flow"' in prompt

    def test_security_has_actionability_field(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert '"actionability"' in prompt
        assert "must_fix" in prompt
        assert "should_fix" in prompt

    def test_quality_has_hard_exclusions(self):
        w = QualityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<hard_exclusions>" in prompt
        assert "Missing repository/service abstraction" in prompt
        assert "5k LOC" in prompt
        assert "Magic numbers" in prompt

    def test_quality_has_evidence_requirements(self):
        w = QualityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<evidence_requirements>" in prompt
        assert "concrete consequence" in prompt.lower()

    def test_architecture_has_scale_awareness(self):
        w = ArchitectureWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<scale_awareness>" in prompt
        assert "3k LOC" in prompt
        assert "15k LOC" in prompt
        assert "Do NOT recommend" in prompt

    def test_architecture_has_evidence_requirements(self):
        w = ArchitectureWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<evidence_requirements>" in prompt
        assert "Could be better" in prompt


# ── TestWorkerContextInjection ────────────────────────────────────


class TestWorkerContextInjection:
    """Tests for pattern_context and project_context injection."""

    def test_security_pattern_context_in_prompt(self):
        ctx = "## Known Vulnerability Patterns\nVP-001: Client-writable authority"
        w = SecurityWorker(segment_id="seg-test", pattern_context=ctx)
        prompt = w.build_system_prompt()
        assert "VP-001" in prompt
        assert "Client-writable authority" in prompt

    def test_security_project_context_in_prompt(self):
        ctx = "## Project Context\nProject Stage: MVP"
        w = SecurityWorker(segment_id="seg-test", project_context=ctx)
        prompt = w.build_system_prompt()
        assert "Project Stage: MVP" in prompt

    def test_security_both_contexts_in_prompt(self):
        pattern = "## Patterns\nVP-001"
        project = "## Context\nMVP stage"
        w = SecurityWorker(segment_id="seg-test", pattern_context=pattern, project_context=project)
        prompt = w.build_system_prompt()
        assert "VP-001" in prompt
        assert "MVP stage" in prompt
        # project_context should appear before pattern_context
        assert prompt.index("MVP stage") < prompt.index("VP-001")

    def test_security_no_context_still_valid(self):
        w = SecurityWorker(segment_id="seg-test")
        prompt = w.build_system_prompt()
        assert "<role>" in prompt
        assert "</output_format>" in prompt

    def test_quality_project_context_in_prompt(self):
        ctx = "## Project Context\nTeam Size: 1"
        w = QualityWorker(segment_id="seg-test", project_context=ctx)
        prompt = w.build_system_prompt()
        assert "Team Size: 1" in prompt

    def test_architecture_project_context_in_prompt(self):
        ctx = "## Project Context\nProject Stage: early_product"
        w = ArchitectureWorker(segment_id="seg-test", project_context=ctx)
        prompt = w.build_system_prompt()
        assert "early_product" in prompt

    def test_task_prompt_includes_methodology_reference(self):
        w = SecurityWorker(segment_id="seg-test")
        ctx = SegmentContext(
            segment=Segment(id="seg-test", label="test", files=["app.py"]),
            file_contents={"app.py": "code"},
        )
        prompt = w.build_task_prompt(ctx, wave=1, repo_path="/tmp/repo")
        assert "step-by-step methodology" in prompt
        assert "data_flow" in prompt
        assert "confidence >= 0.7" in prompt
