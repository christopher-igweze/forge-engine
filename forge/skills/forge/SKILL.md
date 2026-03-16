---
name: forge
description: Fix security, quality, and architecture issues from a FORGE scan report. Use after running forge_scan via MCP.
argument-hint: (no arguments needed — reads the scan report automatically)
---

# FORGE — Fix Issues from Scan Report

You have a FORGE scan report with security, quality, and architecture findings. Your job is to fix them.

## Setup

The scan report is in `.artifacts/report/discovery_report.json`. If it doesn't exist, tell the user to run `forge_scan(path=".")` first via the FORGE MCP tool.

## Process

1. **Read the report** — Read `.artifacts/report/discovery_report.json` and parse the findings
2. **Prioritize** — Sort by severity: critical > high > medium > low. Skip `info`.
3. **Spin up parallel agents** — Use the Agent tool to fix independent findings in parallel:
   - Group findings by file — fixes to the same file go to one agent
   - Independent files can be fixed simultaneously
4. **For each finding:**
   - Read the affected file(s) listed in `locations`
   - Apply the fix described in `suggested_fix`
   - Match the existing code style exactly
   - Run tests if they exist (`pytest`, `npm test`, etc.)
5. **Re-scan** — Run `forge_scan(path=".")` again to verify improvements
6. **Report** — Show before/after: findings count, readiness score, what was fixed

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

## Constraints

- Don't add dependencies unless absolutely necessary
- Run existing tests after each fix to catch regressions
- If a fix is complex, do it in small steps and verify each one
- Commit micro-steps with descriptive messages
- If a finding seems like a false positive, skip it and note why
