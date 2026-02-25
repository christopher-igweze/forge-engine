"""Prompt templates for Agent 11: Integration Validator.

Validates the merged codebase after all fixes are applied.
Runs integration checks and compares against original findings.

Phase 1 stub — full implementation in Phase 3.
"""

SYSTEM_PROMPT = """\
You are a senior QA engineer performing integration validation.

After all fixes have been merged, validate:
1. The codebase still builds and runs
2. All existing tests pass
3. No regressions were introduced
4. The original findings are actually resolved

Respond with a JSON object containing: passed (bool), tests_run, tests_passed,
tests_failed, regressions_detected[], new_issues_introduced[], summary.
"""


def integration_validator_task_prompt(
    *,
    all_findings_json: str,
    all_fixes_json: str,
    test_results: str = "",
) -> str:
    """Build the task prompt for the integration validator."""
    return (
        f"## Original Findings\n{all_findings_json}\n\n"
        f"## Applied Fixes\n{all_fixes_json}\n\n"
        f"## Test Results\n{test_results}\n\n"
        "Validate that fixes resolved the findings without regressions."
    )
