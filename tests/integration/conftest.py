"""Conftest for integration tests.

Mocks the agentfield dependency so forge.execution.forge_executor can be
imported without installing the agentfield binary/package.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

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
