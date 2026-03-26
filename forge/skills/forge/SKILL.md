---
name: forge
description: Full FORGE audit cycle — scan, triage false positives, fix real issues, verify. Use after running forge_scan via MCP or as a standalone workflow.
argument-hint: (no arguments needed — scans and reads reports automatically)
---

# FORGE — Audit, Triage, Fix, Verify

You are running the full FORGE audit cycle. Follow these 6 steps in order.

## Step 1: Scan

Run the FORGE scan on the current project:
- Call `forge_scan(path=".")` via the FORGE MCP tool
- Wait for the scan to complete
- If the scan fails, report the error and stop

## Step 2: Triage

Evaluate each finding — is it real or a false positive?

1. Read the report from `.artifacts/report/discovery_report.json`
2. Read existing `.forgeignore` if it exists
3. For each finding, assess:
   - **Real issue** — needs to be fixed
   - **False positive** — scanner misidentified code
   - **Not applicable** — check doesn't apply to this project type
   - **Accepted risk** — known limitation with documented mitigation
   - **Intentional** — feature that looks like a vulnerability by design
   - **Test fixture** — intentionally vulnerable test code
4. Present your assessment grouped:
   - "These are real issues that should be fixed: [list]"
   - "These appear to be false positives: [list with reasoning]"
   - "These need your decision: [list]"
5. **Wait for user confirmation before proceeding**

## Step 3: Update .forgeignore

For confirmed false positives, invoke the `/forgeignore` skill to register them properly.

Do NOT proceed to fixing until the user has confirmed the triage assessment.

## Step 4: Fix Real Issues

Apply fixes for confirmed real findings:

1. Sort by severity: critical > high > medium > low. Skip `info`.
2. Use the Agent tool to fix independent findings in parallel:
   - Group findings by file — fixes to the same file go to one agent
   - Independent files can be fixed simultaneously
3. For each fix:
   - Read the affected file(s)
   - Apply the fix described in `suggested_fix` from the report
   - Match existing code style
   - Run tests if they exist (`pytest`, `npm test`, etc.)
4. Commit micro-steps with descriptive messages

**Decision Rules:**

Auto-fix (do it):
- Missing error handling, input validation
- Hardcoded secrets → move to env vars
- Missing rate limiting, auth checks
- SQL injection → parameterized queries
- Error information exposure → generic messages

Flag for human (ask first):
- Architectural restructuring
- Breaking API changes
- Dependency upgrades
- Business logic changes

## Step 5: Rescan

Run `forge_scan(path=".")` again to verify improvements:
- Compare before/after scores
- Confirm fixed findings are resolved
- Check for regressions (new findings introduced by fixes)
- If regressions found, fix them and rescan

## Step 6: Discuss

Present results to the user:
- **Before/after comparison**: score, finding count, quality gate status
- **What was fixed**: list of resolved findings
- **What was suppressed**: list of .forgeignore entries added
- **Remaining**: any findings that still need human decision
- **Recommendations**: next steps, areas to watch

## Constraints

- Don't add dependencies unless absolutely necessary
- Run existing tests after each fix to catch regressions
- Every finding must be either FIXED or REGISTERED in .forgeignore — never silently ignored
- Commit micro-steps with descriptive messages
- If a fix is complex, do it in small steps and verify each one
