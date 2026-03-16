# FORGE — Fix Issues from Scan Report

You have a FORGE scan report with security, quality, and architecture findings. Your job is to fix them.

## Setup

The user should have already run `forge_scan` via MCP. The scan report is in `.artifacts/report/discovery_report.json`. If it doesn't exist, run `forge_scan(path=".")` first.

## Process

1. **Read the report** — Read `.artifacts/report/discovery_report.json` and parse the findings
2. **Prioritize** — Sort by severity: critical > high > medium > low
3. **Fix each finding** using your Edit/Write/Bash tools:
   - Read the affected file(s) listed in the finding's `locations`
   - Apply the fix described in `suggested_fix`
   - Run tests if they exist (`pytest`, `npm test`, etc.)
4. **Track progress** — After fixing each finding, note what you changed
5. **Re-scan** — Run `forge_scan(path=".")` again to verify improvements
6. **Report** — Show before/after: findings count, readiness score delta

## Decision Rules

**Auto-fix (do it):**
- Missing error handling → add try/catch
- Missing input validation → add validation
- Hardcoded secrets → move to env vars
- Missing rate limiting → add middleware
- SQL injection → use parameterized queries
- Missing auth checks → add middleware
- Error information exposure → return generic messages

**Flag for human (don't auto-fix):**
- Architectural restructuring (splitting modules, changing patterns)
- Breaking API changes (changing function signatures, endpoints)
- Dependency upgrades (version bumps)
- Business logic changes

## Tips

- Match the existing code style exactly
- Don't add dependencies unless absolutely necessary
- Run existing tests after each fix to catch regressions
- If a fix is complex, do it in small steps and verify each one
- Skip `info` severity findings — they're informational only
- Use the Agent tool to parallelize independent fixes across different files
