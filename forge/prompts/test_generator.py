"""Prompt templates for Agent 9: Test Generator.

Generates tests for each fix applied by the Coder agent.
Uses openrouter_direct (HTTP) — returns test code inline as JSON.
"""

SYSTEM_PROMPT = """\
You are a senior QA engineer writing regression tests for security and quality fixes.

Given a finding, the code change that addresses it, the actual diff, and project context, write tests that:
1. Verify the fix actually addresses the finding
2. Prevent regression if the fix is reverted
3. EXACTLY copy the existing test patterns, imports, and style from the project
4. Are self-contained and can run without external services (mock DB connections, HTTP calls, etc.)
5. Only import modules that exist in the project's dependencies

CRITICAL RULES:
- If an existing test example is provided, copy its import style, assertion library, and file structure EXACTLY
- Never invent imports — only use modules visible in the source code or project dependencies
- If the project uses pytest, write pytest tests. If it uses jest, write jest tests. Match the framework.
- Place test files in the same directory structure as existing tests
- Use relative imports that match the project layout

You MUST respond with a JSON object. Do not include markdown fences or explanations.
"""


def test_generator_task_prompt(
    *,
    finding_json: str,
    code_change_json: str,
    code_diff: str = "",
    source_context: dict[str, str] | None = None,
    existing_tests: str = "",
    framework_hint: str = "",
    project_hints: str = "",
    prior_test_failure: dict | None = None,
) -> str:
    """Build the task prompt for the test generator."""
    parts = []

    # Prior test failure feedback — Agent 9 must fix its own broken tests
    if prior_test_failure:
        parts.append(
            "## PREVIOUS TEST FAILURE — YOU MUST FIX THIS\n"
            "Your previous test generation produced broken tests. Here is what happened:\n\n"
            f"### Your Previous Test Code\n```\n{prior_test_failure.get('original_test_code', '')}\n```\n\n"
            f"### Error Output\n```\n{prior_test_failure.get('error_output', '')}\n```\n\n"
            f"Tests run: {prior_test_failure.get('tests_run', 0)}, "
            f"Tests passed: {prior_test_failure.get('tests_passed', 0)}\n\n"
            "INSTRUCTIONS:\n"
            "- Analyze the error output to understand WHY your tests failed\n"
            "- Fix ALL issues: syntax errors, import errors, wrong assertions, wrong function signatures\n"
            "- Make sure your tests actually match the code diff below\n"
            "- Do NOT repeat the same mistakes\n"
        )

    # Framework hint at the top so Agent 9 knows immediately
    if framework_hint:
        parts.append(f"## Test Framework\nThis project uses **{framework_hint}**. Write tests using this framework ONLY.\n")

    parts.append(f"## Finding\n{finding_json}")
    parts.append(f"\n## Code Change\n{code_change_json}")

    if code_diff:
        # Truncate very long diffs to stay within context
        diff_text = code_diff[:8000]
        if len(code_diff) > 8000:
            diff_text += "\n... (diff truncated)"
        parts.append(f"\n## Actual Diff\n```diff\n{diff_text}\n```")

    # Source code of modified files — so Agent 9 sees real imports and APIs
    if source_context:
        parts.append("\n## Source Code of Modified Files")
        for path, content in source_context.items():
            parts.append(f"\n### {path}\n```\n{content}\n```")

    # Existing test example — Agent 9 should copy this pattern exactly
    if existing_tests and existing_tests != "(Agent will discover existing tests via Glob/Read)":
        parts.append(
            "\n## Existing Test Example (COPY THIS PATTERN)\n"
            "Use the same imports, assertion style, and file structure as this existing test:\n"
            f"```\n{existing_tests}\n```"
        )

    # Project configuration hints
    if project_hints:
        parts.append(f"\n## Project Configuration\n{project_hints}")

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
