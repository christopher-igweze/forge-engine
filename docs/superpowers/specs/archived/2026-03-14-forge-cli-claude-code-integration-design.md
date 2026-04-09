# FORGE CLI Integration for Claude Code

**Date:** 2026-03-14 (revised 2026-03-15)
**Status:** Design complete, implementation pending
**Location:** forge-engine repo

---

## Problem

Users want to run FORGE scans from their CLI and have Claude Code autonomously act on the results — scanning a codebase, reading the report, and fixing findings without manual intervention. Today, FORGE is only accessible through the Vibe2Prod web platform.

## Design Goals

1. **Maximum privacy** — user's code never leaves their machine
2. **IP protection** — FORGE agent prompts and orchestration logic are not readable
3. **Usage tracking** — anonymous telemetry for product analytics
4. **Optional data sharing** — opt-in anonymized findings for learning loop
5. **Flexible modes** — local (own API key) or cloud (Vibe2Prod credits)

---

## Architecture

### Distribution: Nuitka-compiled binary

FORGE is distributed as a compiled native binary, not readable Python source. The PyPI package contains:

```
vibe2prod (PyPI wheel, per-platform)
├── forge/
│   ├── __init__.py                        # Thin import layer (readable)
│   ├── _core.cpython-312-darwin.so        # Nuitka-compiled binary (ALL IP INSIDE)
│   │   └── Contains: 12 agents, prompts, orchestration, triage,
│   │       scoring, schemas, telemetry client, data sharing client
│   ├── mcp_server.py                      # MCP tool definitions (readable, no IP)
│   ├── claude_skill.md                    # Workflow doc (readable, no IP)
│   └── cli.py                             # CLI entry point (readable, no IP)
├── .mcp.json
└── pyproject.toml
```

**What users CAN see:** MCP tool definitions, CLI interface, skill doc
**What users CANNOT see:** Agent prompts, orchestration logic, triage rules, scoring algorithms, telemetry internals

### Build pipeline (GitHub Actions, private repo)

```
forge-engine (private repo)
  ↓ GitHub Actions on release tag
  ↓
Nuitka compile forge/ → _core.so
  ↓ Per platform: linux-x64, linux-arm64, macos-arm64, windows-x64
  ↓
Build platform-specific wheels
  ↓
Publish to PyPI as `vibe2prod`
```

---

## Execution Modes

### Local mode (default) — maximum privacy

```
User's machine (everything stays here)
┌─────────────────────────────────────────┐
│ claude / cursor / windsurf               │
│   ↓ MCP (stdio)                          │
│ forge/mcp_server.py                      │
│   ↓ in-process call                      │
│ forge/_core.so (compiled binary)         │
│   ↓ LLM calls                           │
│ OpenRouter API (user's own key)          │
│   model: minimax/MiniMax-M1 (default)    │
│                                          │
│ Repo scanned: local filesystem           │
│ Results: local .artifacts/ directory     │
│ Telemetry: anonymous metrics → v2p API   │
└─────────────────────────────────────────┘
```

- User provides `OPENROUTER_API_KEY`
- Code never leaves their machine
- Default model: Minimax M1 (~$0.03-0.05 per scan)
- Override: `--model claude-sonnet-4-6` for higher quality

### Cloud mode (optional) — no API key needed

```
User's machine                     Vibe2Prod infrastructure
┌──────────────────┐              ┌─────────────────────┐
│ MCP Server        │──HTTP──→    │ POST /api/audit      │
│ (thin client)     │             │ → Daytona sandbox    │
│ mode="cloud"      │←─JSON──    │ → FORGE runs here    │
│                   │             │ → charges credits    │
└──────────────────┘              └─────────────────────┘
```

- User provides `VIBE2PROD_API_KEY` (from dashboard)
- Charges against Vibe2Prod credit balance
- Uses existing forge_bridge.py → Daytona pipeline
- For users who don't want to manage OpenRouter keys

### Authenticated mode (optional upgrade)

```bash
vibe2prod auth login    # Opens browser → Clerk auth → stores API key
vibe2prod scan .        # Scan history syncs to web dashboard
```

Unlocks:
- Scan history in web dashboard
- Cross-repo trend tracking
- Team sharing
- Cloud remediation (Tier 3 via SWE-AF)

---

## MCP Server

### Transport: stdio (not HTTP)

stdio is correct for local tooling. Claude Code runs the MCP server as a subprocess — no network, no ports, no auth, no CORS. HTTP MCP is for remote hosted services, which is not the use case here.

### Tools

| Tool | Description | Local mode | Cloud mode |
|------|-------------|------------|------------|
| `forge_scan` | Discovery scan | `_core.run_standalone(mode="discovery")` | `POST /api/audit` |
| `forge_fix` | Full remediation | `_core.run_standalone(mode="full")` | `POST /api/fix-scan/{id}` |
| `forge_report` | Read cached report | Read `.artifacts/` | `GET /api/status/{id}` |
| `forge_findings` | List findings | Read `.artifacts/scan/` | `GET /api/status/{id}` |
| `forge_config` | Show/set config | Read/write `~/.vibe2prod/config.toml` | N/A |

### Mode switching

```python
@mcp.tool()
async def forge_scan(path: str, model: str | None = None, mode: str = "local") -> dict:
    if mode == "local":
        from forge._core import run_standalone
        config = {"mode": "discovery", "model": model or "minimax/MiniMax-M1"}
        result = await run_standalone(path, config)
        await _emit_telemetry("scan_complete", result)
        await _maybe_share_findings(result)  # Only if opted in
        return result.to_dict()
    elif mode == "cloud":
        api_key = os.environ.get("VIBE2PROD_API_KEY")
        if not api_key:
            raise ValueError("VIBE2PROD_API_KEY required for cloud mode")
        return await _cloud_scan(path, api_key)
```

### Auto-Discovery

```json
{
  "mcpServers": {
    "forge": {
      "command": "python",
      "args": ["-m", "forge.mcp_server"]
    }
  }
}
```

### Skill Doc Workflow

The autonomous loop Claude follows:

1. **Scan** — `forge_scan(path=".")` to discover findings
2. **Prioritize** — Sort by severity (critical > high > medium > low)
3. **Fix** — For each finding: read file, apply fix, run tests
4. **Verify** — `forge_scan(path=".")` again, compare before/after scores
5. **Report** — Show delta: findings resolved, score improvement, cost

Decision rules:
- **Auto-fix:** Security vulnerabilities, missing error handling, type issues, test gaps
- **Flag for human:** Breaking API changes, architectural restructuring, dependency upgrades
- **Cost guardrail:** Warn before full remediation (~$0.50-2.00 with Minimax)

---

## Telemetry (always on, anonymous)

### What is collected

```json
{
  "event": "scan_complete",
  "machine_id": "sha256_of_machine_uuid",
  "version": "1.2.0",
  "model": "minimax/MiniMax-M1",
  "mode": "local",
  "scan_type": "discovery",
  "findings_count": 23,
  "findings_by_severity": {"critical": 2, "high": 5, "medium": 10, "low": 6},
  "findings_by_category": {"security": 8, "quality": 10, "architecture": 5},
  "duration_seconds": 31.4,
  "cost_usd": 0.04,
  "readiness_score": 62,
  "repo_stats": {"files": 147, "lines": 12400, "languages": ["python", "javascript"]},
  "timestamp": "2026-03-15T10:00:00Z"
}
```

### What is NOT collected

- Repository name, URL, or any identifying information
- File paths, file contents, or code snippets
- Finding descriptions, titles, or details
- User PII (no email, no name, no IP logged)
- Anything that could identify the project being scanned

### Implementation

```python
# Inside forge/_core.so (compiled, not readable):

async def _emit_telemetry(event_type: str, result) -> None:
    """Fire-and-forget. Never blocks, never fails."""
    if os.environ.get("VIBE2PROD_TELEMETRY", "true").lower() == "false":
        return
    try:
        payload = {
            "event": event_type,
            "machine_id": _hashed_machine_id(),
            "version": __version__,
            "model": result.model,
            "findings_count": result.total_findings,
            "findings_by_severity": result.severity_counts,
            "findings_by_category": result.category_counts,
            "duration_seconds": result.duration_seconds,
            "cost_usd": result.cost_usd,
            "readiness_score": result.readiness_score,
            "repo_stats": _safe_repo_stats(result.repo_path),
            "timestamp": datetime.utcnow().isoformat(),
        }
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                "https://api.vibe2prod.net/api/telemetry",
                json=payload,
                headers={"X-Client": "forge-cli"},
            )
    except Exception:
        pass  # Never fail, never retry, never block
```

### Opt-out

```bash
export VIBE2PROD_TELEMETRY=false
```

Documented in README, CLI help, and skill doc. Transparent and respectful.

---

## Data Sharing Program (opt-in, anonymized)

### Purpose

Build a learning dataset of common codebase problems, what models can fix, fix success rates, and common vulnerability patterns. This data feeds back into FORGE to improve:

- Agent prompt optimization (what instructions produce better fixes)
- Triage accuracy (which findings are actually fixable)
- Model selection (which model handles which finding type best)
- Vulnerability pattern library (prevalence, false positive rates)

### What is shared (only when opted in)

```json
{
  "event": "findings_shared",
  "machine_id": "sha256_of_machine_uuid",
  "version": "1.2.0",
  "findings": [
    {
      "category": "security",
      "severity": "high",
      "type": "sql_injection",
      "cwe_id": "CWE-89",
      "owasp_ref": "A03:2021",
      "language": "python",
      "framework": "fastapi",
      "file_type": ".py",
      "line_count_affected": 3,
      "fix_outcome": "completed",
      "fix_model": "minimax/MiniMax-M1",
      "fix_attempts": 1,
      "fix_duration_seconds": 8.2
    }
  ],
  "repo_profile": {
    "primary_language": "python",
    "frameworks": ["fastapi", "sqlalchemy"],
    "total_files": 147,
    "total_lines": 12400,
    "has_tests": true,
    "has_ci": true
  }
}
```

### What is NEVER shared (even when opted in)

- File paths or file names
- Code content or snippets
- Finding descriptions or titles (only categorization)
- Repository name, URL, or owner
- Suggested fix content
- Any string that could identify the project

### How to opt in

```bash
# One-time opt-in:
vibe2prod config set data_sharing true

# Or environment variable:
export VIBE2PROD_DATA_SHARING=true

# Or in ~/.vibe2prod/config.toml:
[privacy]
data_sharing = true
```

### First-run prompt

On first scan, the CLI shows:

```
FORGE v1.2.0 — AI-powered codebase hardening

Help improve FORGE by sharing anonymized finding patterns?
  - What: vulnerability types, fix success rates, language/framework stats
  - NOT shared: code, file paths, repo identity, finding descriptions
  - Details: https://vibe2prod.net/data-sharing

Share anonymized data? [y/N]:
```

Default is **No**. Opt-in only. Stored in `~/.vibe2prod/config.toml`.

### Backend endpoint

```python
# backend/api/routes/telemetry.py

@router.post("/api/telemetry")
async def ingest_telemetry(event: TelemetryEvent):
    """Anonymous telemetry — no auth required."""
    await supabase_client.store_telemetry(event)
    return {"ok": True}

@router.post("/api/telemetry/findings")
async def ingest_shared_findings(event: SharedFindingsEvent):
    """Opt-in anonymized findings — no auth required."""
    # Validate no PII in payload (defense in depth)
    _strip_potential_pii(event)
    await supabase_client.store_shared_findings(event)
    return {"ok": True}
```

### What you learn from this data

| Insight | How it helps |
|---------|-------------|
| "SQL injection is 23% of all findings" | Prioritize injection-focused agent prompts |
| "Minimax fixes 89% of quality issues but only 62% of security" | Route security to Sonnet, quality to Minimax |
| "FastAPI projects average 31 findings, Next.js averages 18" | Calibrate scoring by framework |
| "CWE-79 (XSS) has 40% false positive rate" | Improve triage classifier for XSS |
| "Fix attempts > 2 correlate with architectural findings" | Better tier classification |
| "Python repos fix in 1.2 attempts avg, JS in 1.8" | Language-specific retry budgets |

This is the **data moat** — no competitor has this dataset because no one else does end-to-end remediation at scale.

---

## Model Configuration

### Default: Minimax M1

```toml
# ~/.vibe2prod/config.toml (or env vars)
[models]
default = "minimax/MiniMax-M1"        # ~$0.03-0.05 per scan
coder = "minimax/MiniMax-M1"           # Remediation coder agent
planner = "minimax/MiniMax-M1"         # Triage and planning agents
analysis = "minimax/MiniMax-M1"        # Discovery analysis agents

# Override for higher quality:
# coder = "anthropic/claude-sonnet-4-6"  # ~$0.30-0.50 per scan
```

### CLI override

```bash
vibe2prod scan . --model claude-sonnet-4-6    # One-off override
vibe2prod config set models.coder claude-sonnet-4-6  # Persistent
```

### Cost comparison

| Model | Scan cost | Fix cost (Tier 2) | Quality |
|-------|-----------|-------------------|---------|
| Minimax M1 | ~$0.03 | ~$0.20 | Good for quality issues, adequate for security |
| Haiku 4.5 | ~$0.08 | ~$0.50 | Fast, good for planning |
| Sonnet 4.6 | ~$0.30 | ~$2.00 | Best code quality, best for complex security |

Default Minimax keeps the barrier to entry low. Users who want higher quality can upgrade per-scan or per-config.

---

## User Configuration

### Config file: `~/.vibe2prod/config.toml`

```toml
[auth]
# Optional — enables dashboard sync, cloud mode, team features
api_key = ""

[models]
default = "minimax/MiniMax-M1"
# coder = "anthropic/claude-sonnet-4-6"  # uncomment for higher quality

[privacy]
telemetry = true          # Anonymous usage metrics (opt-out with false)
data_sharing = false       # Anonymized findings for learning (opt-in)

[mode]
default = "local"          # "local" or "cloud"

[scan]
max_cost_usd = 5.0         # Abort if estimated cost exceeds this
```

### Environment variables (override config file)

```bash
OPENROUTER_API_KEY=sk-or-v1-...     # Required for local mode
VIBE2PROD_API_KEY=v2p_...           # Required for cloud mode
VIBE2PROD_TELEMETRY=true            # Anonymous metrics
VIBE2PROD_DATA_SHARING=false        # Anonymized findings
VIBE2PROD_MODEL=minimax/MiniMax-M1  # Default model
```

---

## Implementation Phases

### Phase 1: Local MVP (~1 week)

1. Update `forge/mcp_server.py` — add mode switching (local/cloud), config loading
2. Update `forge/cli.py` — add `auth login`, `config set/get` commands
3. Add telemetry client to `forge/_core` — fire-and-forget metrics
4. Add `POST /api/telemetry` endpoint to vibe2prod backend
5. Add `~/.vibe2prod/config.toml` support
6. Update `forge/claude_skill.md` — document OPENROUTER_API_KEY requirement, costs
7. Switch default model to Minimax M1

### Phase 2: Data sharing + Nuitka (~1 week)

1. Add data sharing client to `forge/_core` — opt-in findings
2. Add `POST /api/telemetry/findings` endpoint to vibe2prod backend
3. Add first-run opt-in prompt
4. Set up Nuitka build pipeline in GitHub Actions
5. Build per-platform wheels (linux-x64, linux-arm64, macos-arm64)
6. Publish to PyPI as compiled binary

### Phase 3: Cloud mode + Auth (~1 week)

1. Add cloud mode to MCP server (calls forge_bridge.py via Vibe2Prod API)
2. Add `vibe2prod auth login` (Clerk browser flow → store API key)
3. Dashboard scan history for authenticated CLI users
4. Progress reporting via `ctx.report_progress()`

---

## Verification Plan

1. **Local mode:** `OPENROUTER_API_KEY=... vibe2prod scan .` — verify scan completes, findings returned, telemetry sent
2. **MCP integration:** `claude mcp add forge -- python -m forge.mcp_server` — verify all 4 tools work
3. **Autonomous loop:** Claude scans → reads findings → fixes → re-scans → reports delta
4. **Telemetry:** Verify `POST /api/telemetry` receives events, no PII in payload
5. **Data sharing opt-in:** Verify prompt appears on first run, findings sent only when opted in
6. **Opt-out:** `VIBE2PROD_TELEMETRY=false` — verify zero network calls during scan
7. **Nuitka binary:** `python -c "import forge._core"` works, `strings forge/_core.so | grep -i prompt` returns nothing readable
8. **Cloud mode:** `VIBE2PROD_API_KEY=... vibe2prod scan . --mode cloud` — verify scan runs on platform
9. **Model override:** `vibe2prod scan . --model claude-sonnet-4-6` — verify correct model used
10. **Cross-tool:** Test with Cursor or Windsurf to confirm MCP portability

---

## Privacy Summary

| Data | Collected? | When | Can identify project? |
|------|-----------|------|----------------------|
| Finding counts by severity/category | Always (telemetry) | Every scan | No |
| Scan duration, cost, model used | Always (telemetry) | Every scan | No |
| Repo stats (file count, languages) | Always (telemetry) | Every scan | No |
| Machine ID (hashed) | Always (telemetry) | Every scan | No (anonymous) |
| Finding types (CWE, OWASP category) | Opt-in only | When data_sharing=true | No |
| Fix success/failure per finding type | Opt-in only | When data_sharing=true | No |
| Framework/language detection | Opt-in only | When data_sharing=true | No |
| Code, file paths, repo name | **NEVER** | — | — |
| Finding descriptions/titles | **NEVER** | — | — |
| Suggested fix content | **NEVER** | — | — |

**Pitch:** "Your code never leaves your machine. FORGE runs locally with your own API key. We only see anonymous counts — never your code."
