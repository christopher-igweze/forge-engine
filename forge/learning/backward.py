"""Backward LLM for generating textual gradients on FORGE agent failures.

Implements the "textual gradient" concept from AdalFlow/LLM-AutoDiff:
a critic LLM analyzes what went wrong in a failed agent invocation and
generates structured, actionable feedback for prompt optimization.

Only called on failed/suboptimal nodes — selective computation keeps costs low.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from forge.learning.graph import GraphNode

logger = logging.getLogger(__name__)

BACKWARD_SYSTEM_PROMPT = """\
You are a prompt engineering critic. You analyze AI agent failures and generate
specific, actionable feedback for improving agent system prompts.

Your output MUST be a single JSON object with these fields:
- "target_node": string — the agent name that needs improvement
- "feedback": string — what went wrong and why (2-3 sentences)
- "suggested_prompt_changes": array of strings — concrete, specific changes
- "confidence": number 0-1 — how confident you are in the diagnosis

Focus on ROOT CAUSES, not symptoms. For example:
- BAD: "The agent failed to produce valid JSON"
- GOOD: "The agent was not given explicit output format constraints, so it
  returned free-form text instead of the required JSON structure"

Only suggest changes that would prevent the specific failure observed.
Do NOT suggest generic improvements or changes unrelated to the failure.
"""

BACKWARD_TASK_TEMPLATE = """\
## Agent Failure Analysis

**Agent:** {agent_name}
**Phase:** {phase}
**Node ID:** {node_id}

### Current Prompt Template
{prompt_template}

### Expected Output
{expected}

### Actual Output / Error
{actual}

### Agent Metrics
{metrics}

---

Analyze this failure. What specific change to the agent's system prompt would
have prevented it? Respond with the JSON object described in your instructions.
"""


@dataclass
class TextualGradient:
    """Feedback from backward LLM about what went wrong."""

    target_node: str  # agent that needs improvement
    feedback: str  # what went wrong and why
    suggested_prompt_changes: list[str]  # concrete suggestions
    confidence: float  # 0-1 how confident the backward LLM is

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_node": self.target_node,
            "feedback": self.feedback,
            "suggested_prompt_changes": self.suggested_prompt_changes,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TextualGradient:
        return cls(
            target_node=data.get("target_node", ""),
            feedback=data.get("feedback", ""),
            suggested_prompt_changes=data.get("suggested_prompt_changes", []),
            confidence=float(data.get("confidence", 0.0)),
        )


async def generate_textual_gradient(
    node: GraphNode,
    expected: Any,
    actual: Any,
    model: str = "anthropic/claude-haiku-4.5",
) -> TextualGradient:
    """Use a critic LLM to analyze what went wrong and suggest prompt improvements.

    Selective computation — only call this on failed/suboptimal nodes.
    Uses the existing AgentAI client for the LLM call.
    """
    from forge.vendor.agent_ai import AgentAI, AgentAIConfig

    # Format inputs for the backward LLM
    expected_str = json.dumps(expected, indent=2, default=str) if not isinstance(expected, str) else expected
    actual_str = json.dumps(actual, indent=2, default=str) if not isinstance(actual, str) else actual
    metrics_str = json.dumps(node.metrics, indent=2) if node.metrics else "No metrics available"

    task = BACKWARD_TASK_TEMPLATE.format(
        agent_name=node.agent_name,
        phase=node.phase,
        node_id=node.node_id,
        prompt_template=node.prompt_template or "(prompt template not captured)",
        expected=expected_str,
        actual=actual_str,
        metrics=metrics_str,
    )

    ai = AgentAI(AgentAIConfig(
        provider="openrouter_direct",
        model=model,
        cwd=".",
        max_turns=1,
        allowed_tools=[],
        agent_name="backward_llm",
    ))

    logger.info(
        "Generating textual gradient for %s (%s)",
        node.node_id, node.agent_name,
    )

    response = await ai.run(task, system_prompt=BACKWARD_SYSTEM_PROMPT)

    # Parse the response
    gradient = _parse_gradient_response(response.text, node)

    logger.info(
        "Textual gradient for %s: confidence=%.2f, %d suggestions",
        node.node_id, gradient.confidence, len(gradient.suggested_prompt_changes),
    )
    return gradient


def _parse_gradient_response(text: str, node: GraphNode) -> TextualGradient:
    """Parse backward LLM response into a TextualGradient.

    Falls back to a low-confidence gradient if parsing fails.
    """
    if not text:
        return _fallback_gradient(node, "Backward LLM returned empty response")

    cleaned = text.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end])

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse backward LLM JSON response for %s", node.node_id)
        return _fallback_gradient(node, f"Parse failure. Raw response: {text[:200]}")

    return TextualGradient(
        target_node=data.get("target_node", node.agent_name),
        feedback=data.get("feedback", ""),
        suggested_prompt_changes=data.get("suggested_prompt_changes", []),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
    )


def _fallback_gradient(node: GraphNode, reason: str) -> TextualGradient:
    """Create a low-confidence fallback gradient when parsing fails."""
    return TextualGradient(
        target_node=node.agent_name,
        feedback=f"Analysis failed: {reason}",
        suggested_prompt_changes=[],
        confidence=0.0,
    )


async def generate_gradients_for_failures(
    nodes: list[GraphNode],
    expected_outputs: dict[str, Any] | None = None,
    actual_outputs: dict[str, Any] | None = None,
    model: str = "anthropic/claude-haiku-4.5",
) -> list[TextualGradient]:
    """Generate textual gradients for a batch of failed nodes.

    Processes failed nodes sequentially to manage API costs.
    """
    expected_outputs = expected_outputs or {}
    actual_outputs = actual_outputs or {}

    gradients: list[TextualGradient] = []
    for node in nodes:
        if node.success:
            continue

        expected = expected_outputs.get(node.node_id, "Successful completion with valid output")
        actual = actual_outputs.get(node.node_id, node.error or "Unknown failure")

        gradient = await generate_textual_gradient(
            node=node,
            expected=expected,
            actual=actual,
            model=model,
        )
        gradients.append(gradient)

    logger.info("Generated %d textual gradients from %d nodes", len(gradients), len(nodes))
    return gradients
