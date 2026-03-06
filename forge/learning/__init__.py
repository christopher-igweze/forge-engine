"""FORGE Learning Loop — feedback from scan data back to agent improvement.

Modules:
    feedback   — fix outcome aggregation + few-shot examples + agent guidance
    report     — learning loop status report
    graph      — computation graph representation of pipeline runs
    backward   — backward LLM for textual gradient generation
    optimizer  — prompt optimization using textual gradients
    validation — A/B validation framework for prompt patches
    cli        — CLI entry point for optimization commands
"""

from forge.learning.backward import TextualGradient, generate_textual_gradient
from forge.learning.graph import ForgeGraph, GraphEdge, GraphNode
from forge.learning.optimizer import OptimizationMode, PromptChange, PromptPatch
from forge.learning.validation import ABResult, GoldenTest, MetricComparison

__all__ = [
    "ABResult",
    "ForgeGraph",
    "GoldenTest",
    "GraphEdge",
    "GraphNode",
    "MetricComparison",
    "OptimizationMode",
    "PromptChange",
    "PromptPatch",
    "TextualGradient",
    "generate_textual_gradient",
]
