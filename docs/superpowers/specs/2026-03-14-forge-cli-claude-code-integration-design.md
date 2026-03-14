# FORGE CLI Integration for Claude Code

**Date:** 2026-03-14
**Status:** Design complete, implementation pending
**Location:** forge-engine repo

---

## Problem

Users want to run FORGE scans from their CLI and have Claude Code autonomously act on the results — scanning a codebase, reading the report, and fixing findings without manual intervention. Today, FORGE is only accessible through the Vibe2Prod web platform or by running the CLI manually with no AI tool integration.

## Key Constraint

FORGE already has a working CLI (`vibe2prod scan`, `vibe2prod fix`, `vibe2prod report`) and a standalone Python API (`run_standalone()`). **No CLI needs to be built.** The problem is purely about the integration layer between FORGE and AI coding tools.

---

## Decision: MCP Server + Skill Doc

### Why MCP (not just Bash)

| Factor | Bash tool | MCP server |
|--------|-----------|------------|
| Timeout | 10min hard cap | No limit |
| `vibe2prod fix` (25-30min) | Impossible | Works |
| Output format | Raw stdout, truncation risk | Structured JSON |
| Progress reporting | None (blank until done) | Real-time updates |
| Tool portability | Claude Code only | Cursor, Windsurf, Cline, Continue |
| Discoverability | User must know to run it | Auto-discovered via `.mcp.json` |
| Permissions | Prompts on first use | Pre-approvable via `mcp__forge__*` |

**The 10-minute Bash timeout makes MCP mandatory for the full autonomous loop.**

### Why a Skill Doc Too

MCP provides the **tools**. The skill doc provides the **workflow intelligence**:

- Scan first, then read and prioritize findings
- Auto-fix security and quality issues (tier 1-2)
- Flag architectural changes for human review (tier 3)
- Re-scan after fixes to verify score improvement
- Cost awareness ($0.30-0.50 per scan, $2-5 per full remediation)
- Stop conditions (diminishing returns, budget limits)

---

## Architecture

```
User's project/
  .mcp.json          <-- auto-discovers FORGE MCP server

forge-engine/
  forge/
    mcp_server.py    <-- FastMCP server (~150 lines, stdio transport)
    claude_skill.md  <-- Workflow orchestration for Claude Code
    standalone.py    <-- Existing: run_standalone() API
    cli.py           <-- Existing: vibe2prod scan/fix/report
```

### MCP Server Tools

| Tool | Description | Calls | Typical duration |
|------|-------------|-------|-----------------|
| `forge_scan` | Discovery scan, returns findings | `run_standalone(mode="discovery")` | ~30s |
| `forge_fix` | Full 12-agent remediation | `run_standalone(mode="full")` | ~25-30min |
| `forge_report` | Read cached `.artifacts/` report | File read | Instant |
| `forge_findings` | Individual finding details | File read | Instant |

The MCP server calls `run_standalone()` **in-process** (not via subprocess), avoiding serialization overhead and enabling native progress reporting through FORGE's telemetry hooks.

### Auto-Discovery

Projects include a `.mcp.json` at their root:

```json
{
  "mcpServers": {
    "forge": {
      "command": "uvx",
      "args": ["--from", "vibe2prod", "python", "-m", "forge.mcp_server"]
    }
  }
}
```

Claude Code (and other MCP-compatible tools) auto-discover FORGE when opening the project.

### Skill Doc Workflow

The autonomous loop Claude follows:

1. **Scan** — `forge_scan(path=".")` to discover findings
2. **Prioritize** — Sort by severity (critical > high > medium > low)
3. **Fix** — For each finding:
   - Read the affected file
   - Apply the suggested fix (or generate one)
   - Run relevant tests
4. **Verify** — `forge_scan(path=".")` again, compare before/after scores
5. **Report** — Show delta: findings resolved, score improvement, cost

Decision rules embedded in the skill doc:
- **Auto-fix:** Security vulnerabilities, missing error handling, type issues, test gaps
- **Flag for human:** Breaking API changes, architectural restructuring, dependency upgrades
- **Cost guardrail:** Warn before triggering full remediation ($2-5)

---

## What We Decided NOT to Build

Based on analysis of cli_research.md recommendations:

| Proposal | Decision | Reason |
|----------|----------|--------|
| Rebuild CLI with Typer+Rich | Skip | CLI already exists and works |
| PageRank repo maps (Aider pattern) | Skip | FORGE agents handle context internally |
| SDK-first refactor (OpenHands pattern) | Skip | `run_standalone()` already IS the SDK |
| Homebrew tap | Skip | Premature — pipx/uvx is sufficient |
| npm MCP wrapper package | Skip | Premature — `uvx` handles Python distribution |
| `.forge/permissions.yaml` | Skip | MCP has its own permission model |

These are valid v2 ideas. Not needed for the integration goal.

---

## Implementation Phases

### Phase 1: MVP (~3-5 days)

1. `forge/mcp_server.py` — FastMCP server with 4 tools
2. `.mcp.json` — auto-discovery config
3. `forge/claude_skill.md` — autonomous workflow doc
4. `pyproject.toml` — add `mcp>=1.0` dependency, MCP entry point

### Phase 2: Polish (week 2)

1. Progress reporting via `ctx.report_progress()` hooked into `ForgeTelemetry`
2. `.claude/settings.json` template with pre-approved permissions
3. README section: "Using FORGE with Claude Code"

---

## Verification Plan

1. Start MCP server locally, call each tool, verify structured JSON returns
2. Register with Claude Code: `claude mcp add forge -- python -m forge.mcp_server`
3. Run full autonomous loop: scan -> read findings -> fix -> re-scan
4. Verify remediation works within MCP (no timeout)
5. Test `.mcp.json` auto-discovery in a fresh project
6. Test with at least one non-Claude tool (Cursor or Windsurf) to confirm portability
