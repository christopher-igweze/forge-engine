# FORGE v3 Architecture

## Pipeline

```
forge_scan("./my-app")
│
├── Step 1: Codebase Analysis (LLM — 1 call)
│   │
│   │  The only agent that MUST be LLM. Maps the project structure,
│   │  identifies data flows, auth boundaries, entry points, tech stack.
│   │  Everything downstream depends on this map.
│   │
│   └── Output: CodebaseMap
│
├── Step 2: Deterministic Scanning (zero LLM cost)
│   │
│   ├── 2a. Opengrep SAST (16 custom rules)
│   │   └── Taint analysis, pattern matching, secret detection
│   │
│   ├── 2b. 47 Built-in Checks (7 dimensions)
│   │   ├── Security (12 checks)
│   │   ├── Reliability (7 checks)
│   │   ├── Maintainability (5 checks)
│   │   ├── Test Quality (7 checks)
│   │   ├── Performance (5 checks)
│   │   ├── Documentation (6 checks)
│   │   └── Operations (5 checks)
│   │
│   └── Output: Deterministic findings + dimension scores + composite score
│
├── Step 3: LLM Security Auditor (3 LLM calls)
│   │
│   │  Evaluates against OWASP ASVS requirements (closed questions).
│   │  Finds business logic flaws Opengrep can't reason about.
│   │  Intent detection built into prompt.
│   │
│   ├── Pass 1: Auth flow (ASVS V2-V4)
│   ├── Pass 2: Data handling (ASVS V5-V6)
│   ├── Pass 3: Infrastructure (ASVS V8, V10, V14)
│   │
│   └── Output: LLM findings (advisory, don't affect deterministic score)
│
├── Step 4: Fix Strategist (1 LLM call)
│   │
│   │  Creates prioritized remediation plan from ALL findings.
│   │  Groups by dependency, estimates effort, orders execution.
│   │  The plan is the deliverable — user/coding agent follows it.
│   │
│   └── Output: Remediation plan with priorities + dependencies
│
├── Step 5: Post-Processing (zero LLM cost)
│   │
│   ├── 5a. Fingerprinting — stable IDs for every finding
│   ├── 5b. Severity calibration — OWASP boost, arch cap, confidence
│   ├── 5c. .forgeignore — user-defined suppressions applied
│   ├── 5d. Baseline comparison — new / recurring / fixed / regressed
│   ├── 5e. Quality gate — pass/fail against thresholds
│   ├── 5f. Feedback tracking — per-agent false positive rates
│   └── 5g. AIVSS scoring — agentic AI risk assessment (NEW)
│   │
│   └── Output: Delta report, quality gate result, readiness score, AIVSS score
│
└── Report
    ├── Deterministic score (stable, same code = same score)
    ├── LLM findings (advisory context)
    ├── Remediation plan (actionable)
    ├── Baseline delta (progress tracking)
    ├── Quality gate (pass/fail for CI/CD)
    ├── AIVSS score (agentic AI risk, NEW)
    └── Compliance mapping (OWASP ASVS, STRIDE, NIST SSDF)
```

## LLM Calls Per Scan

| Step | Agent | Calls | Model | Cost |
|------|-------|-------|-------|------|
| 1 | Codebase Analyst | 1 | Minimax M2.5 | ~$0.01 |
| 3 | Security Auditor | 3 | Haiku 4.5 | ~$0.15 |
| 4 | Fix Strategist | 1 | Haiku 4.5 | ~$0.05 |
| **Total** | | **5** | | **~$0.21** |

Down from 7-11 calls in v2. Quality Auditor, Architecture Reviewer, Triage Classifier, and Intent Analyzer removed — their work is now covered by deterministic checks + rubric-based security auditor + .forgeignore.

## Agents: Active vs Deprecated

### Active (5 LLM calls)

| Agent | Why It's Needed |
|-------|----------------|
| **Codebase Analyst** | Maps structure, data flows, auth boundaries. Nothing else can do this. |
| **Security Auditor** | Reasons about exploit chains, business logic flaws. Opengrep finds patterns, this finds logic. |
| **Fix Strategist** | Creates the remediation plan. This is the main deliverable for the user. |

### Deprecated (covered by deterministic layer)

| Agent | Replaced By |
|-------|-------------|
| Quality Auditor | 47 deterministic checks (reliability, maintainability, test quality, performance) |
| Architecture Reviewer | Deterministic checks (MNT-001 to MNT-005) + mostly produced false positives |
| Triage Classifier | Severity calibration + actionability classification (deterministic) |
| Intent Analyzer | .forgeignore + convention detection in security auditor prompt |

### Dead (remediation never worked reliably)

| Agent | Status |
|-------|--------|
| Tier 2 Coder | Dead — remediation burns money, mediocre output |
| Tier 3 Coder | Dead |
| Test Generator | Dead — only ran after coder |
| Code Reviewer | Dead |
| Integration Validator | Dead |
| Debt Tracker | Replaced by deterministic readiness score |

### Parked (not removed, not used)

| System | Status |
|--------|--------|
| Hive/Swarm Discovery | Over-engineered. Opengrep + deterministic checks cover the depth. Keep code, don't run. |
| AgentField integration | Platform deployment path. Works but standalone/MCP is the primary path. |

## Scoring Architecture

```
┌─────────────────────────────────────────────┐
│           PRODUCTION READINESS SCORE         │
│                                              │
│  Source: Deterministic checks ONLY           │
│  Same code = same score, every time          │
│                                              │
│  7 dimensions, weighted:                     │
│    Security (30%)                             │
│    Reliability (20%)                          │
│    Maintainability (15%)                      │
│    Test Quality (15%)                         │
│    Performance (10%)                          │
│    Documentation (5%)                         │
│    Operations (5%)                            │
│                                              │
│  Band: A (80+), B (60-79), C (40-59),       │
│        D (20-39), F (0-19)                   │
├─────────────────────────────────────────────┤
│           AIVSS SCORE (NEW)                  │
│                                              │
│  Source: Auto-detected from code analysis    │
│  Scores agentic AI risk (0-10 scale)         │
│  10 amplification factors (AARS)             │
│  OWASP AIVSS v0.5 standard                  │
├─────────────────────────────────────────────┤
│           LLM FINDINGS (advisory)            │
│                                              │
│  Source: Security Auditor (3 passes)         │
│  Does NOT affect readiness score             │
│  Provides context, exploit reasoning         │
│  Included in report as supplementary         │
├─────────────────────────────────────────────┤
│           QUALITY GATE (pass/fail)           │
│                                              │
│  Based on: deterministic findings only       │
│  Default: 0 new critical, 0 new high         │
│  Profiles: forge-way, strict, startup        │
│  For CI/CD integration                       │
├─────────────────────────────────────────────┤
│           COMPLIANCE MAPPING                 │
│                                              │
│  OWASP ASVS level estimation                 │
│  STRIDE threat coverage                      │
│  NIST SSDF practice coverage                 │
└─────────────────────────────────────────────┘
```

## What Needs Building

| Item | Status | Priority |
|------|--------|----------|
| AIVSS scoring module | Spec written, not built | HIGH — differentiator |
| Wire LLM validator into pipeline | Code exists, not connected | LOW — nice-to-have |
| Python 3.9 compat fixes | Syntax issues in evaluation/ | MEDIUM — blocks some tests |
| Remove deprecated agents from default pipeline | They still run | HIGH — saves cost per scan |
| Drop Quality Auditor + Arch Reviewer from pipeline | Still making LLM calls | HIGH — saves ~$0.30/scan |

## File Map

```
forge/
  phases.py              → Pipeline orchestration (all steps above)
  config.py              → All configuration fields
  schemas.py             → Data models (ForgeResult, AuditFinding, etc.)
  standalone.py          → CLI/MCP entry point
  cli.py                 → Typer CLI commands

  reasoners/
    discovery.py         → Classic discovery (Step 1 + Step 3)
    hive_discovery.py    → [PARKED] Swarm discovery
    triage.py            → [DEPRECATED] Triage classifier
    remediation.py       → [DEAD] Coder dispatch
    validation.py        → [DEAD] Integration validator

  prompts/
    codebase_analyst.py  → Step 1 prompt
    security_auditor.py  → Step 3 prompts (OWASP ASVS rubric)
    quality_auditor.py   → [DEPRECATED] Covered by deterministic checks
    architecture_reviewer.py → [DEPRECATED] Mostly false positives
    fix_strategist.py    → Step 4 prompt

  execution/
    opengrep_runner.py   → Step 2a: Opengrep SAST
    fingerprint.py       → Step 5a: Stable finding IDs
    baseline.py          → Step 5d: Cross-scan comparison
    forgeignore.py       → Step 5c: User suppression rules
    severity.py          → Step 5b: Severity calibration
    quality_gate.py      → Step 5e: Pass/fail gate
    feedback.py          → Step 5f: Per-agent FP tracking
    readiness_score.py   → Step 5g: Discovery-mode score
    delta.py             → Delta mode (changed files only)
    llm_validator.py     → [NOT WIRED] LLM validation of deterministic findings

  evaluation/
    __init__.py          → run_evaluation() orchestrator
    checks/              → 47 deterministic checks (7 dimensions)
    dimensions.py        → Weighted scoring + composite
    quality_gate.py      → Quality gate profiles
    compliance.py        → OWASP/STRIDE/NIST mapping
    aivss.py             → [NOT BUILT] OWASP AIVSS scoring

  rules/
    security/            → 12 Opengrep YAML rules
    quality/             → 2 Opengrep YAML rules
    performance/         → 2 Opengrep YAML rules
```
