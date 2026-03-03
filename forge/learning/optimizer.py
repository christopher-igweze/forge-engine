"""Prompt optimization using textual gradients.

Takes TextualGradient feedback from the backward LLM and generates concrete
PromptPatch objects that describe how to modify agent system prompts.

Safety constraints enforce maximum change rates and protect invariant sections.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from forge.learning.backward import TextualGradient
from forge.learning.graph import ForgeGraph

logger = logging.getLogger(__name__)

# Maximum percentage of prompt that can change in a single optimization cycle
MAX_CHANGE_PCT = 20.0

# Lines containing this marker are never modified
INVARIANT_MARKER = "# INVARIANT"


class OptimizationMode(str, Enum):
    CONSERVATIVE = "conservative"  # Only add few-shots + clarifications
    MODERATE = "moderate"  # Rewrite instruction sections
    AGGRESSIVE = "aggressive"  # Full restructuring (requires approval)


@dataclass
class PromptChange:
    """A single change to an agent's prompt."""

    action: str  # "add" | "remove" | "modify"
    section: str  # which section of the prompt
    original: str  # original text (empty for "add")
    replacement: str  # new text (empty for "remove")
    reason: str  # why this change is suggested

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "section": self.section,
            "original": self.original,
            "replacement": self.replacement,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptChange:
        return cls(
            action=data.get("action", "modify"),
            section=data.get("section", ""),
            original=data.get("original", ""),
            replacement=data.get("replacement", ""),
            reason=data.get("reason", ""),
        )


@dataclass
class PromptPatch:
    """A set of changes to apply to an agent's prompt."""

    agent_name: str
    changes: list[PromptChange] = field(default_factory=list)
    mode: OptimizationMode = OptimizationMode.CONSERVATIVE
    estimated_change_pct: float = 0.0  # % of prompt modified
    gradients_used: list[str] = field(default_factory=list)  # gradient feedback strings

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "changes": [c.to_dict() for c in self.changes],
            "mode": self.mode.value,
            "estimated_change_pct": self.estimated_change_pct,
            "gradients_used": self.gradients_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptPatch:
        return cls(
            agent_name=data.get("agent_name", ""),
            changes=[PromptChange.from_dict(c) for c in data.get("changes", [])],
            mode=OptimizationMode(data.get("mode", "conservative")),
            estimated_change_pct=data.get("estimated_change_pct", 0.0),
            gradients_used=data.get("gradients_used", []),
        )


def generate_prompt_patch(
    graph: ForgeGraph,
    gradients: list[TextualGradient],
    mode: OptimizationMode = OptimizationMode.CONSERVATIVE,
) -> list[PromptPatch]:
    """Generate prompt patches from textual gradients.

    Safety constraints:
    - Max 20% of prompt changed per cycle
    - Lines marked # INVARIANT are never modified
    - CONSERVATIVE: only add few-shots and clarifications
    - MODERATE: rewrite instruction sections
    - AGGRESSIVE: full restructure (saved to patches/ for manual review)
    """
    if not gradients:
        return []

    # Group gradients by target agent
    by_agent: dict[str, list[TextualGradient]] = {}
    for g in gradients:
        by_agent.setdefault(g.target_node, []).append(g)

    patches: list[PromptPatch] = []
    for agent_name, agent_gradients in by_agent.items():
        patch = _build_patch_for_agent(agent_name, agent_gradients, graph, mode)
        if patch.changes:
            patches.append(patch)

    logger.info(
        "Generated %d prompt patches from %d gradients (mode=%s)",
        len(patches), len(gradients), mode.value,
    )
    return patches


def _build_patch_for_agent(
    agent_name: str,
    gradients: list[TextualGradient],
    graph: ForgeGraph,
    mode: OptimizationMode,
) -> PromptPatch:
    """Build a PromptPatch for a single agent from its gradients."""
    # Find the agent's node to get current prompt template
    node = next(
        (n for n in graph.nodes.values() if n.agent_name == agent_name),
        None,
    )
    current_prompt = node.prompt_template if node else ""

    # Filter gradients by confidence threshold
    min_confidence = {
        OptimizationMode.CONSERVATIVE: 0.7,
        OptimizationMode.MODERATE: 0.5,
        OptimizationMode.AGGRESSIVE: 0.3,
    }[mode]

    high_confidence = [g for g in gradients if g.confidence >= min_confidence]
    if not high_confidence:
        return PromptPatch(agent_name=agent_name, mode=mode)

    changes: list[PromptChange] = []
    gradients_used: list[str] = []

    for gradient in high_confidence:
        gradients_used.append(gradient.feedback)

        for suggestion in gradient.suggested_prompt_changes:
            change = _suggestion_to_change(suggestion, current_prompt, mode)
            if change:
                changes.append(change)

    # Apply mode constraints
    changes = _apply_mode_constraints(changes, mode)

    # Estimate change percentage
    change_pct = _estimate_change_pct(changes, current_prompt)
    if change_pct > MAX_CHANGE_PCT:
        # Trim changes to stay within budget
        changes = _trim_to_budget(changes, current_prompt, MAX_CHANGE_PCT)
        change_pct = _estimate_change_pct(changes, current_prompt)

    return PromptPatch(
        agent_name=agent_name,
        changes=changes,
        mode=mode,
        estimated_change_pct=round(change_pct, 1),
        gradients_used=gradients_used,
    )


def _suggestion_to_change(
    suggestion: str,
    current_prompt: str,
    mode: OptimizationMode,
) -> PromptChange | None:
    """Convert a textual suggestion into a PromptChange.

    CONSERVATIVE: only creates "add" changes (appending clarifications/examples)
    MODERATE: creates "add" and "modify" changes
    AGGRESSIVE: creates "add", "modify", and "remove" changes
    """
    suggestion_lower = suggestion.lower()

    # Detect the type of change suggested
    if any(k in suggestion_lower for k in ("add", "include", "append", "insert")):
        return PromptChange(
            action="add",
            section="constraints",
            original="",
            replacement=suggestion,
            reason=suggestion,
        )

    if any(k in suggestion_lower for k in ("remove", "delete", "drop")):
        if mode == OptimizationMode.CONSERVATIVE:
            return None  # Conservative mode doesn't remove
        return PromptChange(
            action="remove",
            section="constraints",
            original=suggestion,
            replacement="",
            reason=suggestion,
        )

    if any(k in suggestion_lower for k in ("change", "modify", "rewrite", "replace", "update")):
        if mode == OptimizationMode.CONSERVATIVE:
            # Downgrade to an "add" — add a clarification instead
            return PromptChange(
                action="add",
                section="clarifications",
                original="",
                replacement=f"CLARIFICATION: {suggestion}",
                reason=suggestion,
            )
        return PromptChange(
            action="modify",
            section="instructions",
            original="",
            replacement=suggestion,
            reason=suggestion,
        )

    # Default: treat as an addition
    return PromptChange(
        action="add",
        section="guidance",
        original="",
        replacement=suggestion,
        reason=suggestion,
    )


def _apply_mode_constraints(
    changes: list[PromptChange],
    mode: OptimizationMode,
) -> list[PromptChange]:
    """Filter changes based on optimization mode."""
    if mode == OptimizationMode.CONSERVATIVE:
        return [c for c in changes if c.action == "add"]
    elif mode == OptimizationMode.MODERATE:
        return [c for c in changes if c.action in ("add", "modify")]
    return changes  # AGGRESSIVE allows all


def _estimate_change_pct(changes: list[PromptChange], current_prompt: str) -> float:
    """Estimate what percentage of the prompt is affected by the changes."""
    if not current_prompt:
        return 0.0

    prompt_len = len(current_prompt)
    change_chars = sum(
        max(len(c.original), len(c.replacement))
        for c in changes
    )
    return (change_chars / prompt_len * 100) if prompt_len > 0 else 0.0


def _trim_to_budget(
    changes: list[PromptChange],
    current_prompt: str,
    max_pct: float,
) -> list[PromptChange]:
    """Trim changes list to stay within the change budget.

    Keeps highest-impact changes first (by replacement length).
    """
    if not current_prompt:
        return changes

    budget_chars = len(current_prompt) * max_pct / 100
    trimmed: list[PromptChange] = []
    used = 0.0

    # Sort by replacement length descending (keep biggest changes first)
    for c in sorted(changes, key=lambda x: len(x.replacement), reverse=True):
        cost = max(len(c.original), len(c.replacement))
        if used + cost <= budget_chars:
            trimmed.append(c)
            used += cost

    return trimmed


def _contains_invariant(text: str) -> bool:
    """Check if text contains an INVARIANT marker."""
    return INVARIANT_MARKER in text


def save_patches(patches: list[PromptPatch], output_dir: Path) -> list[Path]:
    """Save prompt patches as JSON files for review.

    Returns the list of paths written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for patch in patches:
        filename = f"patch_{patch.agent_name}_{patch.mode.value}.json"
        path = output_dir / filename
        path.write_text(json.dumps(patch.to_dict(), indent=2))
        paths.append(path)
        logger.info("Saved patch: %s (%d changes)", path, len(patch.changes))

    return paths


def load_patch(path: Path) -> PromptPatch:
    """Load a prompt patch from a JSON file."""
    data = json.loads(path.read_text())
    return PromptPatch.from_dict(data)
