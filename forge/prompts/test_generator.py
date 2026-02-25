"""Prompt templates for Agent 9: Test Generator.

Generates tests for each fix applied by the Coder agent.
Follows the codebase's existing test framework and patterns.

Phase 1 stub — full implementation in Phase 2.
"""

SYSTEM_PROMPT = """\
You are a senior QA engineer writing tests for code changes.

Given a code change and the original finding it addresses, write tests that:
1. Verify the fix actually addresses the finding
2. Prevent regression if the fix is reverted
3. Follow the codebase's existing test framework and patterns

Respond with a JSON object containing: test_files_created[], tests_written (int),
tests_passing (int), coverage_summary, summary.
"""


def test_generator_task_prompt(
    *,
    finding_json: str,
    code_change_json: str,
    existing_tests: str = "",
) -> str:
    """Build the task prompt for the test generator."""
    return (
        f"## Finding\n{finding_json}\n\n"
        f"## Code Change\n{code_change_json}\n\n"
        f"## Existing Tests\n{existing_tests}\n\n"
        "Write tests that verify this fix and prevent regression."
    )
