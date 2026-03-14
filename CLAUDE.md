# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

FORGE (Framework for Orchestrated Remediation & Governance Engine) is a 12-agent AI system that takes vibe-coded MVPs and hardens them for production. It operates in 4 phases:

1. **Discovery** (Agents 1-4) — Codebase analyst, security auditor, quality auditor, architecture reviewer
2. **Triage** (Agents 5-6) — Triage classifier (tier 0-3), fix strategist (priority + ordering)
3. **Remediation** (Agents 7-10) — Tier 2/3 coders, test generator, code reviewer
4. **Validation** (Agents 11-12) — Integration validator, debt tracker (readiness report)

Three control loops govern remediation: inner (coder retry, max 3), middle (escalation: reclassify/defer), outer (replan via fix strategist, max 1).

## Commands

```bash
# Install for development
pip install -e ".[dev]"            # standalone mode
pip install -e ".[platform,dev]"   # with AgentField support

# Run all tests (unit + integration + golden)
pytest

# Run by marker
pytest -m unit                     # unit tests only
pytest -m "not live"               # skip live API tests
FORGE_LIVE_TESTS=1 pytest -m live  # run live E2E (requires OPENROUTER_API_KEY)

# CLI commands
vibe2prod scan ./my-app            # discovery only (agents 1-6)
vibe2prod fix ./my-app             # full pipeline (all 12 agents)
vibe2prod report ./my-app          # view last run's report

# Start as AgentField node (platform mode)
python -m forge
```

## Architecture

```
forge/
  __main__.py          → AgentField node entry point
  app.py               → AgentField app, registers reasoners (remediate, discover, scan)
  app_helpers.py       → Shared pipeline helpers
  standalone.py        → run_standalone() — primary SDK entry point for CLI/tests
  cli.py               → Typer CLI: vibe2prod scan/fix/report
  config.py            → ForgeConfig (Pydantic, extra="forbid"), model routing, provider mapping
  schemas.py           → Pydantic models: ForgeMode, Finding, RemediationPlan, ForgeResult, etc.
  phases.py            → Phase orchestration: discovery, triage, remediation, validation
  reasoners/
    discovery.py       → Classic discovery (agents 1-4, parallel audit)
    hive_discovery.py  → Swarm-based discovery (Layer 0 graph → Layer 1 workers → Layer 2 synthesis)
    triage.py          → Triage classifier + fix strategist
    remediation.py     → Coder dispatch, inner/middle/outer loops, worktree isolation
    validation.py      → Integration validator + debt tracker
  swarm/
    worker.py          → Hive swarm workers (per-segment analysis)
    synthesizer.py     → Hive synthesis (merge worker outputs)
    orchestrator.py    → Hive orchestration (segment → dispatch → synthesize)
  execution/
    telemetry.py       → ForgeTelemetry: async-safe cost tracking via contextvars
  vendor/
    agent_ai/          → AgentAI LLM client (OpenRouter direct + opencode subprocess)
  graph/               → Code graph analysis (tree-sitter AST, community detection)
  learning/            → Training data collection for fine-tuning flywheel
  compliance/          → NIST SSDF compliance mapping
  conventions/         → Convention detection and enforcement
  patterns/            → Vulnerability pattern library (YAML-based)
  prompts/             → Agent prompt templates
tests/
  unit/                → ~470+ unit tests
  integration/         → ~40+ integration tests (mocked LLM), live E2E tests
  golden/              → ~18 golden snapshot tests
  fixtures/            → Shared test fixtures
doc/                   → Design specs (hive discovery, hybrid remediation, etc.)
```

## Key Patterns

**Standalone mode** is the primary entry point for CLI and tests. `run_standalone(repo_path, config)` in `forge/standalone.py` runs the full pipeline without AgentField. All live E2E tests use this path.

**Agent pipeline:** Each phase returns structured Pydantic models. Discovery produces findings, triage classifies them into tiers (0-3) and creates a remediation plan, remediation applies fixes in isolated git worktrees, validation scores the result.

**Model routing:** `ForgeConfig.model_for_role(role)` resolves via cascade: `FORGE_DEFAULT_MODELS` < `models.default` < `models.<role>`. Cheap agents (codebase analyst, quality auditor) use MiniMax M2.5. Mid-tier agents (security, triage, review) use Haiku 4.5. Coders use Sonnet 4.6. Fallback escalates to Kimi K2.5.

**Provider routing:** `ROLE_TO_PROVIDER` maps each role to `openrouter_direct` (text-in/JSON-out), `opencode` (subprocess with file tools), or `openrouter_tools` (native function calling). Analysis agents use direct, coders use opencode, fallback uses tools.

**Telemetry:** `ForgeTelemetry` uses `contextvars.ContextVar` for async-safe singleton. `AgentAI.run()` auto-logs every LLM call. Cost summaries written to `<repo>/.artifacts/telemetry/`.

**Config is strict:** `ForgeConfig` uses `extra="forbid"`. Unknown fields crash validation. When adding config fields, add them to `ForgeConfig` in `forge/config.py`.

**Tier routing:** Tier 0 = auto-skip (noise). Tier 1 = deterministic fix (no LLM). Tier 2 = scoped AI fix (1-3 files, Sonnet). Tier 3 = architectural fix (5-15 files, Sonnet).

**Worktree isolation:** Each Tier 2/3 fix runs in its own git worktree under `<repo>/.forge-worktrees/`. Merged back on success, cleaned up after run.

**Convergence loop:** After remediation, FORGE can re-scan and re-fix until `convergence_target_score` (default 95) is reached or `max_convergence_iterations` (default 3) is exhausted.

## Configuration

Key `ForgeConfig` fields (all have defaults):

| Field | Default | Description |
|-------|---------|-------------|
| `mode` | `full` | Pipeline mode: full, discovery, remediation, validation |
| `models` | `null` | Per-role model overrides dict |
| `discovery_mode` | `classic` | Discovery architecture: classic or swarm (hive) |
| `max_inner_retries` | `3` | Coder retry attempts |
| `max_middle_escalations` | `2` | Escalation attempts before defer |
| `max_outer_replans` | `1` | Fix strategist replans |
| `agent_timeout_seconds` | `900` | Per-agent timeout (15 min) |
| `dry_run` | `false` | Scan only, no fixes applied |
| `convergence_enabled` | `true` | Re-scan after fixes for score improvement |
| `convergence_target_score` | `95` | Stop when score reaches this |
| `webhook_url` | `""` | POST endpoint for scan progress events |

Environment variables:
- `OPENROUTER_API_KEY` — Required. All LLM calls route through OpenRouter.
- `FORGE_LIVE_TESTS=1` — Enable live E2E tests that call real APIs.

## Related Repos

- **vibe2prod** (`christopher-igweze/vibe2prod`) — FastAPI + Next.js platform. Calls FORGE via HTTP through `forge_bridge.py`.
- **security-probe** — Live security scanning microservice, called from vibe2prod for real-time probe checks.
