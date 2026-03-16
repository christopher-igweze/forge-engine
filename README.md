# FORGE

**Framework for Orchestrated Remediation & Governance Engine**

A 12-agent AI system that scans codebases for security, quality, and architecture issues — then fixes them.

## Quick Start

```bash
# Install
pip install vibe2prod

# Register as MCP server in Claude Code
claude mcp add forge -e OPENROUTER_API_KEY=your-key -- python -m forge.mcp_server

# Scan a repo
# (use the forge_scan tool in Claude Code)
```

Get an OpenRouter API key at [openrouter.ai](https://openrouter.ai) (free signup).

### The /forge Skill

After scanning, use the `/forge` skill in Claude Code to autonomously fix findings. It reads the scan report, prioritizes issues, and applies fixes with micro-commits.

Full CLI documentation: [vibe2prod.net/cli](https://vibe2prod.net/cli)

## Architecture

```
Discovery (Agents 1-4)     Scan codebase, identify issues
    |
Triage (Agents 5-6)        Classify by complexity tier (0-3), plan fixes
    |
Remediation (Agents 7-10)  Apply fixes via three control loops
    |
Validation (Agents 11-12)  Verify fixes, generate readiness report
```

### Agents

| # | Agent | Role |
|---|-------|------|
| 1 | Codebase Analyst | Map architecture, files, dependencies |
| 2 | Security Auditor | 3-pass parallel security scan |
| 3 | Quality Auditor | 3-pass parallel quality scan |
| 4 | Architecture Reviewer | Structural coherence evaluation |
| 5 | Fix Strategist | Prioritize and order fixes |
| 6 | Triage Classifier | Assign complexity tiers (0-3) |
| 7 | Coder Tier 2 | Scoped fixes (1-3 files) |
| 8 | Coder Tier 3 | Architectural fixes (5-15 files) |
| 9 | Test Generator | Write tests for fixes |
| 10 | Code Reviewer | Review fix quality |
| 11 | Integration Validator | Verify merged codebase |
| 12 | Debt Tracker | Generate readiness report |

### Control Loops

- **Inner Loop**: Coder -> Review -> Retry (max 3 iterations)
- **Middle Loop**: Escalation when inner loop exhausted (RECLASSIFY / DEFER)
- **Outer Loop**: Re-plan with Fix Strategist (max 1 replan)

### Tier Routing

- **Tier 0**: Auto-skip (invalid / false-positive)
- **Tier 1**: Deterministic fix (no LLM needed)
- **Tier 2**: Scoped AI fix (1-3 files, Sonnet 4.6)
- **Tier 3**: Architectural AI fix (5-15 files, Sonnet 4.6)

## Requirements

- Python 3.12+
- OpenRouter API key (for LLM providers)
- [AgentField](https://github.com/anomalyco/agentfield) control plane (optional — only needed for platform mode)

## Usage

### Standalone Mode

Run FORGE locally without an AgentField server:

```python
from forge.standalone import run_standalone

result = await run_standalone(repo_path="./my-app", config={"mode": "discovery"})
```

### AgentField Mode

```bash
# Start as AgentField node
python -m forge

# Or via entry point
forge-engine
```

FORGE registers as an AgentField node (`forge-engine`) and exposes three reasoners:

- `remediate` — Full pipeline: scan -> triage -> fix -> validate
- `discover` — Scan-only mode (Agents 1-6, no fixes)
- `scan` — Alias for discover (free tier)

### Hive Discovery (Swarm Mode)

An alternative discovery architecture using a three-layer swarm approach:

```python
config = {"discovery_mode": "swarm"}  # default: "classic"
```

See `doc/hive-discovery-spec.md` for the full design.

## Configuration

Model routing is configurable per-agent via the `models` dict:

```python
config = {
    "models": {
        "default": "anthropic/claude-haiku-4.5",
        "coder_tier2": "anthropic/claude-sonnet-4.6",
        "coder_tier3": "anthropic/claude-sonnet-4.6",
    }
}
```

Resolution: `defaults` < `models.default` < `models.<role>`

## Resilience

FORGE normalizes LLM outputs before validation to handle model inconsistencies:

- **Category aliases**: LLM-returned categories are mapped to canonical categories (`quality`, `reliability`, `security`) via `_CATEGORY_ALIASES`
- **Priority floor**: Priorities < 1 are clamped to 1 before validation
- **Dependency coercion**: `depends_on_finding_id` returned as a list is coerced to a string
