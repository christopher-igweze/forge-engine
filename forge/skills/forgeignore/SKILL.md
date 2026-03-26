---
name: forgeignore
description: Manage .forgeignore — evaluate findings for false positives, write suppression entries, share anonymized data for FORGE training. Can be used standalone or invoked by /forge during triage.
argument-hint: (no arguments needed — reads scan report automatically)
---

# Forgeignore — Suppression Register Management

You are managing the `.forgeignore` file — the suppression register where every decision NOT to fix something is documented.

## When This Skill Is Invoked

- **From /forge**: During triage (step 3), after user confirms false positives
- **Standalone**: User wants to review/update suppressions outside a full audit cycle

## Reading the Current State

1. Read `.forgeignore` in the repo root (may not exist yet — that's OK)
2. Read the latest scan report from `.artifacts/report/discovery_report.json`
3. Understand what's already suppressed vs what's new

## Evaluating Findings

For each finding that may be a false positive, apply these rules:

**Suppress (register in .forgeignore):**
- Scanner detecting its own detection patterns (e.g., security check code flagged as insecure)
- Checks that don't apply to the project type (e.g., health endpoints for CLI tools)
- Findings in test fixtures that are intentionally vulnerable
- Already-fixed issues where the pattern still triggers the detector
- Accepted risks with documented mitigations

**Do NOT suppress:**
- Real security vulnerabilities
- Actual code quality issues
- Anything where you're not confident it's a false positive

When in doubt, ask the user.

## Writing Entries

Every `.forgeignore` entry MUST have:

- **`type`**: One of: `false_positive`, `not_applicable`, `already_fixed`, `accepted_risk`, `intentional`, `test_fixture`
- **`reason`**: Clear explanation of WHY this is suppressed
- At least one **matcher**: `check_id`, `pattern`, or `path`

### Entry Format

```yaml
# What: [brief description of what's being suppressed and why]
# Added: [today's date]
- check_id: "SEC-001"
  type: "false_positive"
  reason: "Template text referencing 'hardcoded secrets' as a check name, not actual secrets in code."
```

### Matcher Types

- `check_id: "SEC-001"` — Suppresses a specific deterministic check by ID
- `pattern: "regex"` — Suppresses LLM findings whose title matches the regex
- `path: "forge/**/file.py"` — Narrows the match to specific files (glob syntax)

### Optional Fields

- `category: "security"` — Only suppress findings in this category
- `max_severity: "medium"` — Only suppress at or below this severity
- `expires: "2026-06-01"` — Auto-expire after this date

## Rules

1. **NEVER add an entry without `reason` and `type`**
2. **Prefer fixing over suppressing** — only suppress if genuinely not fixable
3. **Group entries by category** with YAML comments for readability
4. **Be specific** — use `check_id` for deterministic checks, `pattern` for LLM findings
5. **Add date comments** so reviewers know when decisions were made

## Data Sharing

After writing all entries for this triage cycle, check sharing preferences:

1. Call `forge_config()` via the FORGE MCP tool
2. If `share_forgeignore` is `true`:
   - The sync happens automatically after the next `forge_scan` — no manual POST needed
   - If you want to sync immediately without rescanning, POST to `{forge_config().vibe2prod_url}/api/training/forgeignore`

Note: Automatic sync runs after every scan if sharing is consented. Manual POST is only needed for standalone /forgeignore usage outside a scan cycle.

**What is shared:** suppression type, category, pattern/check_id, and reasoning only.
**What is NOT shared:** repo name, file paths, code content, or any identifying information.

If the POST fails, log a warning and continue — sharing is best-effort, not blocking.

## Constraints

- Never suppress silently — every entry needs a documented reason
- Never modify existing entries without user approval
- If `.forgeignore` doesn't exist, create it with a header comment explaining the format
- Validate all entries have required fields before writing
