"""Build prompt-injectable conventions context from extracted signals.

Zero LLM cost — pure string construction injected into existing prompts.
The conventions string appears alongside <project_context> in the system
prompt of discovery agents.

Usage:
    from forge.conventions.extractor import ConventionsExtractor
    from forge.conventions.formatter import build_conventions_context_string

    conventions = ConventionsExtractor(repo_path).extract()
    context_str = build_conventions_context_string(conventions)
"""

from __future__ import annotations

from forge.conventions.models import ProjectConventions


def build_conventions_context_string(conventions: ProjectConventions) -> str:
    """Build a prompt-injectable conventions section.

    Args:
        conventions: Auto-detected project conventions.

    Returns:
        A formatted XML-wrapped string ready for injection into LLM prompts.
        Returns empty string if no conventions were detected.
    """
    if conventions.is_empty:
        return ""

    parts = ["<project_conventions>"]
    parts.append("## Auto-Detected Project Conventions\n")
    parts.append(
        "These conventions were auto-detected from the project's own configuration "
        "files. Findings that conflict with these conventions are likely intentional "
        "choices, NOT issues to report.\n"
    )

    # ── Linting ──────────────────────────────────────────────────────
    lint = conventions.lint
    if lint.tool or lint.disabled_rules:
        parts.append("**Linting:**")
        if lint.tool:
            parts.append(f"- Linter: {lint.tool}")
        if lint.disabled_rules:
            rules_str = ", ".join(lint.disabled_rules[:20])
            parts.append(f"- Rules explicitly disabled: {rules_str}")
            parts.append(
                "  (These rules are intentionally turned off — do NOT flag violations)"
            )
        if lint.line_length:
            parts.append(f"- Line length limit: {lint.line_length}")
        if lint.target_version:
            parts.append(f"- Target version: {lint.target_version}")
        if lint.formatter:
            parts.append(f"- Formatter: {lint.formatter}")
        parts.append("")

    # ── Testing ──────────────────────────────────────────────────────
    test = conventions.test
    if test.framework:
        parts.append("**Testing:**")
        parts.append(f"- Framework: {test.framework}")
        if test.custom_markers:
            markers_str = ", ".join(test.custom_markers[:15])
            parts.append(f"- Custom markers: {markers_str}")
            parts.append(
                "  (Tests with these markers are categorized intentionally — "
                "do NOT flag as missing/skipped tests)"
            )
        if test.test_paths:
            parts.append(f"- Test paths: {', '.join(test.test_paths)}")
        if test.coverage_threshold is not None:
            parts.append(f"- Coverage threshold: {test.coverage_threshold}%")
        parts.append("")

    # ── TypeScript ───────────────────────────────────────────────────
    ts = conventions.typescript
    if ts.config_file:
        parts.append("**TypeScript:**")
        if ts.strict is not None:
            strict_label = "enabled" if ts.strict else "disabled"
            parts.append(f"- strict mode: {strict_label}")
            if not ts.strict:
                parts.append(
                    "  (Project does not enforce strict typing — do NOT flag "
                    "missing strict checks)"
                )
        if ts.no_implicit_any is not None:
            any_label = "enabled" if ts.no_implicit_any else "disabled"
            parts.append(f"- noImplicitAny: {any_label}")
            if not ts.no_implicit_any:
                parts.append(
                    "  (Project allows implicit `any` — do NOT flag `any` type usage)"
                )
        if ts.target:
            parts.append(f"- Target: {ts.target}")
        if ts.jsx:
            parts.append(f"- JSX: {ts.jsx}")
        parts.append("")

    # ── Detected patterns ────────────────────────────────────────────
    if conventions.detected_patterns:
        parts.append("**Detected Patterns:**")
        for pattern in conventions.detected_patterns[:10]:
            parts.append(f"- {pattern}")
        parts.append("")

    # ── Instruction ──────────────────────────────────────────────────
    parts.append(
        "DO NOT flag findings that conflict with these conventions unless they "
        "create a direct security vulnerability. These are intentional project "
        "choices documented in the project's own configuration files."
    )

    parts.append("</project_conventions>")

    return "\n".join(parts)
