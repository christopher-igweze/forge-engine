"""Shared helper functions for FORGE report generation.

Small utilities used across report modules: score coloring, labeling, and
HTML escaping.
"""

from __future__ import annotations


def _score_color(score: int) -> str:
    """Get a CSS color for a readiness score."""
    if score >= 80:
        return "#22c55e"  # green
    if score >= 60:
        return "#eab308"  # yellow
    if score >= 40:
        return "#f97316"  # orange
    return "#ef4444"  # red


def _score_label(score: int) -> str:
    """Get a human-readable label for a readiness score."""
    if score >= 80:
        return "Production Ready"
    if score >= 60:
        return "Needs Improvement"
    if score >= 40:
        return "Significant Issues"
    return "Not Production Ready"


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
