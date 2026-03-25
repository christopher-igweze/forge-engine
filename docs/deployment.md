# Vibe2Prod (FORGE Engine) — Deployment & Operations Guide

Version: 0.3.0

---

## 1. Two Execution Modes

| Mode | Install | Use Case |
|---|---|---|
| **CLI (standalone)** | `pip install vibe2prod` | Local use. Code stays on your machine. User brings own API key. |
| **Platform (Daytona)** | `pip install vibe2prod[platform]` | Production. Runs in Daytona sandboxes via AgentField. |

---

## 2. CLI Mode (Standalone)

### Quick Start

```bash
pip install vibe2prod

export OPENROUTER_API_KEY=sk-or-v1-...

vibe2prod scan ./my-app           # Discovery only (scan + triage)
vibe2prod fix ./my-app            # Full pipeline (scan + fix + validate)
vibe2prod report ./my-app         # View report from last run
```

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required by `pyproject.toml` |
| git | any recent | Worktree support for parallel fix isolation |
| OpenRouter API key | -- | All LLM calls route through OpenRouter |
| opencode | v1.2+ | Required for coder agents (Tier 2/3), test generator |

Optional:
- `weasyprint >= 60.0` — PDF report generation (`pip install vibe2prod[pdf]`)

### CLI Commands

#### `vibe2prod scan <path>`

Discovery mode: runs Agents 1-6 (codebase analysis, security audit, quality audit, architecture review, triage, fix strategy). Produces findings but does **not** apply fixes.

```bash
vibe2prod scan ./my-app
vibe2prod scan ./my-app --model anthropic/claude-haiku-4.5
vibe2prod scan ./my-app --json          # JSON output
vibe2prod scan ./my-app --verbose       # Debug logging
```

#### `vibe2prod fix <path>`

Full remediation pipeline: all 12 agents. Fixes are applied in isolated git worktrees and merged back on success.

```bash
vibe2prod fix ./my-app
vibe2prod fix ./my-app --coder-model anthropic/claude-sonnet-4.6
vibe2prod fix ./my-app --max-retries 2
vibe2prod fix ./my-app --dry-run        # Plan without applying fixes
```

#### `vibe2prod report <path>`

Display the report from the last FORGE run.

```bash
vibe2prod report ./my-app               # Pretty text output
vibe2prod report ./my-app --format json  # Raw JSON
vibe2prod report ./my-app --format html  # HTML report
```

### Code Privacy

In CLI mode, your code **never leaves your machine**. Only LLM API calls are sent to OpenRouter. No AgentField server, no Daytona sandbox, no telemetry phoning home.

---

## 3. Platform Mode (Daytona Sandboxes)

Production deployment runs in **Daytona sandboxes** (Linux containers). This is the primary deployment target for the Vibe2Prod platform.

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required by `pyproject.toml` |
| AgentField | >= 0.1.9 | Agent runtime and control plane |
| opencode | v1.2+ | Coder agents (Tier 2/3), test generator, integration validator |
| OpenRouter API key | -- | All LLM calls route through OpenRouter |
| git | any recent | Worktree support for parallel fix isolation |
| Daytona | -- | Ephemeral sandbox orchestration |

Optional:
- `weasyprint >= 60.0` — PDF report generation (`pip install vibe2prod[platform,pdf]`)
- `claude-agent-sdk >= 0.1.20` — Claude provider support (`pip install vibe2prod[platform,claude]`)

### Install

```bash
pip install vibe2prod[platform]       # core + AgentField
pip install vibe2prod[platform,pdf]   # with PDF report generation
```

### Start the Node

```bash
# Via entry point
forge-engine

# Via module
python -m forge

# With custom port and server
FORGE_PORT=9000 AGENTFIELD_SERVER=http://control-plane:8080 python -m forge
```

The node registers itself with the AgentField control plane at startup.

### Calling Reasoners via AgentField

```bash
agentfield call forge-engine.remediate \
  --arg repo_path=/path/to/repo \
  --arg config='{"mode": "full"}'
```

---

## 4. Environment Variables

### Required (both modes)

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter. All LLM calls fail without this. |

### Platform mode only

| Variable | Default | Description |
|---|---|---|
| `FORGE_NODE_ID` | `forge-engine` | AgentField node identifier |
| `WORKSPACES_DIR` | `/workspaces` | Directory for cloning repos when `repo_url` is provided |
| `AGENTFIELD_SERVER` | `http://localhost:8080` | AgentField control plane URL |
| `AGENTFIELD_API_KEY` | (none) | AgentField authentication key |
| `FORGE_PORT` | `8004` | Port the FORGE node listens on |
| `FORGE_HOST` | `0.0.0.0` | Host the FORGE node binds to |
| `FORGE_DEBUG` | `false` | Enable debug logging. Never enable in production. |

### Secret Management

**NEVER commit API keys or secrets to version control.** Follow these practices:

- **OPENROUTER_API_KEY** and **AGENTFIELD_API_KEY** are sensitive credentials. Load them from:
  - Environment variables (preferred for CI/CD and containers)
  - GitHub Secrets / AWS Secrets Manager / HashiCorp Vault (for production)
  - System keyring via `vibe2prod setup` (for local development)
- Add `*.env` and `.env*` to `.gitignore` — never commit `.env` files
- Rotate any credentials that may have been exposed in git history
- Use `detect-secrets` or similar pre-commit hooks to prevent accidental key commits
- The CLI stores config at `~/.vibe2prod/config.json` with `0o600` permissions (owner-only read/write). For additional security, use environment variables instead of file storage

---

## 5. Configuration

### ForgeConfig Fields

The `config` dict (passed via CLI `--model` flags or API payload) is validated against `ForgeConfig`. All fields have defaults.

| Field | Type | Default | Description |
|---|---|---|---|
| `runtime` | string | `"open_code"` | Runtime backend identifier |
| `models` | dict | `null` | Per-role model overrides (see Model Routing) |
| `mode` | enum | `"full"` | Pipeline mode: `full`, `discovery`, `remediation`, `validation` |
| `max_inner_retries` | int | `3` | Inner loop: max coder retries on REQUEST_CHANGES |
| `max_middle_escalations` | int | `2` | Middle loop: max escalation attempts |
| `max_outer_replans` | int | `1` | Outer loop: how many times to re-run Fix Strategist |
| `agent_timeout_seconds` | int | `900` | Per-agent timeout (15 minutes) |
| `enable_tier0_autofix` | bool | `true` | Enable Tier 0 auto-skip for invalid/noise findings |
| `enable_tier1_rules` | bool | `true` | Enable Tier 1 deterministic template fixes |
| `enable_parallel_audit` | bool | `true` | Run discovery audit agents (2-4) concurrently |
| `enable_learning` | bool | `true` | Log training data pairs |
| `dry_run` | bool | `false` | Scan only, no fixes applied |

### Model Routing

Model resolution cascade: `FORGE_DEFAULT_MODELS < models.default < models.<role>`

```bash
# CLI override
vibe2prod fix ./my-app --model anthropic/claude-haiku-4.5
vibe2prod fix ./my-app --coder-model anthropic/claude-sonnet-4.6
```

Default model assignments:

| Role | Default Model | Provider | Purpose |
|---|---|---|---|
| `codebase_analyst` | `minimax/minimax-m2.5` | openrouter_direct | Cheap analysis |
| `quality_auditor` | `minimax/minimax-m2.5` | openrouter_direct | Cheap analysis |
| `debt_tracker` | `minimax/minimax-m2.5` | openrouter_direct | Cheap analysis |
| `security_auditor` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `architecture_reviewer` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `fix_strategist` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `triage_classifier` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `test_generator` | `anthropic/claude-haiku-4.5` | opencode | Needs file tools |
| `code_reviewer` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `integration_validator` | `anthropic/claude-haiku-4.5` | opencode | Needs file tools |
| `coder_tier2` | `anthropic/claude-sonnet-4.6` | opencode | Frontier coding |
| `coder_tier3` | `anthropic/claude-sonnet-4.6` | opencode | Frontier coding |

**Model ID format:** `provider/model` (e.g., `anthropic/claude-sonnet-4.6`). Do NOT prefix with `openrouter/`.

### Provider Routing

- **openrouter_direct** — stdlib HTTPS client. Text-in, JSON-out. Used for analysis/planning agents.
- **opencode** — Invokes `opencode run` as subprocess with file-editing tools. Used for coders, test generator, integration validator.

---

## 6. Monitoring

### Telemetry

When `enable_learning` is `true` (default), telemetry is written to `<repo>/.artifacts/telemetry/`:

| File | Format | Contents |
|---|---|---|
| `cost_summary.json` | JSON | Cost by agent/model, total tokens, invocations |
| `invocations.jsonl` | JSONL | Per-invocation: model, tokens, cost, latency |
| `training_data.jsonl` | JSONL | Per-finding: metadata, tier, outcome, files changed |

### Checkpoints

Saved at phase boundaries in `<repo>/.forge-checkpoints/`. Auto-cleared on success, persist on crash for resume.

### Reports

Written to `<repo>/.artifacts/report/`. Two report types:

- **Discovery Report**: Findings + architecture context (modules, entry points, key patterns, data flows, auth boundaries, finding hotspots) + remediation plan. Includes LOC total, file count, and primary language in the meta bar. Generated as JSON + HTML.
- **Production Readiness Report**: Production readiness score (0-100) + category scores + tech debt summary + recommendations. Generated as JSON + HTML + optional PDF (if weasyprint installed).

### Worktrees

Each Tier 2/3 fix gets its own git worktree under `<repo>/.forge-worktrees/`. Cleaned up after each run.

---

## 7. Troubleshooting

**"OPENROUTER_API_KEY is not set"**
```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

**"AgentField is not installed"**

You're in standalone mode. Use the CLI directly:
```bash
vibe2prod scan ./my-app
```

Or install AgentField for platform mode:
```bash
pip install vibe2prod[platform]
```

**Agent timeout (15 minutes default)**
```bash
# Via CLI: not yet configurable, increase in config dict
# Via API: {"config": {"agent_timeout_seconds": 1800}}
```

**opencode hangs (macOS development only)**

The `opencode run` command can hang on macOS due to non-TTY detection. This does NOT affect production (Daytona/Linux). For local development on macOS, use the Python wrapper at `~/.opencode/bin/opencode`.

**Crash recovery**

FORGE auto-resumes from the latest checkpoint. To force a fresh run:
```bash
rm -rf /path/to/repo/.forge-checkpoints/
```

**Stale worktrees**
```bash
rm -rf .forge-worktrees/
git worktree prune
git branch --list 'forge/fix-*' | xargs -r git branch -D
```

---

## 8. Directory Layout (Runtime)

```
<repo>/
  .artifacts/
    telemetry/
      cost_summary.json
      invocations.jsonl
      training_data.jsonl
    report/
      discovery_report.json     # discovery mode
      discovery_report.html     # discovery mode
      production_readiness.json # full pipeline
      production_readiness.html # full pipeline
      production_readiness.pdf  # if weasyprint installed
  .forge-checkpoints/           # cleared on success
  .forge-worktrees/             # cleared after run
```

Add to `.gitignore`:
```
.forge-checkpoints/
.forge-worktrees/
.artifacts/telemetry/
```

---

## 9. Development

### Local setup (macOS)

```bash
git clone https://github.com/christopher-igweze/forge-engine.git
cd forge-engine
pip install -e ".[dev]"          # standalone mode (no agentfield)
pip install -e ".[platform,dev]" # platform mode (with agentfield)
```

### Run tests

```bash
pytest                            # unit + integration + golden
pytest -m unit                    # unit tests only
pytest -m "not live"              # skip live API tests
FORGE_LIVE_TESTS=1 pytest -m live # run live E2E tests
```
