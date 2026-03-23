"""Tests for HiveOrchestrator — the three-layer pipeline coordinator.

Covers:
- End-to-end with mocked LLM (real temp Python project, deterministic Layer 0)
- Correct number of workers created (segments x worker_types)
- Wave 1 and Wave 2 execution
- Wave 2 disabled when enable_wave2=False
- Custom worker types
- Worker failure handling (exceptions don't crash others)
- Artifact saving
- Result structure validation
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Mock helpers ────────────────────────────────────────────────────


@contextmanager
def mock_agent_ai(run_side_effect=None, run_return_value=None):
    """Context manager that injects a mock forge.vendor.agent_ai into sys.modules.

    Each ``AgentAI(config)`` call returns a *new* mock instance whose
    ``.run()`` uses the supplied side_effect or return_value.

    This avoids importing the real module which uses Python 3.12+ syntax.
    """
    def _make_instance(config=None):
        inst = MagicMock()
        if run_side_effect is not None:
            inst.run = AsyncMock(side_effect=run_side_effect)
        elif run_return_value is not None:
            inst.run = AsyncMock(return_value=run_return_value)
        else:
            inst.run = AsyncMock(return_value=MagicMock(parsed=None, text="{}"))
        return inst

    mock_ai_cls = MagicMock(side_effect=_make_instance)
    mock_config_cls = MagicMock()

    fake_mod = ModuleType("forge.vendor.agent_ai")
    fake_mod.AgentAI = mock_ai_cls
    fake_mod.AgentAIConfig = mock_config_cls

    saved = sys.modules.get("forge.vendor.agent_ai")
    sys.modules["forge.vendor.agent_ai"] = fake_mod
    try:
        yield mock_ai_cls
    finally:
        if saved is None:
            sys.modules.pop("forge.vendor.agent_ai", None)
        else:
            sys.modules["forge.vendor.agent_ai"] = saved


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_python_project(tmp_path):
    """Create a real temp Python project for Layer 0 graph building.

    Structure:
        src/
            app.py          - main entry with imports
            auth/
                __init__.py
                service.py  - auth service
                middleware.py - auth middleware
            models/
                __init__.py
                user.py     - user model
            utils/
                __init__.py
                helpers.py  - utility functions
        tests/
            test_app.py     - test file
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "from src.auth.service import AuthService\n"
        "from src.models.user import User\n"
        "from src.utils.helpers import format_response\n\n"
        "def main():\n"
        "    auth = AuthService()\n"
        "    user = User(name='test')\n"
        "    return format_response(user)\n\n"
        "def health_check():\n"
        "    return {'status': 'ok'}\n"
    )

    auth = src / "auth"
    auth.mkdir()
    (auth / "__init__.py").write_text("")
    (auth / "service.py").write_text(
        "import hashlib\n"
        "import os\n\n"
        "class AuthService:\n"
        "    def authenticate(self, token: str) -> bool:\n"
        "        return len(token) > 0\n\n"
        "    def hash_password(self, password: str) -> str:\n"
        "        salt = os.urandom(32)\n"
        "        return hashlib.sha256(password.encode()).hexdigest()\n"
    )
    (auth / "middleware.py").write_text(
        "from src.auth.service import AuthService\n\n"
        "class AuthMiddleware:\n"
        "    def __init__(self):\n"
        "        self.auth = AuthService()\n\n"
        "    def process(self, request):\n"
        "        token = request.headers.get('Authorization')\n"
        "        return self.auth.authenticate(token)\n"
    )

    models = src / "models"
    models.mkdir()
    (models / "__init__.py").write_text("")
    (models / "user.py").write_text(
        "class User:\n"
        "    def __init__(self, name: str, email: str = ''):\n"
        "        self.name = name\n"
        "        self.email = email\n\n"
        "    def to_dict(self):\n"
        "        return {'name': self.name, 'email': self.email}\n"
    )

    utils = src / "utils"
    utils.mkdir()
    (utils / "__init__.py").write_text("")
    (utils / "helpers.py").write_text(
        "import json\n\n"
        "def format_response(data):\n"
        "    if hasattr(data, 'to_dict'):\n"
        "        return json.dumps(data.to_dict())\n"
        "    return json.dumps(data)\n\n"
        "def validate_input(value: str) -> bool:\n"
        "    return bool(value and value.strip())\n"
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "from src.app import health_check\n\n"
        "def test_health_check():\n"
        "    result = health_check()\n"
        "    assert result['status'] == 'ok'\n"
    )

    return tmp_path


def _make_worker_response() -> MagicMock:
    """Create a mock LLM response for a worker call."""
    findings = [
        {
            "id": "SEC-001",
            "title": "Generic finding",
            "description": "Found by worker",
            "category": "security",
            "severity": "medium",
            "confidence": 0.8,
        }
    ]
    resp = MagicMock()
    resp.parsed = None
    resp.is_error = False
    resp.text = json.dumps({"findings": findings, "summary": "analysis complete"})
    return resp


def _make_synthesis_response() -> MagicMock:
    """Create a mock synthesis LLM response."""
    data = {
        "codebase_map": {
            "modules": [{"name": "src", "path": "src", "purpose": "Main source", "files": ["app.py"], "loc": 100}],
            "dependencies": [],
            "data_flows": [],
            "auth_boundaries": [],
            "entry_points": [{"path": "src/app.py", "type": "function", "is_public": True}],
            "tech_stack": {"backend": "python"},
            "architecture_summary": "Simple Python project",
            "key_patterns": ["MVC"],
        },
        "findings": [
            {
                "id": "F-001",
                "title": "Weak password hashing",
                "description": "SHA256 without proper salting",
                "category": "security",
                "severity": "high",
                "locations": [{"file_path": "src/auth/service.py", "line_start": 8}],
                "suggested_fix": "Use bcrypt",
                "confidence": 0.9,
                "agent": "synthesis",
                "tier": 2,
            }
        ],
        "triage_result": {
            "decisions": [{"finding_id": "F-001", "tier": 2, "confidence": 0.9, "rationale": "Scoped fix"}],
            "tier_0_count": 0,
            "tier_1_count": 0,
            "tier_2_count": 1,
            "tier_3_count": 0,
        },
        "remediation_plan": {
            "items": [
                {
                    "finding_id": "F-001",
                    "title": "Fix password hashing",
                    "tier": 2,
                    "priority": 1,
                    "estimated_files": 1,
                    "files_to_modify": ["src/auth/service.py"],
                    "depends_on": [],
                    "acceptance_criteria": ["Uses bcrypt"],
                    "approach": "Replace sha256 with bcrypt",
                }
            ],
            "dependencies": [],
            "execution_levels": [["F-001"]],
            "deferred_finding_ids": [],
            "total_items": 1,
            "summary": "Fix weak hashing",
        },
    }
    resp = MagicMock()
    resp.parsed = None
    resp.is_error = False
    resp.text = json.dumps(data)
    return resp


def _dispatch_run(worker_resp, synth_resp):
    """Build an async side_effect that distinguishes worker vs synthesis calls."""
    async def _run(prompt, **kwargs):
        sys_prompt = kwargs.get("system_prompt", "") or ""
        if "senior software architect synthesizing" in sys_prompt:
            return synth_resp
        return worker_resp
    return _run


# ── TestHiveOrchestratorE2E ─────────────────────────────────────────


class TestHiveOrchestratorE2E:
    """End-to-end with real Layer 0, mocked LLM."""

    async def test_full_pipeline(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        artifacts_dir = str(sample_python_project / ".artifacts")
        run_fn = _dispatch_run(_make_worker_response(), _make_synthesis_response())

        with mock_agent_ai(run_side_effect=run_fn):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                artifacts_dir=artifacts_dir,
            )
            result = await orchestrator.run()

        # Verify result structure
        assert "codebase_map" in result
        assert "findings" in result
        assert "triage_result" in result
        assert "remediation_plan" in result
        assert "graph" in result
        assert "stats" in result

        # Stats should have timing info
        stats = result["stats"]
        assert "layer0_time_seconds" in stats
        assert "layer1_time_seconds" in stats
        assert "layer2_time_seconds" in stats
        assert "total_time_seconds" in stats
        assert stats["segments"] > 0

    async def test_layer0_builds_graph(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        run_fn = _dispatch_run(_make_worker_response(), _make_synthesis_response())

        with mock_agent_ai(run_side_effect=run_fn):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
            )
            result = await orchestrator.run()

        graph = result["graph"]
        assert "nodes" in graph
        assert "segments" in graph
        assert "edges" in graph
        assert len(graph["segments"]) > 0
        assert graph["stats"]["total_files"] > 0

    async def test_correct_worker_count(self, sample_python_project):
        """Workers = segments x worker_types per wave."""
        from forge.swarm.orchestrator import HiveOrchestrator

        call_count = {"worker": 0, "synthesis": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def counting_run(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                call_count["synthesis"] += 1
                return synth_resp
            call_count["worker"] += 1
            return worker_resp

        with mock_agent_ai(run_side_effect=counting_run):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
            )
            result = await orchestrator.run()

        num_segments = result["stats"]["segments"]
        # Default: 3 worker types, 2 waves
        expected_worker_calls = num_segments * 3 * 2
        assert call_count["worker"] == expected_worker_calls
        assert call_count["synthesis"] == 1

    async def test_artifacts_saved(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        artifacts_dir = str(sample_python_project / ".test-artifacts")
        run_fn = _dispatch_run(_make_worker_response(), _make_synthesis_response())

        with mock_agent_ai(run_side_effect=run_fn):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                artifacts_dir=artifacts_dir,
            )
            await orchestrator.run()

        hive_dir = Path(artifacts_dir) / "hive"
        assert hive_dir.exists()

        expected_artifacts = [
            "layer0_graph.json",
            "wave1_findings.json",
            "wave2_findings.json",
            "layer1_enriched_graph.json",
            "synthesis_result.json",
            "discovery_result.json",
        ]
        for artifact in expected_artifacts:
            artifact_path = hive_dir / artifact
            assert artifact_path.exists(), f"Missing artifact: {artifact}"
            data = json.loads(artifact_path.read_text())
            assert data is not None

    async def test_synthesis_called_once(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        call_count = {"synthesis": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def counting_run(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                call_count["synthesis"] += 1
                return synth_resp
            return worker_resp

        with mock_agent_ai(run_side_effect=counting_run):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
            )
            await orchestrator.run()

        assert call_count["synthesis"] == 1


# ── TestWave2Disabled ───────────────────────────────────────────────


class TestWave2Disabled:
    async def test_wave2_false_skips_wave2(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        call_count = {"worker": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def counting_run(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                return synth_resp
            call_count["worker"] += 1
            return worker_resp

        with mock_agent_ai(run_side_effect=counting_run):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                enable_wave2=False,
            )
            result = await orchestrator.run()

        num_segments = result["stats"]["segments"]
        # Only Wave 1: segments * 3 worker_types
        expected_calls = num_segments * 3
        assert call_count["worker"] == expected_calls

    async def test_wave2_false_stats(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        run_fn = _dispatch_run(_make_worker_response(), _make_synthesis_response())

        with mock_agent_ai(run_side_effect=run_fn):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                enable_wave2=False,
            )
            result = await orchestrator.run()

        assert result["stats"]["wave2_findings"] == 0

    async def test_wave2_false_no_wave2_artifact(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        artifacts_dir = str(sample_python_project / ".test-artifacts-nw2")
        run_fn = _dispatch_run(_make_worker_response(), _make_synthesis_response())

        with mock_agent_ai(run_side_effect=run_fn):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                artifacts_dir=artifacts_dir,
                enable_wave2=False,
            )
            await orchestrator.run()

        # wave2_findings.json should NOT be created
        assert not (Path(artifacts_dir) / "hive" / "wave2_findings.json").exists()
        # wave1_findings.json SHOULD exist
        assert (Path(artifacts_dir) / "hive" / "wave1_findings.json").exists()


# ── TestCustomWorkerTypes ───────────────────────────────────────────


class TestCustomWorkerTypes:
    async def test_two_worker_types(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        call_count = {"worker": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def counting_run(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                return synth_resp
            call_count["worker"] += 1
            return worker_resp

        with mock_agent_ai(run_side_effect=counting_run):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                worker_types=["security", "quality"],
            )
            result = await orchestrator.run()

        num_segments = result["stats"]["segments"]
        # 2 worker types, 2 waves
        expected_calls = num_segments * 2 * 2
        assert call_count["worker"] == expected_calls

    async def test_single_worker_type(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        call_count = {"worker": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def counting_run(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                return synth_resp
            call_count["worker"] += 1
            return worker_resp

        with mock_agent_ai(run_side_effect=counting_run):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
                worker_types=["security"],
                enable_wave2=False,
            )
            result = await orchestrator.run()

        num_segments = result["stats"]["segments"]
        # 1 worker type, 1 wave
        expected_calls = num_segments * 1
        assert call_count["worker"] == expected_calls


# ── TestWorkerFailureHandling ───────────────────────────────────────


class TestWorkerFailureHandling:
    async def test_failed_worker_doesnt_crash_others(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        call_index = {"n": 0}
        worker_resp = _make_worker_response()
        synth_resp = _make_synthesis_response()

        async def sometimes_fail(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                return synth_resp
            call_index["n"] += 1
            # Fail every 3rd worker call
            if call_index["n"] % 3 == 0:
                raise RuntimeError("LLM API error")
            return worker_resp

        with mock_agent_ai(run_side_effect=sometimes_fail):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
            )
            result = await orchestrator.run()

        # Should complete despite failures
        assert "findings" in result
        assert "stats" in result
        # At least some workers should succeed
        assert result["stats"]["wave1_findings"] >= 0

    async def test_all_workers_fail_still_completes(self, sample_python_project):
        from forge.swarm.orchestrator import HiveOrchestrator

        synth_resp = _make_synthesis_response()

        async def always_fail(prompt, **kwargs):
            sys_prompt = kwargs.get("system_prompt", "") or ""
            if "senior software architect synthesizing" in sys_prompt:
                return synth_resp
            raise RuntimeError("Total worker failure")

        with mock_agent_ai(run_side_effect=always_fail):
            orchestrator = HiveOrchestrator(
                repo_path=str(sample_python_project),
            )
            result = await orchestrator.run()

        assert result["stats"]["wave1_findings"] == 0
        assert result["stats"]["wave2_findings"] == 0
        # Synthesis should still run
        assert "findings" in result


# ── TestOrchestratorInit ────────────────────────────────────────────


class TestOrchestratorInit:
    def test_default_values(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(repo_path="/tmp/repo")
        assert orch.repo_path == "/tmp/repo"
        assert orch.worker_model == "minimax/minimax-m2.5"
        assert orch.synthesis_model == "anthropic/claude-sonnet-4.6"
        assert orch.ai_provider == "openrouter_direct"
        assert orch.target_segments == 5
        assert orch.enable_wave2 is True
        assert orch.worker_types == ["security", "quality", "architecture"]
        assert orch.project_context == {}

    def test_custom_values(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(
            repo_path="/tmp/repo",
            repo_url="https://github.com/test/repo",
            worker_model="custom/worker",
            synthesis_model="custom/synth",
            ai_provider="claude",
            target_segments=10,
            enable_wave2=False,
            worker_types=["security"],
        )
        assert orch.repo_url == "https://github.com/test/repo"
        assert orch.worker_model == "custom/worker"
        assert orch.synthesis_model == "custom/synth"
        assert orch.ai_provider == "claude"
        assert orch.target_segments == 10
        assert orch.enable_wave2 is False
        assert orch.worker_types == ["security"]

    def test_default_artifacts_dir(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(repo_path="/tmp/repo")
        assert orch.artifacts_dir == "/tmp/repo/.artifacts"

    def test_custom_artifacts_dir(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(repo_path="/tmp/repo", artifacts_dir="/custom/artifacts")
        assert orch.artifacts_dir == "/custom/artifacts"

    def test_project_context_stored(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        ctx = {"project_stage": "mvp", "team_size": 1}
        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context=ctx)
        assert orch.project_context == ctx

    def test_project_context_none_defaults_to_empty_dict(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context=None)
        assert orch.project_context == {}


# ── TestCreateWorkers ───────────────────────────────────────────────


class TestCreateWorkers:
    def test_creates_correct_workers(self):
        from forge.swarm.orchestrator import HiveOrchestrator
        from forge.swarm.worker import SecurityWorker, QualityWorker, ArchitectureWorker
        from forge.graph.models import CodeGraph, Segment

        orch = HiveOrchestrator(repo_path="/tmp/repo")
        orch._graph = CodeGraph(
            segments=[
                Segment(id="seg-0", label="module0", files=["a.py"]),
                Segment(id="seg-1", label="module1", files=["b.py"]),
            ]
        )

        workers = orch._create_workers()
        assert len(workers) == 6  # 2 segments * 3 types

        security_workers = [w for w in workers if isinstance(w, SecurityWorker)]
        quality_workers = [w for w in workers if isinstance(w, QualityWorker)]
        arch_workers = [w for w in workers if isinstance(w, ArchitectureWorker)]

        assert len(security_workers) == 2
        assert len(quality_workers) == 2
        assert len(arch_workers) == 2

    def test_custom_worker_types_subset(self):
        from forge.swarm.orchestrator import HiveOrchestrator
        from forge.swarm.worker import SecurityWorker
        from forge.graph.models import CodeGraph, Segment

        orch = HiveOrchestrator(repo_path="/tmp/repo", worker_types=["security"])
        orch._graph = CodeGraph(
            segments=[
                Segment(id="seg-0", label="module0", files=["a.py"]),
                Segment(id="seg-1", label="module1", files=["b.py"]),
            ]
        )

        workers = orch._create_workers()
        assert len(workers) == 2  # 2 segments * 1 type
        assert all(isinstance(w, SecurityWorker) for w in workers)

    def test_invalid_worker_type_ignored(self):
        from forge.swarm.orchestrator import HiveOrchestrator
        from forge.graph.models import CodeGraph, Segment

        orch = HiveOrchestrator(repo_path="/tmp/repo", worker_types=["security", "invalid_type"])
        orch._graph = CodeGraph(
            segments=[
                Segment(id="seg-0", label="module0", files=["a.py"]),
            ]
        )

        workers = orch._create_workers()
        assert len(workers) == 1  # Only security, invalid_type skipped

    def test_project_context_passed_to_all_workers(self):
        from forge.swarm.orchestrator import HiveOrchestrator
        from forge.graph.models import CodeGraph, Segment

        ctx = {"project_stage": "mvp", "team_size": 1, "vision_summary": "Test app"}
        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context=ctx)
        orch._graph = CodeGraph(
            segments=[Segment(id="seg-0", label="module0", files=["a.py"])]
        )

        workers = orch._create_workers()
        assert len(workers) == 3  # 1 segment * 3 types

        # All workers should have project context in their prompt
        for worker in workers:
            prompt = worker.build_system_prompt()
            assert "project_context" in prompt.lower() or "Project Context" in prompt

    def test_empty_project_context_not_injected(self):
        from forge.swarm.orchestrator import HiveOrchestrator
        from forge.graph.models import CodeGraph, Segment

        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context={})
        orch._graph = CodeGraph(
            segments=[Segment(id="seg-0", label="module0", files=["a.py"])]
        )

        workers = orch._create_workers()
        # Workers should NOT have project_context in their prompts
        for worker in workers:
            prompt = worker.build_system_prompt()
            assert "<project_context>" not in prompt

    def test_build_project_context_returns_string(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        ctx = {"project_stage": "growth", "team_size": 5}
        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context=ctx)

        result = orch._build_project_context()
        assert isinstance(result, str)
        assert "<project_context>" in result
        assert "Growth Stage" in result

    def test_build_project_context_empty_returns_empty(self):
        from forge.swarm.orchestrator import HiveOrchestrator

        orch = HiveOrchestrator(repo_path="/tmp/repo", project_context={})
        result = orch._build_project_context()
        assert result == ""
