# FORGE Autonomous Workflow

Use the FORGE MCP tools to audit and harden codebases for production. This document describes the workflow for autonomous codebase remediation.

## When to Use

- User asks to "audit", "scan", "harden", or "check" a codebase
- User wants to find and fix security vulnerabilities, quality issues, or architectural problems
- User wants a Production Readiness Score

## Workflow

### Step 1: Scan

Run discovery to identify all issues:

```
forge_scan(path=".")
```

This takes ~30 seconds and costs ~$0.30-0.50. Returns structured findings with severity levels.

### Step 2: Read and Prioritize Findings

After scanning, read the detailed findings:

```
forge_findings(path=".")
```

Prioritize by severity: **critical > high > medium > low**

Focus on:
1. Security vulnerabilities (SQL injection, XSS, auth bypass, secrets in code)
2. Missing error handling and input validation
3. Type safety issues
4. Test coverage gaps
5. Architecture concerns (last priority)

### Step 3: Fix Findings

For each finding (starting with critical/high severity):
1. Read the affected file(s)
2. Apply the suggested fix or generate a correct one
3. Run relevant tests to verify the fix
4. Commit each fix as a micro-commit

### Step 4: Verify

Re-scan to measure improvement:

```
forge_scan(path=".")
```

Compare before/after:
- Total findings count
- Findings by severity
- Production Readiness Score delta

### Step 5: Report

Present the results:
- Findings resolved (count and list)
- Score improvement (before -> after)
- Estimated cost of scans
- Remaining findings that need human review

## Decision Rules

### Auto-Fix (Tier 1-2)
- Security vulnerabilities: missing auth, injection flaws, exposed secrets
- Quality issues: error handling, input validation, type safety
- Test gaps: missing tests for critical paths

### Flag for Human Review (Tier 3)
- Breaking API changes
- Architectural restructuring
- Major dependency upgrades
- Changes affecting external integrations

### Full Remediation

For comprehensive automated fixing (12-agent pipeline):

```
forge_fix(path=".", dry_run=true)
```

Review the plan, then run without dry_run. WARNING: Takes ~25-30 minutes, costs ~$2-5.

## Cost Awareness

| Operation | Duration | Cost |
|-----------|----------|------|
| `forge_scan` | ~30s | $0.30-0.50 |
| `forge_fix` (dry run) | ~30s | $0.30-0.50 |
| `forge_fix` (full) | ~25-30min | $2-5 |
| `forge_report` | instant | $0 |
| `forge_findings` | instant | $0 |

## Stop Conditions

- All critical and high severity findings are resolved
- Score improvement < 5 points between scans (diminishing returns)
- Budget limit reached (warn user before exceeding $5 total)
- Only Tier 3 (architectural) findings remain — flag for human review

## Reading Cached Results

To check results from a previous run without making API calls:

```
forge_report(path=".")    # Full report with scores
forge_findings(path=".")  # Individual finding details
```
