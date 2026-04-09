# FORGE CLI Improvements — Design Spec

**Date:** 2026-03-25
**Status:** Approved

## Overview

Improve the FORGE CLI to work gracefully without an OpenRouter API key (deterministic-only mode), consolidate scan/fix into a single command, add MCP scope selection and forgeignore data-sharing consent to the setup wizard, feed `.forgeignore` into the LLM security auditor, and restructure the `/forge` skill flow with a new `/forgeignore` skill.

## Changes

### 1. Drop `fix` Command — Consolidate into `scan`

`vibe2prod fix` is deleted entirely (not deprecated). `vibe2prod scan` now runs the full pipeline (`mode: "full"`) and always includes a remediation plan in the report.

- `scan` with API key: Opengrep + LLM agents + deterministic evaluation + remediation plan
- `scan` without API key: Opengrep + deterministic evaluation + rule-based remediation suggestions

The report always includes findings, scores, quality gate result, and remediation guidance regardless of mode.

**Migration:** Any existing scripts calling `vibe2prod fix` will get an unknown command error. This is acceptable for a 1.x release with no stable API contract.

### 2. Graceful Degradation — No API Key / Runtime Failures

The pipeline runs what it can with what's available. No hard dependency on OpenRouter.

**No API key at startup:**
- Skip all LLM steps (codebase analyst, security auditor, fix strategist)
- Run Opengrep SAST, deterministic scoring, quality gate, AIVSS, compliance mapping
- Produce report with rule-based remediation suggestions
- Setup wizard makes API key optional: "Press Enter to skip for deterministic-only mode"

**API key present but call fails at runtime:**
- Retry once on failure
- If still fails, log warning and skip that agent
- Continue with remaining pipeline steps
- Each agent call in `phases.py` wrapped in try/except with retry logic

**Credits exhausted mid-scan:**
- Remaining LLM agents get skipped
- Deterministic steps still run
- Report reflects partial analysis

**Partial LLM results:**
- If some agents succeeded and others failed, use what you got
- Report indicates which agents ran: `Agents: 3/5 completed (security auditor skipped: API error)`
- Add `agents_status` field to `ForgeResult` schema: `list[{agent: str, status: "completed"|"skipped", reason: str | None}]`

**Claude Code without OpenRouter key:**
- MCP server works, scan runs deterministic-only
- `/forge` skill works — Claude reads the deterministic report and can triage, manage forgeignore, and apply fixes
- Claude IS the LLM at that point; it just acts on the report findings

### 3. Setup Wizard — New Flow

#### Interactive TUI (6 steps)

**Step 1 — OpenRouter API Key (now optional)**
- "Enter your OpenRouter API key (press Enter to skip for deterministic-only mode)"
- If skipped: inform user that Opengrep, scoring, quality gate, compliance all work without a key
- Can be added later: `vibe2prod config set openrouter_api_key sk-or-...`

**Step 2 — Dashboard Sync (optional, V2P key)**
- Unchanged from current behavior

**Step 3 — Data Sharing Consent**
- "Help improve FORGE by sharing anonymized .forgeignore suppression data after scans? This shares suppression patterns and reasoning only — no code, file paths, or repo names. (y/N)"
- Saves `share_forgeignore: true/false` to `~/.vibe2prod/config.json`
- Independent of dashboard sync — user can share training data without a V2P account

**Step 4 — Claude Code Integration**
- If Claude Code detected:
  - "Register FORGE for all projects (user) or just this project? [user/project]" Default: user
  - Registers MCP server with chosen scope
  - Installs `/forge` and `/forgeignore` skills to `~/.claude/commands/`
- If not detected: skip, show CLI usage guide

**Step 5 — Getting Started**
- If Claude Code integrated: "You're set! Open any project and ask Claude to scan it, or type `/forge` to run the full audit flow."
- If no Claude Code: print CLI quickstart covering `vibe2prod scan`, `vibe2prod report`, `vibe2prod status`, `.forgeignore` manual management, quality gate profiles, and artifact locations
- Either way: mention that `vibe2prod scan` works with or without API key

**Step 6 — Summary**
- Shows all choices: API key (masked), dashboard, data sharing, Claude scope

#### Headless Mode

New flags:
- `--share-forgeignore` / `--no-share-forgeignore` (default: true)
- `--scope user|project` (default: user)
- `--api-key` is now optional (omit for deterministic-only)

### 4. Discovery Pipeline — .forgeignore in LLM Prompts

When `.forgeignore` exists in the repo, inject its full content into the security auditor's prompt context.

**Implementation in `phases.py`:**
- Before calling the security auditor, check for `.forgeignore` in repo root
- If found, serialize all rules (patterns, check IDs, types, reasons) into a "Previously Assessed Findings" section in the prompt
- Prompt instruction: "The following findings have been reviewed and suppressed by the project maintainers. Do not re-flag these patterns or reword them to bypass suppression. Focus on genuinely new issues not covered by existing suppressions."
- If `.forgeignore` doesn't exist (first scan): auditor runs as before, no change
- Read-only — auditor never writes to `.forgeignore`

**Post-discovery filtering unchanged:**
- Deterministic forgeignore filtering still runs after discovery as a safety net
- Catches anything the LLM flagged despite being told not to

### 5. /forge Skill — New Flow

**Old flow:** scan -> fix/suppress -> rescan -> report

**New flow:**

1. **Scan** — call `forge_scan(path=".")`, get findings
2. **Triage** — evaluate each finding for false positives
   - Read report from `.artifacts/report/discovery_report.json`
   - Read existing `.forgeignore` if present
   - Assess each finding: real issue, false positive, not applicable, accepted risk
   - Present grouped assessment to user: "here are the real issues, here are the ones I think are false positives"
   - **Wait for user confirmation before touching .forgeignore**
3. **Update .forgeignore** — invoke `/forgeignore` skill for confirmed false positives
   - Writes properly formatted entries
   - Shares anonymized data to training endpoint (if consented)
4. **Fix real issues** — apply fixes for confirmed findings
   - Parallel agents grouped by file
   - Run tests after each fix
   - Micro-commits
5. **Rescan** — call `forge_scan(path=".")` to verify
   - Compare before/after scores
   - Confirm fixed findings resolved, no regressions
6. **Discuss** — present results to user
   - Before/after: score, finding count, fixed vs suppressed
   - Remaining findings needing human decision
   - Recommendations for next steps

### 6. /forgeignore Skill (New)

Standalone skill for managing `.forgeignore`. Can be invoked independently or called by `/forge` during triage.

**Responsibilities:**
- Read and parse existing `.forgeignore`
- Evaluate findings for false positives (with reasoning)
- Write properly formatted entries (type, reason, matchers, optional expiry)
- Validate entries match schema (reject entries without reason/type)
- Share anonymized data to training endpoint after batch update (if user consented during setup)

**Training data payload** (sent once per triage cycle to `api.vibe2prod.net/api/training/forgeignore`):
```json
{
  "repo_hash": "<anonymized SHA-256 of repo remote URL or name>",
  "entries_added": 3,
  "entries": [
    {
      "type": "false_positive",
      "category": "security",
      "pattern": "...",
      "reason": "..."
    }
  ],
  "scan_mode": "full|deterministic_only",
  "version": "1.1.0"
}
```

No repo name, no file paths, no code — just suppression patterns and reasoning.

**Sharing trigger:** once per triage cycle, after all false positives are confirmed and written. Not per-entry.

## Files Affected

### CLI & Config
- `forge/cli.py` — delete `fix` command, make API key optional in `scan` (change `_check_api_key` to return `None` instead of exiting), add graceful degradation
- `forge/setup_wizard.py` — add MCP scope choice, data sharing consent, optional API key, getting started guide, multi-skill installation (`install_skill()` accepts skill name param)
- `forge/config.py` — add `share_forgeignore: bool = True` field to `ForgeConfig`
- `forge/config_io.py` — add `share_forgeignore` to `KNOWN_KEYS`

### Pipeline & Discovery
- `forge/phases.py` — load `.forgeignore` and inject into security auditor prompt, wrap each agent call in try/except with retry, add agent skip/continue logic
- `forge/reasoners/discovery.py` — accept `forgeignore_context: str | None` param in security auditor function, append to prompt
- `forge/standalone.py` — handle no-API-key mode (skip LLM agents, run deterministic-only pipeline)
- `forge/schemas.py` — add `agents_status: list[AgentStatus]` to `ForgeResult` for partial-analysis reporting

### MCP & Skills
- `forge/mcp_server.py` — remove API key hard-fail in `forge_scan()`, allow deterministic-only scan
- `forge/skills/forge/SKILL.md` — rewrite with new 6-step flow (scan → triage → forgeignore → fix → rescan → discuss)
- `forge/skills/forgeignore/SKILL.md` — new skill file (create `forge/skills/forgeignore/` directory)

### Prompts
- `forge/prompts/security_auditor.py` — add "Previously Assessed Findings" section template for forgeignore injection

### Tests (update existing, add new)
- `tests/unit/test_cli.py` — remove `fix` command tests, add deterministic-only scan tests
- `tests/unit/test_setup_wizard.py` — add scope choice, data sharing consent, multi-skill install tests
- `tests/unit/test_mcp_server.py` — add no-API-key scan test
- `tests/unit/test_phases.py` — add agent retry/skip tests, forgeignore injection test
- `tests/integration/` — add deterministic-only pipeline end-to-end test
