# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

FORGE (Framework for Orchestrated Remediation & Governance Engine) is an AI-powered codebase auditing engine. It scans codebases for security, quality, and architecture issues using a combination of deterministic analysis (Opengrep) and LLM-based review, then produces a scored evaluation report.

The v3 architecture uses 3 active LLM agents and ~5 LLM calls per scan:

1. **Codebase Analyst** — builds a structured map of the codebase (modules, dependencies, data flows)
2. **Security Auditor** — parallel audit passes (auth flow, data handling, infrastructure)
3. **Fix Strategist** — prioritizes findings and produces a remediation plan

Post-discovery, deterministic systems handle evaluation:
- **Opengrep** — SAST scanner with custom FORGE rules + community rules
- **Evaluation Framework** — deterministic scoring across 5 dimensions
- **AIVSS** — AI-specific vulnerability scoring (like CVSS but for AI-era risks)
- **Quality Gate** — pass/fail gate based on configurable severity thresholds
- **Compliance Mapping** — ASVS, STRIDE, NIST SSDF compliance checks

## Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run all tests (unit + integration + golden)
pytest

# Run by marker
pytest -m unit                     # unit tests only
pytest -m "not live"               # skip live API tests
FORGE_LIVE_TESTS=1 pytest -m live  # run live E2E (requires OPENROUTER_API_KEY)

# CLI commands
vibe2prod scan ./my-app            # discovery + evaluation
vibe2prod fix ./my-app             # full pipeline
vibe2prod report ./my-app          # view last run's report
vibe2prod status ./my-app          # check running scan progress
vibe2prod config set key value     # set config value

# MCP server (for AI IDE integration)
# Registered via .mcp.json — provides forge_scan and forge_status tools
```

## Architecture

```
forge/
  __main__.py          → CLI entry point
  cli.py               → Typer CLI: vibe2prod scan/fix/report/status/config
  config.py            → ForgeConfig (Pydantic, extra="forbid"), model routing, provider mapping
  schemas.py           → Pydantic models: ForgeMode, Finding, RemediationPlan, ForgeResult, etc.
  standalone.py        → run_standalone() — primary SDK entry point for CLI/tests
  phases.py            → Phase orchestration: discovery pipeline
  mcp_server.py        → MCP server for AI IDE integration (forge_scan, forge_status)
  reasoners/
    discovery.py       → Codebase analyst + security/quality/architecture audit
    triage.py          → Fix strategist (priority + ordering)
  evaluation/
    dimensions.py      → 5-dimension scoring (security, quality, architecture, reliability, performance)
    checks/            → Deterministic check implementations per dimension
    quality_gate.py    → Pass/fail gate with configurable profiles
    compliance.py      → ASVS/STRIDE/NIST SSDF compliance mapping
    aivss.py           → AI Vulnerability Severity Score calculator
    aivss_detector.py  → Detects AI/ML-specific vulnerability patterns
    feedback.py        → Actionable fix suggestions per finding
    report.py          → Evaluation report generation
  execution/
    fingerprint.py     → Stable content-based finding IDs (SHA-256, line-bucket tolerant)
    baseline.py        → Cross-scan finding persistence (new/recurring/fixed/regressed)
    forgeignore.py     → .forgeignore parser — user-controlled finding suppression
    severity.py        → Post-discovery severity calibration (arch cap, OWASP boost)
    telemetry.py       → ForgeTelemetry: async-safe cost tracking via contextvars
    run_telemetry.py   → RunTelemetry: real-time observable state + circuit breakers
    context_builder.py → File inventory and codebase context preparation
    report.py          → Discovery report generation (HTML/JSON)
    actionability.py   → Finding actionability classification
    intent_analyzer.py → Deterministic intent detection (suppressions, test files)
    opengrep_runner.py → Opengrep SAST integration
  rules/               → FORGE custom Opengrep YAML rules
  vendor/
    agent_ai/          → AgentAI LLM client (OpenRouter direct)
  graph/               → Code graph analysis (tree-sitter AST, community detection)
  compliance/          → NIST SSDF compliance mapping
  conventions/         → Convention detection and enforcement
  patterns/            → Vulnerability pattern library (YAML-based)
  prompts/             → Agent prompt templates
tests/
  unit/                → Unit tests
  integration/         → Integration tests (mocked LLM), live E2E tests
  golden/              → Golden snapshot tests against known-flawed codebases
  fixtures/            → Shared test fixtures
```

## Key Patterns

**Standalone mode** is the primary entry point for CLI and tests. `run_standalone(repo_path, config)` in `forge/standalone.py` runs the full pipeline. All live E2E tests use this path.

**Pipeline flow:** Opengrep SAST scan -> Codebase Analyst -> Security Auditor (parallel passes) -> Fix Strategist -> Deterministic Evaluation + AIVSS scoring -> Quality Gate -> Report.

**Model routing:** `ForgeConfig.model_for_role(role)` resolves via cascade: `FORGE_DEFAULT_MODELS` < `models.default` < `models.<role>`. Analysis agents (codebase analyst, quality auditor) use MiniMax M2.5. Reasoning agents (security auditor, architecture reviewer, fix strategist) use Haiku 4.5.

**Provider routing:** All discovery agents use `openrouter_direct` (text-in/JSON-out, no tools).

**Telemetry:** `ForgeTelemetry` uses `contextvars.ContextVar` for async-safe singleton. `AgentAI.run()` auto-logs every LLM call. Cost summaries written to `<repo>/.artifacts/telemetry/`.

**Config is strict:** `ForgeConfig` uses `extra="forbid"`. Unknown fields crash validation. When adding config fields, add them to `ForgeConfig` in `forge/config.py`.

**Evaluation scoring:** Deterministic scoring across 5 dimensions (security, quality, architecture, reliability, performance). Each dimension runs checks against findings. Quality gate profiles ("forge-way", "strict", "startup") control pass/fail thresholds.

**Finding lifecycle:** Content-based fingerprints (SHA-256) enable cross-scan tracking. Baseline comparison produces delta (new/recurring/fixed/regressed/suppressed). `.forgeignore` YAML allows user-controlled suppression. Severity calibration adjusts confidence-weighted scores post-discovery.

## Configuration

Key `ForgeConfig` fields (all have defaults):

| Field | Default | Description |
|-------|---------|-------------|
| `mode` | `full` | Pipeline mode: full, discovery |
| `models` | `null` | Per-role model overrides dict |
| `agent_timeout_seconds` | `900` | Per-agent timeout (15 min) |
| `dry_run` | `false` | Scan only, no fixes applied |
| `enable_parallel_audit` | `true` | Run audit passes concurrently |
| `opengrep_enabled` | `true` | Use Opengrep for deterministic scanning |
| `quality_gate_profile` | `forge-way` | Quality gate profile |
| `evaluation_weights` | `null` | Dimension weight overrides |
| `delta_mode` | `false` | Only scan changed files |
| `webhook_url` | `""` | POST endpoint for scan progress events |
| `max_cost_usd` | `0.0` | Cost budget (0 = no limit) |

Environment variables:
- `OPENROUTER_API_KEY` — Required. All LLM calls route through OpenRouter.
- `FORGE_LIVE_TESTS=1` — Enable live E2E tests that call real APIs.

## Finding Lifecycle

FORGE tracks findings across scans using content-based fingerprints (SHA-256 hash of category + file + line bucket + normalized title + CWE). This enables:

**Baseline comparison:** Each scan compares against the previous scan's findings. The report includes a `findings_delta` with counts for new, recurring, fixed, regressed, and suppressed findings.

**`.forgeignore`:** Users can suppress finding patterns permanently by creating a `.forgeignore` YAML file in the repo root. Rules support regex on title, category filter, file path glob, severity cap, and expiry dates. Each rule requires a `reason` field.

**Severity calibration:** After discovery, findings pass through `calibrate_findings()` which:
- Caps architecture findings at MEDIUM severity
- Boosts OWASP Top 10 CWEs to minimum HIGH
- Downgrades findings with confidence < 0.6

**File exclusions:** `context_builder.py` excludes `migrations/` and `alembic/` from `SKIP_DIRS`. Files matching `LOW_RELEVANCE_PATTERNS` (`.sql`, `tasks/`, `docs/`) get -5 relevance in audit pass scoring.

**Integration point:** All finding lifecycle processing happens in `forge/phases.py` after intent analysis. The baseline is stored at `<repo>/.artifacts/baseline.json`.

## Related Repos

- **vibe2prod** (`christopher-igweze/vibe2prod`) — FastAPI + Next.js platform. Calls FORGE via HTTP through `forge_bridge.py`.
- **security-probe** — Live security scanning microservice, called from vibe2prod for real-time probe checks.
