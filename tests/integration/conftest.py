"""Conftest for integration tests.

Mocks the agentfield dependency so forge.execution.forge_executor can be
imported without installing the agentfield binary/package.

Also provides the ``forge_telemetry`` fixture which:
  - Activates a ForgeTelemetry context for each live test
  - Prints a per-test cost summary to stdout
  - Appends a benchmark entry to ``.forge-benchmarks/cost_log.jsonl``
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Create a mock agentfield module before any test imports trigger it.
# forge.execution.forge_executor does `from agentfield import Agent` at
# module level, so we need this in sys.modules before collection.
# AgentRouter stub whose .reasoner() decorator is a no-op passthrough,
# so decorated functions keep their __name__ and can be registered.
class _MockAgentRouter:
    def __init__(self, **kwargs):
        pass

    def reasoner(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

_mock_agentfield = ModuleType("agentfield")
_mock_agentfield.Agent = MagicMock  # type: ignore[attr-defined]
_mock_agentfield.AgentRouter = _MockAgentRouter  # type: ignore[attr-defined]
sys.modules.setdefault("agentfield", _mock_agentfield)


# Also mock the escalation_agent prompts module if not present
try:
    from forge.prompts.escalation_agent import (
        ESCALATION_SYSTEM_PROMPT,
        build_escalation_task,
    )
except ImportError:
    _mock_prompts = ModuleType("forge.prompts.escalation_agent")
    _mock_prompts.ESCALATION_SYSTEM_PROMPT = "You are an escalation agent."  # type: ignore[attr-defined]
    _mock_prompts.build_escalation_task = lambda **kwargs: "Evaluate this finding."  # type: ignore[attr-defined]
    sys.modules.setdefault("forge.prompts.escalation_agent", _mock_prompts)


# ── Benchmark log directory ──────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BENCHMARK_DIR = _REPO_ROOT / ".forge-benchmarks"
_BENCHMARK_LOG = _BENCHMARK_DIR / "cost_log.jsonl"


def _get_git_sha() -> str:
    """Return the current short git SHA, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


@pytest.fixture(autouse=True)
def forge_telemetry(request):
    """Activate a ForgeTelemetry context for every test.

    For non-live tests this is essentially free (no LLM calls happen).
    For live tests, this captures real token counts and costs.

    After the test, it:
      1. Prints a cost summary line to stdout (visible with pytest -s)
      2. Appends a benchmark entry to .forge-benchmarks/cost_log.jsonl
    """
    from forge.execution.telemetry import ForgeTelemetry

    telemetry = ForgeTelemetry(run_id=request.node.nodeid)
    start = time.monotonic()

    with telemetry.activate():
        yield telemetry

    elapsed_s = round(time.monotonic() - start, 2)

    # Only log if there were actual invocations (skip pure unit tests)
    if not telemetry.invocations:
        return

    summary = telemetry.summary()
    cost = summary["total_cost_usd"]
    tokens = summary["total_tokens"]
    n_calls = summary["total_invocations"]

    # Print cost summary to stdout (visible with pytest -s or -v)
    print(
        f"\n  COST: ${cost:.4f} | {tokens:,} tokens | "
        f"{n_calls} calls | {elapsed_s}s",
    )
    if summary["cost_by_agent"]:
        for agent, agent_cost in summary["cost_by_agent"].items():
            print(f"    {agent}: ${agent_cost:.4f}")

    # Append to persistent benchmark log
    _BENCHMARK_DIR.mkdir(exist_ok=True)
    entry = {
        "test_name": request.node.nodeid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _get_git_sha(),
        "outcome": "passed" if not request.node.rep_call else (
            "passed" if request.node.rep_call.passed else "failed"
        ),
        "duration_s": elapsed_s,
        "total_cost_usd": cost,
        "total_tokens": tokens,
        "total_invocations": n_calls,
        "cost_by_agent": summary["cost_by_agent"],
        "cost_by_model": summary["cost_by_model"],
        "invocations": [
            {
                "agent": inv.agent_name,
                "model": inv.model,
                "input_tokens": inv.input_tokens,
                "output_tokens": inv.output_tokens,
                "cost_usd": inv.cost_usd,
                "latency_ms": inv.latency_ms,
            }
            for inv in telemetry.invocations
        ],
    }

    with open(_BENCHMARK_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test outcome on the item for the benchmark fixture."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)
