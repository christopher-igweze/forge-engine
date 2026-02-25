"""Prompt templates for Agent 12: Debt Tracker & Report Generator.

Generates the Production Readiness Report and tracks deferred items.
This is the viral acquisition hook — free scan produces this score.

Phase 1 stub — full implementation in Phase 3.
"""

SYSTEM_PROMPT = """\
You are a production readiness assessor generating a comprehensive report.

## Scoring Dimensions (weighted)
- Security (30%): Authentication, authorization, input validation, secrets management
- Error Handling (20%): Try/catch coverage, error boundaries, graceful degradation
- Test Coverage (15%): Existing tests, coverage gaps, critical path testing
- Architecture (15%): Module organization, separation of concerns, coupling
- Performance (10%): Query optimization, caching, pagination, lazy loading
- Documentation (10%): README, API docs, inline comments, type annotations

## Output
Produce a Production Readiness Report with:
- Overall score (0-100)
- Per-category scores with details
- Fixed issues summary
- Deferred issues with severity and reason
- Top 3-5 actionable recommendations
- Investor-friendly summary (2-3 sentences)

Respond with a JSON object matching the ProductionReadinessReport schema.
"""


def debt_tracker_task_prompt(
    *,
    all_findings_json: str,
    completed_fixes_json: str,
    deferred_items_json: str,
    validation_result_json: str = "",
) -> str:
    """Build the task prompt for the debt tracker."""
    return (
        f"## All Findings\n{all_findings_json}\n\n"
        f"## Completed Fixes\n{completed_fixes_json}\n\n"
        f"## Deferred Items\n{deferred_items_json}\n\n"
        f"## Validation Results\n{validation_result_json}\n\n"
        "Generate a Production Readiness Report with scores and recommendations."
    )
