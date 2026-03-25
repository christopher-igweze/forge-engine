---
name: forge
description: Fix security, quality, and architecture issues from a FORGE scan report. Use after running forge_scan via MCP.
argument-hint: (no arguments needed — reads the scan report automatically)
---

# FORGE — Fix Issues from Scan Report

You have a FORGE scan report with security, quality, and architecture findings. Your job is to fix them — or register them in the suppression register if they're false positives.

## Setup

The scan report is in `.artifacts/report/discovery_report.json`. If it doesn't exist, tell the user to run `forge_scan(path=".")` first via the FORGE MCP tool.

## Process

1. **Read the report** — Read `.artifacts/report/discovery_report.json` and parse the findings
2. **Read the suppression register** — Read `.forgeignore` to understand what's already been assessed
3. **Prioritize** — Sort by severity: critical > high > medium > low. Skip `info`.
4. **For each finding, decide: FIX or SUPPRESS**
   - If it's a real issue → fix it (see Decision Rules below)
   - If it's a false positive → register it in `.forgeignore` (see Suppression Register below)
5. **Spin up parallel agents** — Use the Agent tool to fix independent findings in parallel:
   - Group findings by file — fixes to the same file go to one agent
   - Independent files can be fixed simultaneously
6. **For each fix:**
   - Read the affected file(s) listed in `locations`
   - Apply the fix described in `suggested_fix`
   - Match the existing code style exactly
   - Run tests if they exist (`pytest`, `npm test`, etc.)
7. **Re-scan** — Run `forge_scan(path=".")` again to verify improvements
8. **Report** — Show before/after: findings count, readiness score, what was fixed vs suppressed

## Decision Rules

**Auto-fix (do it):**
- Missing error handling → add try/catch
- Missing input validation → add validation
- Hardcoded secrets → move to env vars
- Missing rate limiting → add middleware
- SQL injection → use parameterized queries
- Missing auth checks → add middleware
- Error information exposure → return generic messages
- Missing type hints → add types

**Flag for human (ask first):**
- Architectural restructuring (splitting modules)
- Breaking API changes (function signatures, endpoints)
- Dependency upgrades (version bumps)
- Business logic changes
- Anything that changes external behavior

**Register as suppression (don't fix):**
- Scanner detecting its own detection patterns (e.g., security check code flagged as insecure)
- Checks that don't apply to the project type (e.g., health endpoints for CLI tools, DB checks for file-based tools)
- Findings in test fixtures that are intentionally vulnerable
- Already-fixed issues where the pattern still triggers the detector
- Accepted risks with documented mitigations

## Suppression Register (.forgeignore)

The `.forgeignore` file is the suppression register. It's a YAML list where every entry is a documented decision NOT to fix something. **No silent suppressions** — every entry must explain itself.

### Required fields

Every `.forgeignore` entry MUST have:
- **`type`**: The category of suppression. One of:
  - `false_positive` — Scanner misidentifies code (e.g., detecting its own patterns)
  - `not_applicable` — Check doesn't apply to this project type
  - `already_fixed` — Code was fixed but pattern still triggers
  - `accepted_risk` — Known limitation with documented mitigation
  - `intentional` — Feature that looks like a vulnerability by design
  - `test_fixture` — Intentionally vulnerable test code
- **`reason`**: A clear explanation of WHY this is suppressed, not just WHAT
- At least one **matcher**: `check_id`, `pattern`, or `path`

### Entry format

```yaml
# What: SEC-001 flags remediation_items.py which contains the string "hardcoded secrets"
# as a check template name, not actual secrets.
# Added: 2026-03-23
- check_id: "SEC-001"
  type: "false_positive"
  reason: "Template text referencing 'hardcoded secrets' as a check name, not actual secrets in code."
```

### Matcher types

- `check_id: "SEC-001"` — Suppresses a specific deterministic check by ID
- `pattern: "regex"` — Suppresses LLM findings whose title matches the regex
- `path: "forge/**/file.py"` — Narrows the match to specific files (glob syntax)

### Optional fields

- `category: "security"` — Only suppress findings in this category
- `max_severity: "medium"` — Only suppress at or below this severity
- `expires: "2026-06-01"` — Auto-expire after this date (forces re-evaluation)

### Rules for managing the register

1. **NEVER add an entry without a `reason` and `type`** — the parser rejects them
2. **Prefer fixing over suppressing** — only suppress if the finding genuinely cannot or should not be fixed
3. **Group entries by category** with section headers for readability
4. **Review quarterly** — remove entries for code that's been deleted or checks that no longer trigger
5. **Add dates** in comments so reviewers know when decisions were made
6. **Be specific** — use `check_id` for deterministic checks, `pattern` for LLM findings

## Constraints

- Don't add dependencies unless absolutely necessary
- Run existing tests after each fix to catch regressions
- If a fix is complex, do it in small steps and verify each one
- Commit micro-steps with descriptive messages
- Every finding must be either FIXED or REGISTERED — never silently ignored
