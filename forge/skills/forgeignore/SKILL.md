---
name: forgeignore
description: Manage .forgeignore — evaluate findings for false positives, write v2 suppression entries with structured matching. Can be used standalone or invoked by /forge during triage.
argument-hint: (no arguments needed — reads scan report automatically)
---

# Forgeignore — Suppression Register Management

You are managing the `.forgeignore` file — the suppression register where every decision NOT to fix something is documented.

**Core principle: Natural language is for humans. Structured anchors are for suppression.**

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

## Writing Entries — v2 Format

The `.forgeignore` file uses v2 format with structured matching. **NEVER use title regex patterns for LLM findings** — titles change between scans and break suppression.

### File Structure

```yaml
version: 2
suppressions:
  # Deterministic check suppressions (use check_id)
  - id: sup_001
    kind: false_positive
    match:
      check_id: "SEC-001"
    reason: Template text referencing 'hardcoded secrets' as a check name, not actual secrets.

  # LLM finding suppressions (use rule_family + file + anchors)
  - id: sup_002
    kind: false_positive
    match:
      rule_family: hardcoded-secret
      file: forge/mcp_server.py
      line_range: [80, 100]
      anchor:
        symbol: _send_telemetry
        snippet_hash: 8f31c2d
    reason: Telemetry sample value, not a real secret.

  # Broader scope (rule_family + file only)
  - id: sup_003
    kind: not_applicable
    match:
      rule_family: missing-rate-limit
      file: forge/cli.py
    reason: CLI tool, not a web server.

  # Test fixtures (file glob)
  - id: sup_004
    kind: test_fixture
    match:
      file: "tests/golden/**"
    reason: Golden codebases are intentionally insecure for testing.
```

### Required Fields

Every entry MUST have:
- **`id`**: Unique suppression ID (e.g., `sup_001`, `sup_002`)
- **`kind`**: One of: `false_positive`, `not_applicable`, `already_fixed`, `accepted_risk`, `intentional`, `test_fixture`
- **`reason`**: Clear explanation of WHY this is suppressed
- **`match`**: At least one matcher (see below)

### Match Criteria (use the right one)

**For deterministic checks (Opengrep, SEC-XXX, etc.):**
```yaml
match:
  check_id: "SEC-001"
```

**For LLM findings — use rule_family, NOT title regex:**
```yaml
match:
  rule_family: hardcoded-secret    # stable slug from RULE_FAMILIES taxonomy
  file: path/to/file.py            # scope to specific file (glob supported)
  line_range: [80, 100]            # narrow to line range (optional)
  anchor:                          # resilient anchors (optional)
    symbol: function_name          # enclosing function/class
    snippet_hash: abc123def        # hash of code snippet
```

**For entire directories (test fixtures, vendored code):**
```yaml
match:
  file: "tests/golden/**"
```

### Rule Family Taxonomy

Use these standard slugs for `rule_family`. Find the finding's `rule_family` field in the scan report:

Security: `hardcoded-secret`, `sql-injection`, `xss`, `path-traversal`, `command-injection`, `ssrf`, `idor`, `missing-auth-check`, `missing-rate-limit`, `insecure-deserialization`, `weak-crypto`, `sensitive-data-exposure`, `missing-input-validation`, `insecure-tls`, `open-redirect`, `csrf`, `session-fixation`, `error-info-leak`, `missing-security-headers`, `cors-misconfiguration`

Quality: `missing-error-handling`, `missing-type-hints`, `dead-code`, `code-duplication`, `complex-function`, `missing-logging`

Architecture: `circular-dependency`, `god-class`, `tight-coupling`, `missing-abstraction`, `config-in-code`

Reliability: `unhandled-exception`, `resource-leak`, `race-condition`, `missing-timeout`, `missing-retry`

Performance: `n-plus-one`, `missing-pagination`, `blocking-io`, `missing-cache`, `missing-index`

### Optional Fields

- `category: "security"` — Only suppress findings in this category
- `max_severity: "medium"` — Only suppress at or below this severity
- `expires: "2026-06-01"` — Auto-expire after this date (forces re-evaluation)

### How to Get Anchor Values

When writing a suppression for an LLM finding:

1. **`rule_family`**: Read from the finding's `rule_family` field in the scan report
2. **`file`**: Read from `locations[0].file_path`
3. **`line_range`**: Read from `locations[0].line_start` and `line_end`, round to nearest range
4. **`symbol`**: Read from the finding's `enclosing_symbol` field, or look at the code to find the enclosing function/class
5. **`snippet_hash`**: Read from the finding's `evidence_hash` field

### Matching Precedence

The system matches suppressions in this order (first match wins):
1. Exact `check_id` match (strongest — for deterministic checks)
2. `rule_family` + `file` + `line_range` (precise location)
3. `rule_family` + `file` + `symbol` (function-scoped)
4. `rule_family` + `file` (file-scoped)
5. Title regex (legacy v1 fallback only — avoid writing new ones)

## Rules

1. **NEVER use `pattern:` (title regex) for new suppressions** — use `rule_family` + `file` instead
2. **NEVER add an entry without `reason` and `kind`**
3. **Prefer fixing over suppressing** — only suppress if genuinely not fixable
4. **Scope narrowly** — use file + line_range + anchor when possible, not just rule_family alone
5. **Add expiry dates** for accepted risks so they get re-evaluated
6. **Group entries by category** with YAML comments for readability

## Migrating v1 to v2

If the `.forgeignore` is in v1 format (list of entries without `version: 2` header):
1. Add `version: 2` at the top
2. Wrap entries in `suppressions:` list
3. Convert `check_id` entries: move into `match.check_id`
4. Convert `pattern` entries: replace with `rule_family` + `file` from the finding's structured data
5. Convert `type` to `kind`
6. Add unique `id` to each entry

## Data Sharing

After writing all entries for this triage cycle, check sharing preferences:

1. Call `forge_config()` via the FORGE MCP tool
2. If `share_forgeignore` is `true`:
   - The sync happens automatically after the next `forge_scan` — no manual POST needed
   - If you want to sync immediately without rescanning, POST to `{forge_config().vibe2prod_url}/api/training/forgeignore`

Note: Automatic sync runs after every scan if sharing is consented. Manual POST is only needed for standalone /forgeignore usage outside a scan cycle.

**What is shared:** suppression type, category, rule_family, and reasoning only.
**What is NOT shared:** repo name, file paths, code content, or any identifying information.

## Constraints

- Never suppress silently — every entry needs a documented reason
- Never modify existing entries without user approval
- If `.forgeignore` doesn't exist, create it with `version: 2` header
- Validate all entries have required fields before writing
- Never use title regex for LLM findings — it will break on the next scan
