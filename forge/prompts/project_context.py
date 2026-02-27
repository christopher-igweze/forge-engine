"""Build prompt-injectable project context from user-provided metadata.

Zero LLM cost — this is pure string construction injected into existing
prompts. The project context string appears in the system prompt between
the base instructions and pattern library context.

Usage:
    ctx = {"project_stage": "mvp", "team_size": 1, "vision_summary": "..."}
    context_str = build_project_context_string(ctx)
    # Inject into prompt: system_prompt += context_str
"""

from __future__ import annotations

_STAGE_LABELS = {
    "mvp": "MVP / Prototype (early stage, solo dev likely, speed over perfection)",
    "early_product": "Early Product (small team, some users, hardening in progress)",
    "growth": "Growth Stage (active users, scaling concerns, needs production rigor)",
    "enterprise": "Enterprise (large team, compliance requirements, full production standards)",
}


def build_project_context_string(ctx: dict) -> str:
    """Build a prompt-injectable project context section.

    Args:
        ctx: User-provided project context dict. All fields are optional.
            Supported keys: project_stage, team_size, vision_summary,
            target_launch, known_compromises, beloved_features,
            original_prompt, sensitive_data_types.

    Returns:
        A formatted string ready for injection into LLM prompts.
        Returns empty string if ctx is empty or None.
    """
    if not ctx:
        return ""

    parts = ["<project_context>"]
    parts.append("## Project Context\n")
    parts.append("The developer provided the following context about this project.")
    parts.append("Use this to calibrate your findings — what matters depends on")
    parts.append("the project's stage, scale, and goals.\n")

    stage = ctx.get("project_stage", "")
    if stage:
        parts.append(f"**Project Stage:** {_STAGE_LABELS.get(stage, stage)}")

    team_size = ctx.get("team_size", 0)
    if team_size:
        label = "developer" if team_size == 1 else "developers"
        parts.append(f"**Team Size:** {team_size} {label}")

    vision = ctx.get("vision_summary", "")
    if vision:
        parts.append(f"**Vision:** {vision}")

    target = ctx.get("target_launch", "")
    if target:
        parts.append(f"**Target Launch:** {target}")

    compromises = ctx.get("known_compromises", [])
    if compromises:
        parts.append("\n**Known Compromises (developer is aware of these):**")
        for c in compromises:
            parts.append(f"- {c}")
        parts.append("\nIf a finding overlaps with a known compromise, note it but")
        parts.append('classify actionability as "informational" — the developer already knows.')

    beloved = ctx.get("beloved_features", [])
    if beloved:
        parts.append(f"\n**Beloved Features:** {', '.join(beloved)}")
        parts.append("The developer values these. Suggest minimal-impact fixes that")
        parts.append("preserve their design intent rather than architectural rewrites.")

    original_prompt = ctx.get("original_prompt", "")
    if original_prompt:
        parts.append("\n**Original Prompt (used to generate this codebase):**")
        parts.append(f"> {original_prompt[:500]}")

    sensitive = ctx.get("sensitive_data_types", [])
    if sensitive:
        parts.append(f"\n**Sensitive Data:** {', '.join(sensitive)}")
        parts.append("Escalate severity for any finding that could expose this data.")

    # Scale-awareness heuristic
    if stage in ("mvp", "early_product") or (team_size and team_size <= 2):
        parts.append("\n**Scale Guidance:** This is a small/early-stage project.")
        parts.append("Do NOT recommend architectural patterns (repository layers,")
        parts.append("service abstractions, etc.) unless they directly fix a")
        parts.append("security vulnerability. Focus on exploitable bugs, not structure.")

    parts.append("</project_context>")

    return "\n".join(parts)
