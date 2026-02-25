"""Prompt templates for the LLM escalation agent (middle loop).

This agent decides what to do when the inner loop is exhausted:
RECLASSIFY, SPLIT, DEFER, or ESCALATE.
"""

ESCALATION_SYSTEM_PROMPT = """\
You are an expert software engineering escalation agent. Your job is to decide
what to do when a code fix has failed after multiple retry attempts.

You will be given:
1. The original finding (security/quality/architecture issue)
2. The fix attempts that were made (coder output + reviewer feedback)
3. The current remediation tier (2 = scoped fix, 3 = architectural fix)

You MUST choose exactly ONE action:

- RECLASSIFY: The fix failed because it needs broader context. Promote from
  Tier 2 → Tier 3 so the coder gets more files and higher turn budget.
  Only valid if current tier is 2.

- SPLIT: The finding is actually multiple issues bundled together. Decompose
  it into 2-3 smaller, independent sub-findings that can each be fixed
  separately. Provide the split items.

- DEFER: The fix is too risky or complex for automated remediation. Mark it
  as documented technical debt with a clear explanation of what a human
  developer needs to do.

- ESCALATE: Multiple fixes in related areas are failing — the remediation
  plan itself needs restructuring. This triggers the outer loop to re-run
  the Fix Strategist.

Decision guidelines:
- If Tier 2 and the reviewer says "needs broader context" → RECLASSIFY
- If the finding mentions multiple distinct issues → SPLIT
- If Tier 3 already failed → DEFER (don't retry at same tier)
- If the reviewer says "fundamental architecture issue" → ESCALATE
- When in doubt → DEFER (first, do no harm)

Respond with a JSON object:
{
  "action": "RECLASSIFY" | "SPLIT" | "DEFER" | "ESCALATE",
  "rationale": "Brief explanation of why this action was chosen",
  "new_tier": 3,  // Only for RECLASSIFY
  "split_items": [  // Only for SPLIT
    {"title": "...", "description": "...", "estimated_files": 1}
  ]
}
"""


def build_escalation_task(
    finding: dict,
    coder_result: dict | None,
    review_result: dict | None,
    current_tier: int,
    iteration_count: int,
) -> str:
    """Build the task prompt for the escalation agent."""
    parts = [
        "## Finding That Failed to Fix",
        f"**ID**: {finding.get('id', 'unknown')}",
        f"**Title**: {finding.get('title', 'unknown')}",
        f"**Category**: {finding.get('category', 'unknown')}",
        f"**Severity**: {finding.get('severity', 'unknown')}",
        f"**Description**: {finding.get('description', '')}",
        f"**Suggested Fix**: {finding.get('suggested_fix', '')}",
        "",
        f"## Current Tier: {current_tier}",
        f"## Retry Attempts: {iteration_count}",
        "",
    ]

    if coder_result:
        parts.extend([
            "## Last Coder Attempt",
            f"**Outcome**: {coder_result.get('outcome', 'unknown')}",
            f"**Summary**: {coder_result.get('summary', '')}",
            f"**Error**: {coder_result.get('error_message', '')}",
            f"**Files Changed**: {', '.join(coder_result.get('files_changed', []))}",
            "",
        ])

    if review_result:
        parts.extend([
            "## Reviewer Feedback",
            f"**Decision**: {review_result.get('decision', 'unknown')}",
            f"**Summary**: {review_result.get('summary', '')}",
            f"**Issues**: {'; '.join(review_result.get('issues', []))}",
            f"**Regression Risk**: {review_result.get('regression_risk', 'unknown')}",
            "",
        ])

    parts.append("## Your Decision")
    parts.append("Respond with the JSON object described in your system prompt.")

    return "\n".join(parts)
