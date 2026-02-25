# FORGE Engine -- Deployment Operations Guide

Version: 0.2.0

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required by `pyproject.toml` (`requires-python = ">=3.12"`) |
| AgentField | >= 0.1.9 | Core dependency; provides the agent runtime and control plane |
| opencode | v1.2+ | Required for coder agents (Tier 2/3), test generator, integration validator |
| OpenRouter API key | -- | All LLM calls route through OpenRouter |
| git | any recent | Worktree support required for parallel fix isolation |

Optional dependencies:

- `weasyprint >= 60.0` -- PDF report generation (`pip install forge-engine[pdf]`)
- `claude-agent-sdk >= 0.1.20` -- Claude provider support (`pip install forge-engine[claude]`)

Install the package:

```bash
pip install -e .           # core only
pip install -e ".[pdf]"    # with PDF report generation
pip install -e ".[dev]"    # with test tooling
```

---

## 2. Known Issues: opencode v1.2 on macOS

The `opencode run` command hangs when spawned as a subprocess on macOS. This affects all coder agents (Tier 2/3), the test generator, and the integration validator -- any agent using the `opencode` provider.

**Root causes:**

- `opencode run` hangs due to non-TTY/piped stdout handling (upstream bug: `anomalyco/opencode#11891`)
- The permission "ask" default blocks headless operation even when stdin is `/dev/null` (`anomalyco/opencode#14473`)
- This affects even built-in models -- it is a fundamental non-TTY issue, not a model problem

**Workaround:**

Use a Python wrapper script at `~/.opencode/bin/opencode` that runs opencode in AGENT mode. The wrapper includes a structured output fallback: after the agent loop completes, it makes a cleanup API call if the expected JSON output file was not written.

**Linux/Docker:**

The real v1.2 binary works without issues on Linux (different code path for TTY detection). No wrapper needed in containerized deployments.

---

## 3. Environment Variables

### Required

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter. Used by both `openrouter_direct` and `opencode` providers. All LLM calls fail without this. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `FORGE_NODE_ID` | `forge-engine` | AgentField node identifier. Change when running multiple FORGE instances. |
| `WORKSPACES_DIR` | `/workspaces` | Directory for cloning repos when `repo_url` is provided instead of `repo_path`. |
| `AGENTFIELD_SERVER` | `http://localhost:8080` | URL of the AgentField control plane server. |
| `AGENTFIELD_API_KEY` | (none) | API key for authenticating with the AgentField server. |
| `FORGE_PORT` | `8004` | Port the FORGE node listens on. |
| `FORGE_HOST` | `0.0.0.0` | Host address the FORGE node binds to. |

---

## 4. Configuration

### ForgeConfig Fields

The `config` dict passed to any reasoner is validated against `ForgeConfig`. All fields have defaults.

| Field | Type | Default | Description |
|---|---|---|---|
| `runtime` | string | `"open_code"` | Runtime backend identifier. |
| `models` | dict | `null` | Per-role model overrides (see Model Routing below). |
| `mode` | enum | `"full"` | Pipeline mode: `full`, `discovery`, `remediation`, `validation`. |
| `max_inner_retries` | int | `3` | Inner loop: max coder retries on REQUEST_CHANGES. |
| `max_middle_escalations` | int | `2` | Middle loop: max RECLASSIFY/DEFER escalation attempts. |
| `max_outer_replans` | int | `1` | Outer loop: how many times to re-run the Fix Strategist. |
| `agent_timeout_seconds` | int | `900` | Per-agent timeout (15 minutes). |
| `enable_tier0_autofix` | bool | `true` | Enable Tier 0 auto-skip for invalid/noise findings. |
| `enable_tier1_rules` | bool | `true` | Enable Tier 1 deterministic template fixes. |
| `enable_parallel_audit` | bool | `true` | Run discovery audit agents (2-4) concurrently. |
| `enable_learning` | bool | `true` | Log training data pairs for the fine-tuning flywheel. |
| `repo_url` | string | `""` | Git URL to clone. Mutually exclusive with `repo_path`. |
| `repo_path` | string | `""` | Local path to the repository. |
| `enable_github_pr` | bool | `true` | Create a GitHub PR with the fixes. |
| `github_pr_base` | string | `"main"` | Base branch for the PR. |
| `dry_run` | bool | `false` | Scan only, produce findings but do not apply fixes. |
| `skip_tiers` | list[int] | `[]` | Skip specific tiers (e.g., `[0]` to process noise findings). |
| `focus_categories` | list[str] | `[]` | Only fix findings in these categories (e.g., `["security"]`). |

### Model Routing

Model resolution follows a three-level cascade:

```
FORGE_DEFAULT_MODELS < models.default < models.<role>
```

Pass overrides via the `models` dict in the config payload:

```json
{
  "config": {
    "models": {
      "default": "anthropic/claude-haiku-4.5",
      "coder_tier3": "anthropic/claude-sonnet-4.6"
    }
  }
}
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
| `test_generator` | `anthropic/claude-haiku-4.5` | opencode | Mid-tier, needs tools |
| `code_reviewer` | `anthropic/claude-haiku-4.5` | openrouter_direct | Mid-tier reasoning |
| `integration_validator` | `anthropic/claude-haiku-4.5` | opencode | Mid-tier, needs tools |
| `coder_tier2` | `anthropic/claude-sonnet-4.6` | opencode | Frontier coding model |
| `coder_tier3` | `anthropic/claude-sonnet-4.6` | opencode | Frontier coding model |

**Model ID format:** Use `provider/model` (e.g., `anthropic/claude-sonnet-4.6`). Do NOT prefix with `openrouter/` -- the client strips this prefix automatically, but omitting it avoids confusion.

### Provider Routing

Two providers serve different agent types:

- **openrouter_direct** -- stdlib-only HTTPS client (`urllib.request`). Text-in, JSON-out. No subprocess, no CLI dependency. Used for all planning/analysis agents.
- **opencode** -- Invokes the `opencode run` CLI as a subprocess. Provides file-editing tools (Read, Write, Edit, Bash, Glob, Grep). Used for coder agents, test generator, and integration validator.

---

## 5. Running FORGE

### Start the Node

```bash
# Via module
python -m forge

# Via entry point (after pip install)
forge-engine

# With custom port and server
FORGE_PORT=9000 AGENTFIELD_SERVER=http://control-plane:8080 python -m forge
```

The node registers itself with the AgentField control plane at startup.

### Available Reasoners

#### `remediate` -- Full Pipeline

Runs all 12 agents: discover, triage, fix, validate.

```json
{
  "repo_url": "https://github.com/org/repo.git",
  "config": {
    "mode": "full",
    "models": {
      "coder_tier3": "anthropic/claude-sonnet-4.6"
    }
  }
}
```

Or with a local path:

```json
{
  "repo_path": "/path/to/local/repo",
  "config": {
    "enable_parallel_audit": true,
    "max_inner_retries": 2
  }
}
```

#### `discover` / `scan` -- Discovery Only

Runs Agents 1-5 (codebase analysis + audits + triage). No fixes applied. `scan` is an alias for `discover`.

```json
{
  "repo_path": "/path/to/repo",
  "config": {}
}
```

#### `fix_single` -- Single Finding Fix

Fixes one finding at a time. Useful for testing or iterative remediation.

```json
{
  "repo_path": "/path/to/repo",
  "finding": {
    "id": "SEC-001",
    "title": "SQL injection in user query",
    "description": "Unsanitized user input in db.query()",
    "category": "security",
    "severity": "critical",
    "locations": [{"file_path": "src/db.py"}],
    "suggested_fix": "Use parameterized queries"
  },
  "codebase_map": {},
  "config": {}
}
```

### Calling Reasoners via AgentField

```bash
# Using the AgentField CLI or SDK
agentfield call forge-engine.remediate \
  --arg repo_path=/path/to/repo \
  --arg config='{"mode": "full"}'
```

---

## 6. Monitoring

### Telemetry Output

When `enable_learning` is `true` (default), FORGE writes telemetry to `<repo>/.artifacts/telemetry/` at the end of each run:

| File | Format | Contents |
|---|---|---|
| `cost_summary.json` | JSON | Aggregate cost by agent and model, total tokens, invocation counts, elapsed time |
| `invocations.jsonl` | JSONL | One line per agent invocation: model, tokens, cost, latency, success/error |
| `training_data.jsonl` | JSONL | One line per finding-fix pair: finding metadata, tier, outcome, files changed, retry count |

Example `cost_summary.json`:

```json
{
  "run_id": "forge-abc123",
  "total_cost_usd": 0.0847,
  "total_tokens": 52340,
  "total_invocations": 14,
  "successful_invocations": 13,
  "failed_invocations": 1,
  "cost_by_agent": {
    "codebase_analyst": 0.0012,
    "coder_tier2": 0.0534
  },
  "cost_by_model": {
    "minimax/minimax-m2.5": 0.0045,
    "anthropic/claude-sonnet-4.6": 0.0534
  },
  "training_pairs_logged": 5
}
```

### Checkpoint Files

During execution, FORGE saves checkpoints at phase boundaries in `<repo>/.forge-checkpoints/`:

| File | Phase |
|---|---|
| `discovery_complete.json` | After Agents 1-4 finish |
| `triage_complete.json` | After Agents 5-6 finish |
| `fix_progress.json` | After remediation (Agents 7-10) |
| `validation_complete.json` | After Agents 11-12 finish |

Checkpoints are automatically cleared on successful completion. They persist on crash for resume.

### Reports

On runs that include validation, reports are written to `<repo>/.artifacts/report/`:

- JSON report (always)
- HTML report (always)
- PDF report (if `weasyprint` is installed)

### Worktrees

During remediation, each Tier 2/3 fix gets its own git worktree under `<repo>/.forge-worktrees/`. Branch naming follows `forge/fix-<finding-id>`. Worktrees are cleaned up after each run (success or failure).

---

## 7. Troubleshooting

### Common Issues

**"OPENROUTER_API_KEY is not set"**

All LLM calls require this key. Set it in the environment before starting the node:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

**opencode hangs on macOS**

See section 2. Use the Python wrapper at `~/.opencode/bin/opencode` or run FORGE in a Linux container.

**"Either repo_url or repo_path must be provided"**

Every reasoner call needs one of these. If using `repo_url`, the repo is cloned to `$WORKSPACES_DIR/<repo-name>`.

**Agent timeout (15 minutes default)**

Increase `agent_timeout_seconds` in the config for large repos:

```json
{"config": {"agent_timeout_seconds": 1800}}
```

**Structured output parse failures**

The opencode provider logs stdout previews to the log file when JSON parsing fails. Check the agent's log file for `schema parse failed` events. Common cause: the coder model is too weak to produce valid JSON (minimax-m2.5 struggles with structured output -- use claude-haiku-4.5 or stronger for roles requiring JSON output).

### Crash Recovery (Checkpoint Resume)

FORGE automatically detects and resumes from the latest checkpoint on the next run against the same repo:

1. Checkpoints are saved in `<repo>/.forge-checkpoints/`
2. On the next `remediate` call for the same repo, FORGE finds the latest checkpoint and skips completed phases
3. Resume order: validation > remediation > triage > discovery (most recent phase wins)

To force a fresh run, delete the checkpoint directory:

```bash
rm -rf /path/to/repo/.forge-checkpoints/
```

### Worktree Cleanup

FORGE cleans up worktrees automatically, but a hard crash may leave stale worktrees. To clean up manually:

```bash
# From within the repo directory:

# Remove all FORGE worktrees
rm -rf .forge-worktrees/

# Prune stale git worktree references
git worktree prune

# Delete leftover FORGE branches
git branch --list 'forge/fix-*' | xargs -r git branch -D
```

If `git worktree remove` complains about a locked worktree, remove the lock file:

```bash
# Find and remove stale lock files
find .git/worktrees -name "locked" -delete
git worktree prune
```

### Log Levels

FORGE uses Python's standard `logging` module. Set the log level via the root logger:

```bash
LOGLEVEL=DEBUG python -m forge
```

Or configure programmatically before starting the node.

---

## 8. Directory Layout (Runtime)

After a full run, the repo will contain:

```
<repo>/
  .artifacts/
    telemetry/
      cost_summary.json
      invocations.jsonl
      training_data.jsonl
    report/
      forge-<run-id>.json
      forge-<run-id>.html
      forge-<run-id>.pdf        # if weasyprint installed
  .forge-checkpoints/           # cleared on success
    discovery_complete.json
    triage_complete.json
    fix_progress.json
    validation_complete.json
  .forge-worktrees/             # cleared after run
    fix-<finding-id>/
```

Add to `.gitignore`:

```
.forge-checkpoints/
.forge-worktrees/
.artifacts/telemetry/
```
