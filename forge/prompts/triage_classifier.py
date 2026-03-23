"""Prompt templates for Agent 6: Triage Classifier.

The Triage Classifier assigns a complexity tier (0-3) to each finding.
Uses rule-based fast path for known patterns and LLM fallback for
ambiguous cases.
"""

SYSTEM_PROMPT = """\
You are a triage classifier for a codebase remediation engine.

For each finding, assign a complexity tier that determines how it will be fixed:

## Tier Definitions

- **Tier 0 — Invalid**: The finding references a non-existent file, is a duplicate of
  another finding, or is a false positive. Action: auto-skip with log entry.

- **Tier 1 — Deterministic**: A known pattern with a validated fix template. Examples:
  missing rate limiter on an API route, exposed secret in source code, absent error
  boundary in React. Action: apply pre-computed patch (no LLM needed).

- **Tier 2 — Scoped Fix**: A non-trivial fix localized to 1-3 files. Requires AI
  reasoning but not architectural understanding. Examples: add input validation to
  a form handler, implement proper error handling in an API route, add authentication
  middleware. Action: send to Coder agent with scoped file context.

- **Tier 3 — Architectural**: A cross-cutting concern touching 5+ files with dependency
  implications. Examples: refactor monolithic route handler into service layer,
  implement proper separation of concerns, restructure database access patterns.
  Action: send to Coder agent with full module context and strategist guidance.

## Output Requirements

Respond with a JSON object matching this schema:

```json
{
  "decisions": [
    {
      "finding_id": "F-abc12345",
      "tier": 2,
      "confidence": 0.9,
      "rationale": "Adding auth middleware is a scoped 1-file change",
      "fix_template_id": "",
      "relevant_files": ["src/middleware/auth.ts", "src/routes/admin.ts"]
    }
  ],
  "tier_0_count": 1,
  "tier_1_count": 3,
  "tier_2_count": 5,
  "tier_3_count": 2
}
```

## Guidelines

1. **Be conservative** — when in doubt, assign a higher tier (better to over-prepare)
2. **Check file existence** — if a finding references a file not in the codebase map, it's Tier 0
3. **Count affected files** — 1-3 files = Tier 2, 4+ files = Tier 3
4. **Known patterns** for Tier 1: missing .env.example, exposed API keys, missing rate limiting
   on a single route, missing error boundary wrapper
5. **Tier 3 signals**: "refactor", "restructure", "separation of concerns", "circular dependency"

Respond with ONLY the JSON object, no markdown fencing or explanation.
"""


# ── Rule-based patterns for Tier 1 fast path ──────────────────────────

TIER_1_PATTERNS: list[dict[str, str]] = [
    {
        "pattern": "exposed_secret",
        "keywords": ["hardcoded", "api key", "secret", "password", "token", "credential"],
        "template_id": "replace-hardcoded-secret",
    },
    {
        "pattern": "missing_rate_limit",
        "keywords": ["rate limit", "throttle", "brute force"],
        "template_id": "add-rate-limiter",
    },
    {
        "pattern": "missing_env_example",
        "keywords": [".env.example", "env example", "environment template"],
        "template_id": "create-env-example",
    },
    {
        "pattern": "missing_error_boundary",
        "keywords": ["error boundary", "ErrorBoundary"],
        "template_id": "add-error-boundary",
    },
]

TIER_0_SIGNALS: list[str] = [
    "file not found",
    "does not exist",
    "non-existent",
    "false positive",
    "duplicate of",
]


def triage_classifier_task_prompt(
    *,
    findings_json: str,
    codebase_map_json: str,
) -> str:
    """Build the task prompt for the Triage Classifier.

    Args:
        findings_json: JSON array of AuditFindings to classify.
        codebase_map_json: Serialized CodebaseMap for file existence checks.
    """
    parts = [
        "## Codebase Map\n",
        codebase_map_json,
        "\n\n## Findings to Classify\n",
        findings_json,
        "\n\nClassify each finding into Tier 0-3 based on the complexity "
        "of the fix required. Consider the number of files affected, whether "
        "the fix follows a known pattern, and whether architectural changes "
        "are needed. Include relevant_files for Tier 2 and Tier 3 findings.",
    ]

    return "\n".join(parts)
