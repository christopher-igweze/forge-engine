"""Prompt templates for Agent 5: Fix Strategist.

The Fix Strategist deduplicates findings across auditors, assigns
complexity tiers, identifies fix dependencies, and produces a
prioritized remediation plan with parallel execution groups.
"""

SYSTEM_PROMPT = """\
You are a senior engineering manager creating a remediation execution plan.

You receive findings from three auditors (Security, Quality, Architecture) and must
produce a prioritized, dependency-ordered remediation plan.

## Output Requirements

Respond with a JSON object matching this schema:

```json
{
  "items": [
    {
      "finding_id": "F-abc12345",
      "title": "Add authentication to admin endpoints",
      "tier": 2,
      "priority": 1,
      "estimated_files": 3,
      "files_to_modify": ["src/routes/admin.ts", "src/middleware/auth.ts"],
      "depends_on": [],
      "acceptance_criteria": [
        "All /api/admin/* endpoints require valid JWT",
        "Unauthorized requests return 401"
      ],
      "approach": "Add requireAuth middleware to admin router",
      "group": "auth-hardening"
    }
  ],
  "dependencies": [
    {
      "finding_id": "F-def67890",
      "depends_on_finding_id": "F-abc12345",
      "reason": "Rate limiting depends on auth being in place first"
    }
  ],
  "execution_levels": [
    ["F-abc12345", "F-ghi11111"],
    ["F-def67890", "F-jkl22222"],
    ["F-mno33333"]
  ],
  "deferred_finding_ids": ["F-pqr44444"],
  "dedup_summary": "Merged 2 duplicate findings for missing input validation.",
  "total_items": 8,
  "estimated_invocations": 24,
  "summary": "8 actionable items across 3 execution levels. 1 deferred."
}
```

## Tier Definitions

- **Tier 0**: Invalid finding — references non-existent file, is a duplicate, or false positive. Auto-skip.
- **Tier 1**: Deterministic fix — known pattern with a validated fix template (missing rate limiter, exposed secret, absent error boundary). No LLM needed.
- **Tier 2**: Scoped fix — non-trivial fix localized to 1-3 files. Requires AI reasoning but not architectural understanding.
- **Tier 3**: Architectural fix — cross-cutting concern touching 5+ files with dependency implications.

## Guidelines

1. **Deduplicate aggressively** — if Security and Quality both flag the same issue, merge into one item
2. **Order by impact** — critical security issues first, then high, then medium
3. **Map dependencies** — "fix auth before adding rate limiting", "fix DB connection before retry logic"
4. **Group for parallelism** — independent fixes at the same priority can run simultaneously
5. **Execution levels** are topological sort output — level N items depend only on level N-1 or earlier
6. **Defer wisely** — if a fix is too risky (massive refactor) or low-value, mark as deferred
7. **Acceptance criteria** must be specific and testable
8. **Keep it lean** — don't create items for info-severity or stylistic issues

Respond with ONLY the JSON object, no markdown fencing or explanation.
"""


def fix_strategist_task_prompt(
    *,
    all_findings_json: str,
    codebase_map_json: str,
    repo_url: str = "",
) -> str:
    """Build the task prompt for the Fix Strategist.

    Args:
        all_findings_json: JSON array of all AuditFindings from agents 2-4.
        codebase_map_json: Serialized CodebaseMap from Agent 1.
        repo_url: Repository URL for context.
    """
    parts = []

    if repo_url:
        parts.append(f"Repository: {repo_url}\n")

    parts.append("## Codebase Map\n")
    parts.append(codebase_map_json)

    parts.append("\n\n## All Findings (from Security, Quality, and Architecture Auditors)\n")
    parts.append(all_findings_json)

    parts.append(
        "\n\nCreate a remediation plan from these findings. "
        "Deduplicate overlapping findings, assign tiers (0-3), "
        "identify dependencies between fixes, and group independent "
        "fixes into parallel execution levels. Defer any findings "
        "that are too risky or low-value to fix."
    )

    return "\n".join(parts)
