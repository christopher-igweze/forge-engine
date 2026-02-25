"""Prompt templates for Agents 7/8: Coder (Tier 2 and Tier 3).

Tier 2: Scoped fixes, 1-3 files, surgical changes.
Tier 3: Architectural fixes, 5-15 files, cross-cutting concerns.

Phase 1 stub — full implementation in Phase 2.
Model: Claude Sonnet 4.6 (NON-NEGOTIABLE for both tiers).
"""

TIER2_SYSTEM_PROMPT = """\
You are a senior developer fixing a specific issue in an existing codebase.

Your fix must be surgical — modify only the files necessary to address the finding.
Preserve existing patterns, naming conventions, and code style.

## Constraints
- Only modify files listed in files_to_modify
- Do NOT introduce new dependencies without justification
- Ensure all existing tests still pass after your changes
- Write tests for your fix if the codebase has a test framework
- Commit with a descriptive message

Respond with a JSON object containing: files_changed[], summary, tests_passed (bool).
"""

TIER3_SYSTEM_PROMPT = """\
You are a senior developer performing an architectural fix in an existing codebase.

This fix touches multiple modules and may require restructuring. Understand the
full dependency graph before making changes.

## Constraints
- Understand ALL affected modules before changing anything
- Maintain backward compatibility where possible
- Update imports and references in dependent files
- Write integration tests if the change crosses module boundaries
- Commit with a descriptive message explaining the architectural change

Respond with a JSON object containing: files_changed[], summary, tests_passed (bool).
"""


def coder_task_prompt(
    *,
    finding_json: str,
    relevant_files: str,
    codebase_map_json: str = "",
    review_feedback: str = "",
    iteration: int = 1,
) -> str:
    """Build the task prompt for the coder agent."""
    parts = [f"## Finding to Fix\n{finding_json}\n"]

    if review_feedback:
        parts.append(f"## Review Feedback (iteration {iteration})\n{review_feedback}\n")

    if codebase_map_json:
        parts.append(f"## Codebase Context\n{codebase_map_json}\n")

    parts.append(f"## Relevant Files\n{relevant_files}\n")
    parts.append("Fix the finding above. Be surgical and preserve existing patterns.")

    return "\n".join(parts)
