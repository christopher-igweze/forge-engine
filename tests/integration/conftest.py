"""Conftest for integration tests.

Mocks the agentfield dependency so forge.execution.forge_executor can be
imported without installing the agentfield binary/package.

Also mocks forge.vendor.agent_ai if the types module cannot be imported
(Python <3.12 — AgentResponse uses PEP 695 generic syntax).
"""

from __future__ import annotations

import sys
from enum import Enum
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

# Mock forge.vendor.agent_ai if it cannot be imported natively.
# On Python <3.12, the types module uses `class AgentResponse[T]` (PEP 695)
# which is a SyntaxError. We install mock modules so that code which does
# `from forge.vendor.agent_ai import AgentAI, AgentAIConfig` at module
# level (e.g. forge.reasoners.discovery) can be imported without error.
try:
    from forge.vendor.agent_ai.types import AgentResponse  # noqa: F401
except SyntaxError:
    # Stub Tool enum matching forge.vendor.agent_ai.types.Tool so that
    # module-level attribute access like ``Tool.READ`` works.
    class _ToolStub(str, Enum):
        READ = "Read"
        WRITE = "Write"
        EDIT = "Edit"
        BASH = "Bash"
        GLOB = "Glob"
        GREP = "Grep"
        NOTEBOOK_EDIT = "NotebookEdit"
        TASK = "Task"
        WEB_FETCH = "WebFetch"
        WEB_SEARCH = "WebSearch"

    # Build a mock module tree for forge.vendor.agent_ai
    _mock_types = ModuleType("forge.vendor.agent_ai.types")
    _mock_types.AgentResponse = MagicMock  # type: ignore[attr-defined]
    _mock_types.ClaudeResponse = MagicMock  # type: ignore[attr-defined]
    _mock_types.Message = MagicMock  # type: ignore[attr-defined]
    _mock_types.Metrics = MagicMock  # type: ignore[attr-defined]
    _mock_types.TextContent = MagicMock  # type: ignore[attr-defined]
    _mock_types.ThinkingContent = MagicMock  # type: ignore[attr-defined]
    _mock_types.Tool = _ToolStub  # type: ignore[attr-defined]
    _mock_types.ToolResultContent = MagicMock  # type: ignore[attr-defined]
    _mock_types.ToolUseContent = MagicMock  # type: ignore[attr-defined]
    sys.modules["forge.vendor.agent_ai.types"] = _mock_types

    _mock_base = ModuleType("forge.vendor.agent_ai.providers.base")
    _mock_base.ProviderClient = MagicMock  # type: ignore[attr-defined]
    sys.modules["forge.vendor.agent_ai.providers.base"] = _mock_base

    _mock_providers = ModuleType("forge.vendor.agent_ai.providers")
    sys.modules.setdefault("forge.vendor.agent_ai.providers", _mock_providers)

    _mock_factory = ModuleType("forge.vendor.agent_ai.factory")
    _mock_factory.build_provider_client = MagicMock  # type: ignore[attr-defined]
    sys.modules["forge.vendor.agent_ai.factory"] = _mock_factory

    _mock_client = ModuleType("forge.vendor.agent_ai.client")
    _mock_client.AgentAI = MagicMock  # type: ignore[attr-defined]
    _mock_client.AgentAIConfig = MagicMock  # type: ignore[attr-defined]
    _mock_client.ClaudeAI = MagicMock  # type: ignore[attr-defined]
    _mock_client.ClaudeAIConfig = MagicMock  # type: ignore[attr-defined]
    sys.modules["forge.vendor.agent_ai.client"] = _mock_client

    _mock_agent_ai = ModuleType("forge.vendor.agent_ai")
    _mock_agent_ai.AgentAI = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.AgentAIConfig = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.AgentResponse = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.ClaudeAI = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.ClaudeAIConfig = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.ClaudeResponse = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.Message = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.Metrics = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.TextContent = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.ThinkingContent = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.Tool = _ToolStub  # type: ignore[attr-defined]
    _mock_agent_ai.ToolResultContent = MagicMock  # type: ignore[attr-defined]
    _mock_agent_ai.ToolUseContent = MagicMock  # type: ignore[attr-defined]
    sys.modules["forge.vendor.agent_ai"] = _mock_agent_ai

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
