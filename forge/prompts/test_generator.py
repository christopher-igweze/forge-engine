"""Prompt templates for Agent 9: Test Generator.

Generates tests for each fix applied by the Coder agent.
Uses openrouter_direct (HTTP) — returns test code inline as JSON.
"""

SYSTEM_PROMPT = """\
You are a senior QA engineer writing regression tests for security and quality fixes.

Given a finding, the code change that addresses it, and the actual diff, write tests that:
1. Verify the fix actually addresses the finding
2. Prevent regression if the fix is reverted
3. Follow the codebase's existing test framework and patterns
4. Are self-contained and can run without external services (mock DB connections, HTTP calls, etc.)

You MUST respond with a JSON object. Do not include markdown fences or explanations.
"""


def test_generator_task_prompt(
    *,
    finding_json: str,
    code_change_json: str,
    code_diff: str = "",
    existing_tests: str = "",
) -> str:
    """Build the task prompt for the test generator."""
    parts = [
        f"## Finding\n{finding_json}",
        f"\n## Code Change\n{code_change_json}",
    ]
    if code_diff:
        # Truncate very long diffs to stay within context
        diff_text = code_diff[:8000]
        if len(code_diff) > 8000:
            diff_text += "\n... (diff truncated)"
        parts.append(f"\n## Actual Diff\n```diff\n{diff_text}\n```")
    if existing_tests and existing_tests != "(Agent will discover existing tests via Glob/Read)":
        parts.append(f"\n## Existing Tests\n{existing_tests}")
    parts.append(
        "\n## Instructions\n"
        "Write regression tests for this fix. Return your response as a JSON object with these fields:\n"
        "- finding_id: the finding ID from above\n"
        "- test_file_contents: array of {path, content} objects — each is a test file to create\n"
        "- test_files_created: array of file paths (same paths as test_file_contents)\n"
        "- tests_written: number of test cases written\n"
        "- tests_passing: expected number of passing tests (estimate)\n"
        "- coverage_summary: brief description of what the tests cover\n"
        "- summary: one-sentence summary of the test approach\n"
    )
    return "\n".join(parts)
