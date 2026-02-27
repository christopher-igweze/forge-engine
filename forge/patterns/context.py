"""Build LLM prompt context from vulnerability patterns.

Generates structured text that gets injected into security auditor and
swarm worker prompts so the LLM knows what specific patterns to check.
"""

from __future__ import annotations

from forge.patterns.loader import PatternLibrary


def build_pattern_context_for_prompt(
    library: PatternLibrary,
    category: str = "security",
    tech_hints: list[str] | None = None,
) -> str:
    """Build a prompt section describing known vulnerability patterns.

    Args:
        library: Loaded pattern library.
        category: Filter patterns to this category (empty string = all).
        tech_hints: Detected technologies (e.g. ["supabase", "react"]).
    """
    patterns = library.by_category(category) if category else library.all()
    if not patterns:
        return ""

    tech_hints = [t.lower() for t in (tech_hints or [])]

    sections: list[str] = [
        "## Known Vulnerability Patterns to Check\n",
        "The following vulnerability patterns are common in vibe-coded "
        "applications. Check each one against this codebase.\n",
    ]

    for pattern in patterns:
        sections.append(f"### {pattern.id}: {pattern.name}")
        sections.append(f"**Severity:** {pattern.severity_default}")
        if pattern.cwe_ids:
            sections.append(f"**CWEs:** {', '.join(pattern.cwe_ids)}")

        g = pattern.llm_guidance
        if g.reasoning_prompt:
            sections.append(f"\n{g.reasoning_prompt}")

        if g.key_questions:
            sections.append("\n**Key Questions:**")
            for q in g.key_questions:
                sections.append(f"- {q}")

        # Technology-specific hints for detected stack
        matched_tech = False
        for tech in tech_hints:
            variant = g.technology_variants.get(tech, "")
            if variant:
                sections.append(f"\n**{tech.title()} specific:** {variant}")
                matched_tech = True

        # Fallback to generic hint if no tech matched
        if not matched_tech:
            generic = g.technology_variants.get("generic", "")
            if generic:
                sections.append(f"\n**Check:** {generic}")

        sections.append("")  # blank line separator

    sections.append(
        'When a finding matches one of these patterns, include '
        '"pattern_id" and "pattern_slug" fields in the finding JSON.'
    )

    return "\n".join(sections)


def extract_tech_hints_from_codebase_map(codebase_map_dict: dict) -> list[str]:
    """Extract technology hints from a CodebaseMap dict.

    Scans tech_stack, dependencies, and modules for known BaaS/framework
    identifiers to determine which technology-specific guidance to include.
    """
    hints: set[str] = set()

    # Check tech_stack.packages
    tech_stack = codebase_map_dict.get("tech_stack", {})
    for pkg in tech_stack.get("packages", []):
        _check_tech(pkg, hints)

    # Check dependencies list
    for dep in codebase_map_dict.get("dependencies", []):
        name = dep.get("name", "") if isinstance(dep, dict) else str(dep)
        _check_tech(name, hints)

    # Check modules for framework hints
    for module in codebase_map_dict.get("modules", []):
        name = module.get("name", "") if isinstance(module, dict) else str(module)
        _check_tech(name, hints)

    return sorted(hints)


_TECH_KEYWORDS: dict[str, list[str]] = {
    "supabase": ["supabase"],
    "firebase": ["firebase"],
    "pocketbase": ["pocketbase"],
    "appwrite": ["appwrite"],
}


def _check_tech(value: str, hints: set[str]) -> None:
    """Check a string for known technology keywords."""
    lower = value.lower()
    for tech, keywords in _TECH_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            hints.add(tech)
