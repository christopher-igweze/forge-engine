"""Integration tests for the Hive Discovery pipeline.

Tests the full Layer 0 -> Layer 1 -> Layer 2 pipeline with mocked LLM
calls, as well as feature flag routing and standalone dispatcher registration.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.graph.models import CodeGraph, NodeKind
from forge.schemas import ForgeExecutionState, TriageResult, RemediationPlan
from forge.swarm.orchestrator import HiveOrchestrator


# ── Mock LLM response helpers ──────────────────────────────────────────


def _make_mock_response(json_data: dict) -> MagicMock:
    """Create a mock AgentAI response object.

    Mimics forge.vendor.agent_ai.types.AgentResponse with .text, .parsed,
    and .is_error attributes.
    """
    resp = MagicMock()
    resp.text = json.dumps(json_data)
    resp.parsed = None
    resp.is_error = False
    return resp


def _worker_response() -> MagicMock:
    """Mock worker LLM response with findings."""
    return _make_mock_response({
        "findings": [
            {
                "id": "MOCK-001",
                "title": "Mock finding from worker",
                "description": "Detected a mock issue in the segment",
                "category": "security",
                "severity": "medium",
                "locations": [{"file_path": "src/app.py"}],
                "suggested_fix": "Fix the mock issue",
                "confidence": 0.8,
            },
        ],
        "summary": "Mock worker analysis complete",
    })


def _synthesis_response() -> MagicMock:
    """Mock synthesizer LLM response with full schema."""
    return _make_mock_response({
        "codebase_map": {
            "modules": [{"name": "src", "path": "src/", "purpose": "Main source", "files": ["src/app.py"], "loc": 50}],
            "dependencies": [],
            "data_flows": [],
            "auth_boundaries": [],
            "entry_points": [],
            "tech_stack": {},
            "architecture_summary": "Simple Python app",
            "key_patterns": [],
            "files": [{"path": "src/app.py", "language": "python", "loc": 50}],
            "loc_total": 50,
            "file_count": 1,
            "primary_language": "python",
            "languages": ["python"],
        },
        "findings": [
            {
                "id": "F-synth001",
                "title": "Missing input validation",
                "description": "User input is not validated",
                "category": "security",
                "severity": "high",
                "locations": [{"file_path": "src/app.py", "line_start": 10}],
                "suggested_fix": "Add input validation",
                "confidence": 0.9,
                "agent": "synthesis",
                "tier": 2,
            },
        ],
        "triage_result": {
            "decisions": [
                {
                    "finding_id": "F-synth001",
                    "tier": 2,
                    "confidence": 0.9,
                    "rationale": "Scoped fix affecting one file",
                },
            ],
            "tier_0_count": 0,
            "tier_1_count": 0,
            "tier_2_count": 1,
            "tier_3_count": 0,
        },
        "remediation_plan": {
            "items": [
                {
                    "finding_id": "F-synth001",
                    "title": "Add input validation",
                    "tier": 2,
                    "priority": 1,
                    "estimated_files": 1,
                },
            ],
            "execution_levels": [["F-synth001"]],
            "total_items": 1,
            "summary": "One fix needed",
        },
    })


def _create_test_repo(tmp_path: Path) -> str:
    """Create a minimal Python project structure in tmp_path."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Main app file
    (src_dir / "app.py").write_text(
        '"""Main application module."""\n'
        "\n"
        "import os\n"
        "from src.utils import helper\n"
        "\n"
        "\n"
        "def main():\n"
        '    user_input = input("Enter: ")\n'
        "    result = helper(user_input)\n"
        "    print(result)\n"
        "\n"
        "\n"
        "class AppConfig:\n"
        '    """Application configuration."""\n'
        "\n"
        "    def __init__(self):\n"
        '        self.debug = os.getenv("DEBUG", "false")\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )

    # Utils file
    (src_dir / "utils.py").write_text(
        '"""Utility functions."""\n'
        "\n"
        "\n"
        "def helper(data):\n"
        '    """Process data without validation."""\n'
        "    return data.upper()\n"
        "\n"
        "\n"
        "def sanitize(text):\n"
        '    """Sanitize input text."""\n'
        "    return text.strip()\n"
    )

    # Models file
    (src_dir / "models.py").write_text(
        '"""Data models."""\n'
        "\n"
        "\n"
        "class User:\n"
        "    def __init__(self, name, email):\n"
        "        self.name = name\n"
        "        self.email = email\n"
        "\n"
        "\n"
        "class Session:\n"
        "    def __init__(self, user):\n"
        "        self.user = user\n"
        "        self.active = True\n"
    )

    # __init__.py
    (src_dir / "__init__.py").write_text("")

    return str(tmp_path)


# ── Test 1: Full Pipeline Integration Test ──────────────────────────────


class TestHiveDiscoveryFullPipeline:
    """Full pipeline: Layer 0 (real) -> Layer 1 (mocked LLM) -> Layer 2 (mocked LLM)."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self, tmp_path):
        repo_path = _create_test_repo(tmp_path)
        artifacts_dir = str(tmp_path / ".artifacts")

        # Configure with 2 segments (small repo), all 3 worker types, wave2 enabled
        orchestrator = HiveOrchestrator(
            repo_path=repo_path,
            artifacts_dir=artifacts_dir,
            worker_model="minimax/minimax-m2.5",
            synthesis_model="anthropic/claude-sonnet-4.6",
            target_segments=2,
            enable_wave2=True,
            worker_types=["security", "quality", "architecture"],
        )

        # Mock both worker and synthesizer AgentAI calls.
        # AgentAI is lazily imported inside analyze()/synthesize() via
        # ``from forge.vendor.agent_ai import AgentAI``.  We patch the
        # module-level attribute so the lazy import picks up our mock.
        mock_worker_ai = AsyncMock(return_value=_worker_response())
        mock_synth_ai = AsyncMock(return_value=_synthesis_response())

        # Track calls to distinguish worker from synthesizer.  Worker
        # analyze() uses model=self.model (minimax), synthesizer uses
        # model=self.model (sonnet).  We install a side_effect on the
        # shared mock that routes based on the AgentAIConfig's model.
        call_log: list[str] = []

        def _make_ai(config):
            """Factory that returns a mock with run() bound to worker or synth."""
            mock = MagicMock()
            model = getattr(config, "model", "")
            if "sonnet" in str(model):
                mock.run = mock_synth_ai
                call_log.append("synthesizer")
            else:
                mock.run = mock_worker_ai
                call_log.append("worker")
            return mock

        with patch("forge.vendor.agent_ai.AgentAI", side_effect=_make_ai), \
             patch("forge.vendor.agent_ai.AgentAIConfig", side_effect=lambda **kw: MagicMock(**kw)):

            result = await orchestrator.run()

        # ── Layer 0 verification: real code graph was built ──
        graph = orchestrator._graph
        assert isinstance(graph, CodeGraph)

        # Should have file nodes
        file_nodes = [n for n in graph.nodes.values() if n.kind == NodeKind.FILE]
        assert len(file_nodes) >= 3, f"Expected >= 3 file nodes, got {len(file_nodes)}"

        # Should have segments
        assert len(graph.segments) >= 1, "Expected at least 1 segment"

        # ── Layer 1 verification: workers were called ──
        num_segments = len(graph.segments)
        num_worker_types = 3  # security, quality, architecture
        num_waves = 2  # wave1 + wave2
        expected_worker_calls = num_segments * num_worker_types * num_waves

        assert mock_worker_ai.call_count == expected_worker_calls, (
            f"Expected {expected_worker_calls} worker calls "
            f"({num_segments} segments x {num_worker_types} types x {num_waves} waves), "
            f"got {mock_worker_ai.call_count}"
        )

        # ── Layer 2 verification: synthesis was called once ──
        assert mock_synth_ai.call_count == 1, (
            f"Expected 1 synthesis call, got {mock_synth_ai.call_count}"
        )

        # ── Final result has all required keys ──
        assert "codebase_map" in result
        assert "findings" in result
        assert "triage_result" in result
        assert "remediation_plan" in result
        assert "graph" in result
        assert "stats" in result

        # Verify findings from synthesis
        assert len(result["findings"]) >= 1
        assert result["findings"][0]["id"] == "F-synth001"

        # Verify stats show correct invocation counts
        stats = result["stats"]
        assert stats["total_invocations"] == expected_worker_calls + 1  # workers + synthesizer
        assert stats["segments"] == num_segments

    @pytest.mark.asyncio
    async def test_pipeline_without_wave2(self, tmp_path):
        """Pipeline with wave2 disabled should have fewer worker calls."""
        repo_path = _create_test_repo(tmp_path)
        artifacts_dir = str(tmp_path / ".artifacts")

        orchestrator = HiveOrchestrator(
            repo_path=repo_path,
            artifacts_dir=artifacts_dir,
            target_segments=2,
            enable_wave2=False,
            worker_types=["security", "quality"],
        )

        mock_worker_ai = AsyncMock(return_value=_worker_response())
        mock_synth_ai = AsyncMock(return_value=_synthesis_response())

        def _make_ai(config):
            mock = MagicMock()
            model = getattr(config, "model", "")
            if "sonnet" in str(model):
                mock.run = mock_synth_ai
            else:
                mock.run = mock_worker_ai
            return mock

        with patch("forge.vendor.agent_ai.AgentAI", side_effect=_make_ai), \
             patch("forge.vendor.agent_ai.AgentAIConfig", side_effect=lambda **kw: MagicMock(**kw)):

            result = await orchestrator.run()

        num_segments = len(orchestrator._graph.segments)
        num_worker_types = 2  # security, quality only
        # Wave 2 disabled: only 1 wave
        expected_worker_calls = num_segments * num_worker_types * 1

        assert mock_worker_ai.call_count == expected_worker_calls
        assert result["stats"]["wave2_findings"] == 0


# ── Test 2: Feature Flag — swarm mode calls run_hive_discovery ──────────


class TestFeatureFlagSwarmMode:
    """ForgeConfig with discovery_mode='swarm' routes to run_hive_discovery."""

    @pytest.mark.asyncio
    async def test_swarm_mode_calls_hive_discovery(self):
        cfg = ForgeConfig(discovery_mode="swarm")
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        # Mock app.call to track which reasoner is dispatched
        mock_app = MagicMock()
        call_targets = []

        async def _track_call(target, **kwargs):
            call_targets.append(target)
            # Return a valid hive result
            return {
                "codebase_map": {"files": [], "loc_total": 0, "file_count": 0, "primary_language": "", "languages": []},
                "findings": [],
                "triage_result": {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0},
                "remediation_plan": {"items": [], "execution_levels": [], "total_items": 0},
                "graph": {},
                "stats": {"total_invocations": 1},
            }

        mock_app.call = AsyncMock(side_effect=_track_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        # Verify the call target contains run_hive_discovery
        assert len(call_targets) == 1
        assert "run_hive_discovery" in call_targets[0]


# ── Test 3: Classic Mode Unaffected ──────────────────────────────────────


class TestClassicModeUnaffected:
    """ForgeConfig with discovery_mode='classic' still calls run_codebase_analyst."""

    @pytest.mark.asyncio
    async def test_classic_mode_calls_codebase_analyst(self):
        cfg = ForgeConfig(discovery_mode="classic")
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        # Mock app.call to track which reasoners are dispatched
        mock_app = MagicMock()
        call_targets = []

        async def _track_call(target, **kwargs):
            call_targets.append(target)
            # Agent 1 returns a codebase map dict
            if "codebase_analyst" in target:
                return {
                    "files": [{"path": "src/app.ts", "language": "typescript", "loc": 100}],
                    "loc_total": 100,
                    "file_count": 1,
                    "primary_language": "typescript",
                    "languages": ["typescript"],
                }
            # Agents 2-4 return findings
            return {"findings": []}

        mock_app.call = AsyncMock(side_effect=_track_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        # Should have called 4 agents: codebase_analyst, security, quality, architecture
        assert len(call_targets) == 4

        # First call should be run_codebase_analyst (not run_hive_discovery)
        assert "run_codebase_analyst" in call_targets[0]
        assert not any("run_hive_discovery" in t for t in call_targets)

        # Should have the parallel audit agents
        agent_names = [t.rsplit(".", 1)[-1] for t in call_targets]
        assert "run_codebase_analyst" in agent_names
        assert "run_security_auditor" in agent_names
        assert "run_quality_auditor" in agent_names
        assert "run_architecture_reviewer" in agent_names


# ── Test 4: Triage Skip in Swarm Mode ────────────────────────────────────


class TestTriageSkipInSwarmMode:
    """When swarm mode produces triage_result and remediation_plan, _run_triage returns 0."""

    @pytest.mark.asyncio
    async def test_triage_skip_when_swarm_produced_results(self):
        from forge.app import _run_triage

        cfg = ForgeConfig(discovery_mode="swarm")
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        # Simulate that swarm discovery already populated triage + plan
        from forge.schemas import (
            AuditFinding,
            FindingCategory,
            FindingLocation,
            FindingSeverity,
            RemediationItem,
            RemediationTier,
            TriageDecision,
        )

        finding = AuditFinding(
            id="F-swarm001",
            title="Swarm finding",
            description="Found by swarm",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[FindingLocation(file_path="src/app.py")],
        )
        state.all_findings = [finding]

        state.triage_result = TriageResult(
            decisions=[TriageDecision(
                finding_id="F-swarm001",
                tier=RemediationTier.TIER_2,
                confidence=0.9,
                rationale="Scoped fix",
            )],
            tier_0_count=0,
            tier_1_count=0,
            tier_2_count=1,
            tier_3_count=0,
        )

        state.remediation_plan = RemediationPlan(
            items=[RemediationItem(
                finding_id="F-swarm001",
                title="Fix swarm finding",
                tier=RemediationTier.TIER_2,
                priority=1,
            )],
            execution_levels=[["F-swarm001"]],
            total_items=1,
        )

        mock_app = MagicMock()
        mock_app.call = AsyncMock()

        result = await _run_triage(mock_app, state, cfg, resolved)

        # Should skip triage — 0 invocations, app.call never called
        assert result["invocations"] == 0
        mock_app.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_runs_when_classic_mode(self):
        """In classic mode, triage runs normally even with findings."""
        from forge.app import _run_triage

        cfg = ForgeConfig(discovery_mode="classic")
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        from forge.schemas import (
            AuditFinding,
            FindingCategory,
            FindingLocation,
            FindingSeverity,
        )

        state.all_findings = [AuditFinding(
            id="F-classic001",
            title="Classic finding",
            description="Found by classic agents",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
            locations=[FindingLocation(file_path="src/app.ts")],
        )]

        mock_app = MagicMock()
        # Triage classifier and fix strategist each return a dict
        mock_app.call = AsyncMock(side_effect=[
            {
                "decisions": [{"finding_id": "F-classic001", "tier": 2, "confidence": 0.8, "rationale": "Scoped"}],
                "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 1, "tier_3_count": 0,
            },
            {
                "items": [{"finding_id": "F-classic001", "title": "Fix it", "tier": 2, "priority": 1}],
                "execution_levels": [["F-classic001"]],
                "total_items": 1,
            },
        ])

        result = await _run_triage(mock_app, state, cfg, resolved)

        # Classic mode should run triage (2 agent calls: classifier + strategist)
        assert result["invocations"] == 2
        assert mock_app.call.call_count == 2


# ── Test 5: StandaloneDispatcher Registration ────────────────────────────


class TestProjectContextThreading:
    """Project context is threaded through both swarm and classic discovery modes."""

    @pytest.mark.asyncio
    async def test_swarm_mode_passes_project_context_to_hive(self):
        """Swarm mode forwards cfg.project_context to run_hive_discovery."""
        ctx = {"project_stage": "mvp", "team_size": 1}
        cfg = ForgeConfig(discovery_mode="swarm", project_context=ctx)
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        mock_app = MagicMock()
        captured_kwargs = {}

        async def _capture_call(target, **kwargs):
            captured_kwargs.update(kwargs)
            return {
                "codebase_map": {"files": [], "loc_total": 0, "file_count": 0, "primary_language": "", "languages": []},
                "findings": [],
                "triage_result": {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0},
                "remediation_plan": {"items": [], "execution_levels": [], "total_items": 0},
                "graph": {},
                "stats": {"total_invocations": 1},
            }

        mock_app.call = AsyncMock(side_effect=_capture_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        assert "project_context" in captured_kwargs
        assert captured_kwargs["project_context"] == ctx

    @pytest.mark.asyncio
    async def test_classic_mode_passes_project_context_to_security_auditor(self):
        """Classic mode builds project context string and passes to security auditor."""
        ctx = {"project_stage": "growth", "team_size": 5, "vision_summary": "E-commerce"}
        cfg = ForgeConfig(discovery_mode="classic", project_context=ctx)
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        mock_app = MagicMock()
        security_kwargs = {}

        async def _track_call(target, **kwargs):
            if "security_auditor" in target:
                security_kwargs.update(kwargs)
            if "codebase_analyst" in target:
                return {
                    "files": [{"path": "src/app.ts", "language": "typescript", "loc": 100}],
                    "loc_total": 100,
                    "file_count": 1,
                    "primary_language": "typescript",
                    "languages": ["typescript"],
                }
            return {"findings": []}

        mock_app.call = AsyncMock(side_effect=_track_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        assert "project_context" in security_kwargs
        # Should be a built string, not a raw dict
        assert isinstance(security_kwargs["project_context"], str)
        assert "<project_context>" in security_kwargs["project_context"]
        assert "Growth Stage" in security_kwargs["project_context"]

    @pytest.mark.asyncio
    async def test_actionability_applied_after_classic_discovery(self):
        """Actionability classification runs after classic discovery merges findings."""
        from forge.schemas import AuditFinding, FindingCategory, FindingSeverity

        cfg = ForgeConfig(
            discovery_mode="classic",
            project_context={"project_stage": "mvp", "team_size": 1},
        )
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        mock_app = MagicMock()

        async def _track_call(target, **kwargs):
            if "codebase_analyst" in target:
                return {
                    "files": [], "loc_total": 0, "file_count": 0,
                    "primary_language": "", "languages": [],
                }
            if "security_auditor" in target:
                return {"findings": [
                    {
                        "id": "F-001",
                        "title": "SQL injection",
                        "description": "Unsanitized input in query",
                        "category": "security",
                        "severity": "critical",
                        "confidence": 0.95,
                        "agent": "security_auditor",
                    }
                ]}
            return {"findings": []}

        mock_app.call = AsyncMock(side_effect=_track_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        # Findings should have actionability set
        assert len(state.all_findings) >= 1
        assert state.all_findings[0].actionability == "must_fix"

    @pytest.mark.asyncio
    async def test_actionability_applied_after_swarm_discovery(self):
        """Actionability classification runs after swarm discovery."""
        cfg = ForgeConfig(
            discovery_mode="swarm",
            project_context={"project_stage": "enterprise", "team_size": 20},
        )
        resolved = cfg.resolved_models()

        state = ForgeExecutionState(
            repo_path="/tmp/test-repo",
            artifacts_dir="/tmp/test-artifacts",
        )

        mock_app = MagicMock()

        async def _track_call(target, **kwargs):
            return {
                "codebase_map": {"files": [], "loc_total": 0, "file_count": 0, "primary_language": "", "languages": []},
                "findings": [
                    {
                        "id": "F-swarm-001",
                        "title": "Missing auth check",
                        "description": "No authentication on admin endpoint",
                        "category": "security",
                        "severity": "high",
                        "confidence": 0.9,
                        "agent": "swarm_worker",
                    }
                ],
                "triage_result": {"decisions": [], "tier_0_count": 0, "tier_1_count": 0, "tier_2_count": 0, "tier_3_count": 0},
                "remediation_plan": {"items": [], "execution_levels": [], "total_items": 0},
                "graph": {},
                "stats": {"total_invocations": 1},
            }

        mock_app.call = AsyncMock(side_effect=_track_call)

        from forge.app import _run_discovery
        await _run_discovery(mock_app, state, cfg, resolved)

        assert len(state.all_findings) >= 1
        # Enterprise stage + high severity + high confidence → must_fix
        assert state.all_findings[0].actionability == "must_fix"


class TestStandaloneDispatcherRegistry:
    """run_hive_discovery is registered in the StandaloneDispatcher registry."""

    def test_hive_discovery_registered(self):
        from forge.standalone import StandaloneDispatcher

        dispatcher = StandaloneDispatcher()
        assert "run_hive_discovery" in dispatcher._registry

    def test_hive_discovery_callable(self):
        from forge.standalone import StandaloneDispatcher

        dispatcher = StandaloneDispatcher()
        fn = dispatcher._registry["run_hive_discovery"]
        assert callable(fn)

    @pytest.mark.asyncio
    async def test_dispatcher_resolves_hive_discovery(self):
        """StandaloneDispatcher.call resolves the hive discovery target."""
        from forge.standalone import StandaloneDispatcher

        dispatcher = StandaloneDispatcher()

        # Mock the actual function to avoid running the real pipeline
        mock_fn = AsyncMock(return_value={"findings": [], "stats": {"total_invocations": 0}})
        dispatcher._registry["run_hive_discovery"] = mock_fn

        result = await dispatcher.call(
            "forge-engine.run_hive_discovery",
            repo_path="/tmp/test",
        )

        mock_fn.assert_called_once_with(repo_path="/tmp/test")
        assert result == {"findings": [], "stats": {"total_invocations": 0}}
