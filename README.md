# FORGE

**Framework for Orchestrated Remediation & Governance Engine**

AI-powered codebase audit engine. 3 LLM agents + Opengrep SAST + 47 deterministic checks. ~$0.21 per scan.

## Quick Start

```bash
# Install
pip install vibe2prod

# Scan a repo (code stays local — only LLM API calls leave your machine)
vibe2prod scan ./my-app

# Register as MCP server in Claude Code
claude mcp add forge -e OPENROUTER_API_KEY=your-key -- python -m forge.mcp_server
```

Get an OpenRouter API key at [openrouter.ai](https://openrouter.ai) (free signup). Scans also work without a key (Opengrep-only mode, no LLM agents).

### The /forge Skill

After scanning, use the `/forge` skill in Claude Code to autonomously fix findings. It reads the scan report, prioritizes issues, and applies fixes with micro-commits.

Full CLI documentation: [vibe2prod.net/cli](https://vibe2prod.net/cli)

## Architecture (v3)

```
Step 1: Codebase Analyst (LLM)     Map architecture, entry points, auth flows, data structures
   |
Step 2: Opengrep SAST              49 custom rules + community rules (deterministic)
   |
Step 3: Security Auditor (LLM)     3 parallel passes against OWASP ASVS rubric
   |
Step 4: Fix Strategist (LLM)       Prioritized remediation plan with dependencies
   |
Step 5: Evaluation (deterministic)  47 checks across 7 dimensions → composite score + quality gate
   |
Post:   Fingerprint → Calibrate → Suppress → Baseline → Report
```

### Agents

| # | Agent | Model | LLM Calls | Role |
|---|-------|-------|-----------|------|
| 1 | Codebase Analyst | MiniMax M2.5 | 1 | Map architecture, files, dependencies |
| 2 | Security Auditor | Haiku 4.5 | 3 (parallel) | OWASP ASVS security scan (auth, data, infra) |
| 3 | Fix Strategist | Haiku 4.5 | 1 | Prioritize findings, create execution plan |

Total: 5 LLM calls per scan.

### Evaluation (Zero LLM Cost)

47 deterministic checks across 7 dimensions:

| Dimension | Weight | Checks |
|-----------|--------|--------|
| Security | 30% | 12 (OWASP Top 10, injection, secrets, etc.) |
| Reliability | 20% | 7 |
| Maintainability | 15% | 5 |
| Test Quality | 15% | 7 |
| Performance | 10% | 5 |
| Documentation | 5% | 6 |
| Operations | 5% | 5 |

Produces a composite score with band ratings: **A** (80+), **B** (60-79), **C** (40-59), **D** (20-39), **F** (0-19). Same code always produces the same score.

## CLI

```bash
vibe2prod scan ./repo              # Full pipeline
vibe2prod report ./repo            # View last report
vibe2prod status ./repo            # Real-time progress
vibe2prod config set/get           # Configuration
vibe2prod setup                    # Interactive setup + Claude Code MCP registration
vibe2prod update                   # Self-update + skill/hook sync
```

## SDK

```python
from forge.standalone import run_standalone

result = await run_standalone(
    repo_path="./my-app",
    config={"mode": "full", "quality_gate_profile": "forge-way"}
)
print(result.total_findings, result.evaluation["scores"]["composite"])
```

## Key Features

**Finding Management**
- Content-based fingerprinting (SHA-256) for stable finding identity across scans
- Cross-scan baseline tracking with delta reports (new/recurring/fixed/regressed)
- `.forgeignore` suppression system (YAML v2 schema) with pattern matching, expiry dates, and audit trail
- Severity calibration: OWASP boost, architecture cap, confidence weighting

**Quality Gates**
- Configurable profiles: `forge-way` (default), `strict`, `startup`
- Pass/fail against thresholds (critical/high/medium counts)
- AIVSS scoring for agentic AI risk assessment (OWASP AIVSS v0.5)
- OWASP ASVS level mapping, STRIDE threat coverage, NIST SSDF compliance

**Reporting**
- HTML, JSON, and text report formats
- Real-time status tracking (`live_status.json`)
- Cost and time budgeting with circuit breakers
- Webhook event emission for CI/CD integration

## Configuration

```python
config = {
    "mode": "full",                    # full | discovery
    "models": {
        "default": "anthropic/claude-haiku-4.5",
        "analyst": "minimax/minimax-m2.5",
    },
    "quality_gate_profile": "forge-way",  # forge-way | strict | startup
    "enable_parallel_audit": True,
    "opengrep_enabled": True,
    "max_cost_usd": 0.50,             # Cost budget (0 = no limit)
    "max_duration_seconds": 600,       # Time budget (0 = no limit)
}
```

Model resolution: built-in defaults → `models.default` → `models.<role>`

## Requirements

- Python 3.10+
- OpenRouter API key (optional — Opengrep-only mode works without one)

## Project Structure

```
forge/
├── cli.py                    # Typer CLI: scan, report, status, config, setup
├── standalone.py             # run_standalone() — primary SDK entry point
├── phases.py                 # Pipeline orchestration
├── mcp_server.py             # MCP server (forge_scan, forge_status tools)
├── config.py                 # ForgeConfig + model routing
├── schemas.py                # Pydantic models: Finding, ForgeResult, etc.
├── reasoners/
│   └── discovery.py          # Codebase Analyst + Security Auditor
├── prompts/                  # Agent prompt templates (OWASP ASVS rubric)
├── execution/
│   ├── opengrep_runner.py    # Opengrep SAST integration
│   ├── fingerprint.py        # SHA-256 content-based finding IDs
│   ├── baseline.py           # Cross-scan delta comparison
│   ├── forgeignore.py        # .forgeignore parser + suppression matching
│   ├── severity.py           # Severity calibration
│   ├── quality_gate.py       # Pass/fail gate
│   └── report_rendering.py   # HTML/JSON report generation
├── evaluation/
│   ├── dimensions.py         # 7-dimension scoring + weights
│   ├── checks/               # 47 deterministic checks
│   ├── compliance.py         # OWASP ASVS/STRIDE/NIST mapping
│   └── aivss.py              # OWASP AIVSS scoring
├── rules/                    # 49 custom Opengrep YAML rules
└── graph/                    # Tree-sitter AST analysis
```

## Part of Vibe2Prod

FORGE is the audit engine behind [vibe2prod.net](https://vibe2prod.net). The web platform adds a dashboard, GitHub integration, team management, and wallet-based billing on top of this engine. Source: [`christopher-igweze/vibe2prod`](https://github.com/christopher-igweze/vibe2prod).
