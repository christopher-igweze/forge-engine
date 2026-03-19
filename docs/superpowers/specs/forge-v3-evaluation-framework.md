# FORGE v3 Evaluation Framework

**Date:** 2026-03-18
**Status:** Spec complete, ready for implementation
**Scope:** forge-engine evaluation, scoring, and quality gate system
**Research basis:** `vibe2prod/tasks/research-deterministic-llm-scanning.md`, `vibe2prod/tasks/research-production-grade-standards.md`, `vibe2prod/tasks/forge-v2-remaining-plan.md`

---

## Problem

FORGE v2 produces findings and a single "readiness score" computed as `100 - deductions`. This has three problems:

1. **Score instability** — LLM-produced findings vary between runs. The same codebase can score 62 one run and 71 the next because the LLM flagged different findings.
2. **No standards alignment** — The score is an internal number with no mapping to industry frameworks (OWASP ASVS, ISO 25010, NIST SSDF). Users can't answer "are we ASVS Level 1 compliant?"
3. **No quality gate** — There's no binary pass/fail. Users have to interpret a number. SonarQube proved that binary gates ("you pass or you don't") drive behavior better than scores.
4. **Single dimension** — One number conflates security, quality, testing, performance, and architecture. A codebase with perfect security but no tests scores the same as one with great tests but hardcoded secrets.

## Decision

**Two-layer evaluation: deterministic scoring + LLM advisory.** The score comes from deterministic checks only. LLM findings inform the developer but don't move the number. A binary quality gate sits on top.

Research basis: SAST-Genius achieves 89.5% precision with hybrid deterministic+LLM. CodeRabbit uses deterministic pre-processing with constrained LLM reasoning. SonarQube's binary quality gate eliminates score gaming. Semgrep's memory system achieves 96% agreement with user triage.

---

## Architecture

```
Codebase
    |
    v
LAYER 1: Deterministic Evaluation (score source)
    |-- AST pattern matching (tree-sitter, already in Layer 0)
    |-- Dependency CVE scan (lockfile parsing)
    |-- Configuration checks (secrets, debug mode, env validation)
    |-- Test presence and structure detection
    |-- Convention detection (existing forge/conventions/)
    |-- File structure analysis (health checks, CI/CD, docs)
    |
    |  => Per-dimension scores (0-100)
    |  => Quality gate (pass/fail)
    |
    v
LAYER 2: LLM Advisory (context source, does NOT affect score)
    |-- Security findings (existing agents 2-4 / swarm workers)
    |-- Architecture assessment
    |-- Business logic analysis
    |-- Remediation recommendations
    |
    |  => Advisory findings with actionability tiers
    |  => "Why This Matters" contextual framing
    |
    v
Combined Report
    |-- Deterministic score per dimension
    |-- Quality gate result (pass/fail + reasons)
    |-- LLM advisory findings (grouped by actionability)
    |-- Standards compliance mapping (ASVS level, NIST SSDF coverage)
    |-- Delta from previous scan (via baseline)
```

---

## Layer 1: Deterministic Evaluation

### 7 Dimensions

Each dimension is scored 0-100 independently. The composite score is a weighted average. Weights are configurable via `ForgeConfig.evaluation_weights`.

| Dimension | Default Weight | What It Measures | Source Standards |
|-----------|---------------|-----------------|-----------------|
| **Security** | 30% | Hardcoded secrets, injection patterns, auth gaps, crypto, config | OWASP ASVS, STRIDE, NIST SSDF PW.5/PW.7 |
| **Reliability** | 20% | Error handling, health checks, graceful shutdown, circuit breakers | ISO 25010 Reliability, Google PRR |
| **Maintainability** | 15% | Cyclomatic complexity, nesting depth, god classes, duplication | ISO 25010 Maintainability, SonarQube |
| **Test Quality** | 15% | Test presence, coverage proxy, test types, naming, structure | SMURF, Test Pyramid, SonarQube gates |
| **Performance** | 10% | N+1 patterns, unbounded queries, missing pagination, resource limits | ISO 25010 Performance Efficiency |
| **Documentation** | 5% | README, API docs, inline comments on public API, ADRs | DORA (2x reliability correlation) |
| **Operations** | 5% | CI/CD config, Dockerfile, health endpoints, structured logging, env validation | Google PRR, Cloudflare, Cortex |

### Deterministic Checks Per Dimension

#### Security (30%)

All checks are regex/AST-based. Zero LLM involvement.

| Check ID | Check | Detection Method | Severity | Points |
|----------|-------|-----------------|----------|--------|
| SEC-001 | Hardcoded secrets/API keys | Regex: high-entropy strings, known key prefixes (`sk-`, `AKIA`, `ghp_`) | Critical | -20 |
| SEC-002 | SQL string concatenation | AST: string concat/f-string in DB query calls | Critical | -15 |
| SEC-003 | Command injection patterns | AST: `os.system()`, `subprocess.call(shell=True)` with variables | Critical | -15 |
| SEC-004 | Missing auth middleware on routes | AST: route handlers without auth decorator/middleware | High | -10 |
| SEC-005 | Insecure crypto (MD5/SHA1 for passwords) | Regex + AST: `hashlib.md5`, `hashlib.sha1` in auth context | High | -10 |
| SEC-006 | Debug mode in production config | Regex: `DEBUG=True`, `debug: true` in non-test config | High | -8 |
| SEC-007 | CORS wildcard origin | Regex: `allow_origins=["*"]`, `Access-Control-Allow-Origin: *` | High | -8 |
| SEC-008 | Missing HTTPS enforcement | Config: no TLS/HTTPS in deployment manifests | Medium | -5 |
| SEC-009 | Verbose error exposure | AST: raw exception in HTTP response body | Medium | -5 |
| SEC-010 | PII in log statements | Regex: `password`, `token`, `secret`, `ssn` adjacent to `log`/`print` | Medium | -5 |
| SEC-011 | Missing input validation on endpoints | AST: route params used without Pydantic/schema validation | Medium | -3 |
| SEC-012 | Insecure default config values | Regex: `password = "password"`, `secret = "changeme"` | Medium | -3 |

Score: `max(0, 100 + sum(deductions))`

#### Reliability (20%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| REL-001 | No error handling (bare except or no try/except at API boundary) | AST | -15 |
| REL-002 | No health check endpoint | Route scan: no `/health`, `/healthz`, `/ready` | -10 |
| REL-003 | No graceful shutdown (SIGTERM handler) | AST: no signal handler registration | -8 |
| REL-004 | Silent exception swallowing (`except: pass`) | AST | -8 |
| REL-005 | No timeout on HTTP client calls | AST: `requests.get()`, `httpx.get()` without `timeout=` | -5 |
| REL-006 | No retry logic on external calls | AST: no retry/backoff decorator or loop | -3 |
| REL-007 | Missing connection pool configuration | Config: raw DB connection without pool settings | -3 |

#### Maintainability (15%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| MNT-001 | God classes (>500 lines) | Line count per class | -10 per class (max -30) |
| MNT-002 | High cyclomatic complexity (>20) | AST: branch counting per function | -5 per function (max -20) |
| MNT-003 | Deep nesting (>4 levels) | AST: indent/scope depth | -3 per instance (max -15) |
| MNT-004 | Significant code duplication (>20 lines identical) | Hash-based block comparison | -3 per pair (max -15) |
| MNT-005 | Circular imports | Import graph cycle detection | -5 per cycle (max -15) |

#### Test Quality (15%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| TST-001 | No test files present | File glob: `test_*`, `*_test.*`, `*.spec.*`, `__tests__/` | -40 |
| TST-002 | Only one test type (unit only, no integration) | Directory heuristic | -10 |
| TST-003 | Empty test functions (no assertions) | AST: test function without `assert`/`expect`/`should` | -5 per (max -15) |
| TST-004 | No tests for critical paths (auth, payment, data mutation) | File-test pairing heuristic | -10 |
| TST-005 | Test-to-source ratio below 0.3 | File count ratio | -5 |
| TST-006 | No test configuration (pytest.ini, jest.config, etc.) | File presence | -3 |
| TST-007 | Coverage config exists but threshold below 60% | Config parsing | -5 |

Coverage proxy (no runtime execution): `test_file_count / source_file_count` as a rough signal. Not a replacement for actual coverage, but deterministic.

#### Performance (10%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| PRF-001 | N+1 query pattern (loop containing DB call) | AST: DB call inside for/while loop | -10 per (max -20) |
| PRF-002 | Unbounded query (SELECT without LIMIT) | Regex/AST in ORM calls | -5 per (max -15) |
| PRF-003 | Missing pagination on list endpoints | AST: list endpoint without offset/limit params | -5 per (max -10) |
| PRF-004 | Synchronous I/O in async context | AST: blocking call in `async def` | -5 per (max -15) |
| PRF-005 | No caching pattern present | File scan: no cache import/decorator | -3 |

#### Documentation (5%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| DOC-001 | No README.md | File presence | -30 |
| DOC-002 | README exists but < 10 lines | Line count | -15 |
| DOC-003 | No API documentation (OpenAPI/Swagger) | File/route scan | -10 |
| DOC-004 | No inline docs on public functions (>50% undocumented) | AST: public functions without docstrings | -10 |
| DOC-005 | No ADR directory | File presence: `docs/adr/`, `docs/decisions/` | -5 |
| DOC-006 | No CHANGELOG | File presence | -3 |

#### Operations (5%)

| Check ID | Check | Detection Method | Points |
|----------|-------|-----------------|--------|
| OPS-001 | No CI/CD configuration | File presence: `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile` | -25 |
| OPS-002 | No Dockerfile / container config | File presence | -10 |
| OPS-003 | No structured logging | AST: raw `print()` for logging instead of `logging`/`structlog` | -10 |
| OPS-004 | No environment variable validation | AST: `os.getenv()` without default or validation | -5 per (max -15) |
| OPS-005 | No .env.example / env documentation | File presence | -5 |
| OPS-006 | No linter configuration | File presence: `.eslintrc`, `ruff.toml`, `pyproject.toml[tool.ruff]` | -5 |

---

### Composite Score

```python
composite = sum(dimension_score * weight for dimension, weight in dimensions.items())
```

Score is deterministic: same code = same score, every time.

### Score Bands

| Band | Range | Label | Description |
|------|-------|-------|-------------|
| A | 80-100 | Production Ready | Meets elite team standards (Google PRR, Stripe) |
| B | 60-79 | Near Ready | Minor gaps. Deployable with monitoring. |
| C | 40-59 | Needs Work | Significant gaps. Fix critical issues before deploy. |
| D | 20-39 | Major Gaps | Fundamental issues across multiple dimensions. |
| F | 0-19 | Not Ready | Critical vulnerabilities or missing fundamentals. |

---

## Quality Gate

Binary pass/fail. Deterministic. No LLM involvement.

### Default Gate: "FORGE Way"

Inspired by SonarQube's "Sonar Way" — opinionated defaults that can be overridden.

```python
@dataclass
class QualityGate:
    """Binary pass/fail quality gate."""

    # Dimension minimums (any below = fail)
    min_security_score: int = 40         # No critical vulns
    min_reliability_score: int = 30      # Basic error handling exists
    min_test_score: int = 20             # Tests exist

    # Finding thresholds (on NEW findings only, via baseline comparison)
    max_new_critical: int = 0            # Zero new critical findings
    max_new_high: int = 0               # Zero new high findings
    max_new_medium: int | None = None    # No limit on medium

    # Composite minimum
    min_composite_score: int = 40        # Overall D or above
```

### Gate Evaluation

```python
@dataclass
class QualityGateResult:
    passed: bool
    failures: list[str]          # Human-readable failure reasons
    scores: dict[str, int]       # Per-dimension scores
    composite_score: int
    gate_config: QualityGate

def evaluate_quality_gate(
    scores: DimensionScores,
    baseline_comparison: BaselineComparison | None,
    gate: QualityGate,
) -> QualityGateResult:
    failures = []

    if scores.security < gate.min_security_score:
        failures.append(f"Security score {scores.security} < {gate.min_security_score} minimum")
    if scores.reliability < gate.min_reliability_score:
        failures.append(f"Reliability score {scores.reliability} < {gate.min_reliability_score} minimum")
    if scores.test_quality < gate.min_test_score:
        failures.append(f"Test quality score {scores.test_quality} < {gate.min_test_score} minimum")

    composite = scores.composite()
    if composite < gate.min_composite_score:
        failures.append(f"Composite score {composite} < {gate.min_composite_score} minimum")

    if baseline_comparison:
        new_crit = len([f for f in baseline_comparison.new_findings if f.severity == "critical"])
        new_high = len([f for f in baseline_comparison.new_findings if f.severity == "high"])

        if new_crit > gate.max_new_critical:
            failures.append(f"{new_crit} new critical findings (max {gate.max_new_critical})")
        if new_high > gate.max_new_high:
            failures.append(f"{new_high} new high findings (max {gate.max_new_high})")

    return QualityGateResult(
        passed=len(failures) == 0,
        failures=failures,
        scores=scores.to_dict(),
        composite_score=composite,
        gate_config=gate,
    )
```

### Gate Profiles

| Profile | Use Case | Security Min | Test Min | Max New Critical |
|---------|----------|-------------|----------|-----------------|
| **forge-way** (default) | General purpose | 40 | 20 | 0 |
| **strict** | Pre-production, regulated | 60 | 40 | 0 |
| **startup** | MVPs, prototypes | 30 | 0 | 0 |
| **custom** | User-defined | Configurable | Configurable | Configurable |

Config:
```python
# ForgeConfig
quality_gate: str | dict = "forge-way"  # profile name or custom dict
```

CLI:
```bash
vibe2prod scan . --gate strict
vibe2prod scan . --gate '{"min_security_score": 50, "max_new_critical": 0}'
```

---

## Layer 2: LLM Advisory

LLM findings are the existing FORGE discovery output (security auditor, quality auditor, architecture reviewer, swarm workers). They do NOT affect the deterministic score.

### Actionability Tiers

Every LLM finding is classified into an actionability tier based on severity, confidence, and project context (from the prompt overhaul spec):

| Tier | Label | Criteria | Report Framing |
|------|-------|----------|----------------|
| 1 | **Must Fix** | Critical/High + confidence >= 0.85 + concrete exploit | "This is exploitable now. Fix before shipping." |
| 2 | **Should Fix** | High/Medium + confidence >= 0.7 + evidence-based | "This is a real issue. Prioritize in current sprint." |
| 3 | **Consider** | Medium/Low OR overlaps with known compromise | "Given your project stage, this may not be urgent." |
| 4 | **Informational** | Low OR architectural opinion OR deferred by context | "Noted for completeness. Address when ready." |

### LLM-to-Deterministic Promotion

Some LLM findings can be "promoted" to deterministic checks for future scans:

1. If a finding type is confirmed as true positive 5+ times across scans (via feedback loop), it becomes a candidate for a new deterministic check
2. The pattern extractor (from vulnerability pattern library spec) proposes new `DeterministicSignal` entries
3. Human reviews and promotes to curated pattern library

This is the learning flywheel: LLM finds novel issues → confirmed findings become deterministic rules → deterministic rules produce stable scores.

---

## Standards Compliance Mapping

### OWASP ASVS Level Estimation

Map deterministic checks to ASVS chapters. Report estimated compliance level.

```python
ASVS_CHECK_MAP = {
    # Chapter: [(check_id, asvs_requirement, level), ...]
    "V1 - Encoding/Sanitization": [
        ("SEC-002", "V1.5.3 - Parameterized queries", 1),
        ("SEC-009", "V1.7.2 - No raw error output", 1),
    ],
    "V6 - Authentication": [
        ("SEC-004", "V6.2.1 - Auth required on routes", 1),
        ("SEC-005", "V6.2.3 - Secure password storage", 1),
        ("SEC-006", "V6.3.1 - No debug auth bypass", 1),
    ],
    "V11 - Cryptography": [
        ("SEC-005", "V11.1.1 - No deprecated crypto", 1),
    ],
    "V13 - Configuration": [
        ("SEC-001", "V13.1.3 - No hardcoded secrets", 1),
        ("SEC-006", "V13.4.1 - Secure default config", 1),
        ("SEC-012", "V13.4.2 - No insecure defaults", 1),
    ],
    "V14 - Data Protection": [
        ("SEC-010", "V14.3.3 - No PII in logs", 1),
    ],
}

def estimate_asvs_level(check_results: dict[str, bool]) -> dict:
    """Estimate ASVS compliance level from deterministic check results."""
    l1_checks = [(cid, req) for chapter, checks in ASVS_CHECK_MAP.items()
                  for cid, req, level in checks if level == 1]
    l1_pass = sum(1 for cid, _ in l1_checks if check_results.get(cid, False))
    l1_total = len(l1_checks)

    return {
        "estimated_level": 1 if l1_pass == l1_total else 0,
        "level_1_coverage": f"{l1_pass}/{l1_total}",
        "level_1_percent": round(l1_pass / l1_total * 100, 1) if l1_total else 0,
        "failing_checks": [cid for cid, _ in l1_checks if not check_results.get(cid, False)],
    }
```

### STRIDE Mapping

Every security finding (deterministic and LLM) maps to STRIDE:

| FORGE Check Category | STRIDE | Attack Enabled |
|---------------------|--------|----------------|
| SEC-001 (secrets) | Information Disclosure | Credential theft |
| SEC-002 (SQL injection) | Tampering, Info Disclosure | Data manipulation/exfiltration |
| SEC-003 (command injection) | Tampering, Elevation of Privilege | RCE |
| SEC-004 (missing auth) | Spoofing | Unauthorized access |
| SEC-005 (weak crypto) | Information Disclosure | Password cracking |
| SEC-006 (debug mode) | Information Disclosure | Internal state exposure |
| SEC-007 (CORS wildcard) | Spoofing | Cross-origin attacks |
| SEC-010 (PII in logs) | Information Disclosure | Data leak via log aggregation |
| SEC-011 (no validation) | Tampering | Data corruption |

### NIST SSDF Coverage

Report which SSDF practices FORGE evaluates:

| SSDF Practice | FORGE Coverage | How |
|---------------|---------------|-----|
| PW.5 - Secure coding | Direct | Security dimension checks |
| PW.7 - Code review | Direct | LLM advisory (code review agent) |
| PW.8 - Test executable code | Direct | Test quality dimension |
| RV.1 - Identify vulnerabilities | Direct | Full discovery pipeline |
| RV.2 - Assess and prioritize | Direct | Triage + actionability tiers |
| PO.3 - Supporting toolchains | Partial | Operations dimension (CI/CD, linter) |
| PS.1 - Protect code | Partial | SEC-001 (no secrets in code) |

---

## Context-Aware Severity Modifiers

Deterministic modifiers applied to both Layer 1 checks and Layer 2 findings:

| Context | Detection | Modifier |
|---------|-----------|----------|
| Test file | File path: `test_*`, `*_test.*`, `*.spec.*`, `__tests__/` | Suppress security findings; quality findings at -1 severity |
| Generated code | Path: `generated/`, `proto/`, `__generated__/` | Suppress unless critical |
| Example/sample code | Path: `examples/`, `samples/` | -1 severity |
| Migration files | Path: `migrations/`, `alembic/` | Skip (already in SKIP_DIRS) |
| Vendor/third-party | Path: `vendor/`, `node_modules/`, `third_party/` | Skip entirely |
| Suppression with justification | `.forgeignore` match with `reason` field | Suppress with note |
| Suppression without justification | `.forgeignore` match, no `reason` | Flag the suppression itself |
| ADR-documented decision | `docs/adr/` or `docs/decisions/` file references the pattern | Suppress with ADR reference |

---

## Feedback Loop

Track per-agent and per-check false positive rates across scans.

### Storage

`.artifacts/feedback.json`:

```json
{
  "checks": {
    "SEC-002": {
      "total_triggers": 45,
      "confirmed_fp": 3,
      "fp_rate": 0.067
    }
  },
  "agents": {
    "security_auditor/auth_flow": {
      "total_findings": 120,
      "suppressed_by_forgeignore": 18,
      "suppressed_by_user": 5,
      "fp_rate": 0.192
    },
    "architecture_reviewer": {
      "total_findings": 89,
      "suppressed_by_forgeignore": 31,
      "suppressed_by_user": 12,
      "fp_rate": 0.483
    }
  },
  "updated_at": "2026-03-18T12:00:00Z"
}
```

### Thresholds

| FP Rate | Action |
|---------|--------|
| < 20% | Normal — no action |
| 20-40% | Warning logged: "Agent X has {rate}% FP rate — consider prompt tuning" |
| > 40% | Advisory findings from this agent get downgraded one actionability tier |

### Integration

After `.forgeignore` and baseline comparison, update feedback stats:

```python
# In forge/phases.py, after suppression
feedback = Feedback.load(repo_path)
feedback.record_scan(
    check_results=deterministic_results,
    llm_findings=all_findings,
    suppressed=suppressed_findings,
)
feedback.save(repo_path)

# Downgrade high-FP agents
for finding in advisory_findings:
    agent_fp = feedback.agent_fp_rate(finding.source_agent)
    if agent_fp > 0.4:
        finding.actionability = downgrade_tier(finding.actionability)
```

---

## Delta Mode

Scan only changed files since last baseline. Reduces cost and time for iterative fix-scan loops.

```python
# ForgeConfig
delta_mode: bool = False
```

### How It Works

1. Load baseline — get `last_scan_commit` SHA
2. `git diff --name-only {last_sha} HEAD` → changed files
3. Pass changed file list to context builder as inclusion filter
4. Deterministic checks run on changed files only
5. LLM advisory scans changed files with full-repo context available for cross-reference
6. Baseline comparison: new findings in changed files = truly new; findings in unchanged files = assumed persisting

### When Delta Mode Activates

- `delta_mode: true` in config AND baseline exists AND repo has commits since last scan
- Falls back to full scan if no baseline or no git history

---

## Report Structure

### JSON Report

```json
{
  "version": "3.0",
  "scan_id": "abc123",
  "repo_path": "/path/to/repo",
  "generated_at": "2026-03-18T12:00:00Z",

  "scores": {
    "composite": 67,
    "band": "B",
    "dimensions": {
      "security": { "score": 72, "checks_passed": 10, "checks_failed": 2, "deductions": -28 },
      "reliability": { "score": 55, "checks_passed": 4, "checks_failed": 3, "deductions": -45 },
      "maintainability": { "score": 80, "checks_passed": 4, "checks_failed": 1, "deductions": -20 },
      "test_quality": { "score": 45, "checks_passed": 3, "checks_failed": 4, "deductions": -55 },
      "performance": { "score": 85, "checks_passed": 4, "checks_failed": 1, "deductions": -15 },
      "documentation": { "score": 60, "checks_passed": 3, "checks_failed": 3, "deductions": -40 },
      "operations": { "score": 70, "checks_passed": 4, "checks_failed": 2, "deductions": -30 }
    }
  },

  "quality_gate": {
    "passed": false,
    "profile": "forge-way",
    "failures": [
      "Test quality score 45 < 20 minimum — PASSED",
      "1 new critical finding (max 0)"
    ]
  },

  "compliance": {
    "owasp_asvs": {
      "estimated_level": 0,
      "level_1_coverage": "9/12",
      "level_1_percent": 75.0,
      "failing_checks": ["SEC-004", "SEC-006", "SEC-012"]
    },
    "nist_ssdf": {
      "practices_evaluated": 7,
      "practices_passing": 5
    },
    "stride_coverage": {
      "spoofing": "partial",
      "tampering": "covered",
      "repudiation": "not_evaluated",
      "information_disclosure": "covered",
      "denial_of_service": "not_evaluated",
      "elevation_of_privilege": "covered"
    }
  },

  "deterministic_checks": [
    {
      "id": "SEC-001",
      "name": "Hardcoded secrets",
      "passed": false,
      "severity": "critical",
      "locations": [{"file": "config.py", "line": 42}],
      "stride": "information_disclosure",
      "asvs_ref": "V13.1.3"
    }
  ],

  "advisory_findings": {
    "must_fix": [],
    "should_fix": [],
    "consider": [],
    "informational": []
  },

  "baseline_delta": {
    "new": 3,
    "recurring": 12,
    "fixed": 5,
    "regressed": 0,
    "suppressed": 4
  },

  "feedback": {
    "high_fp_agents": ["architecture_reviewer"],
    "checks_with_fp": []
  },

  "project_context": {}
}
```

### CLI Output

```
FORGE v3 Evaluation Report
==========================

Composite Score: 67/100 (B — Near Ready)
Quality Gate:    FAILED

Dimensions:
  Security        ████████████████████░░░░░  72  (2 checks failed)
  Reliability     ██████████████░░░░░░░░░░░  55  (3 checks failed)
  Maintainability ████████████████████████░  80  (1 check failed)
  Test Quality    ████████████░░░░░░░░░░░░░  45  (4 checks failed)
  Performance     █████████████████████████  85  (1 check failed)
  Documentation   ████████████████░░░░░░░░░  60  (3 checks failed)
  Operations      ██████████████████████░░░  70  (2 checks failed)

Gate Failures:
  ✗ 1 new critical finding (max 0)

OWASP ASVS: Level 0 (75% of Level 1 — 3 checks remain)
NIST SSDF:  5/7 evaluated practices passing

Baseline Delta: +3 new, -5 fixed, 12 recurring, 4 suppressed

Advisory Findings:
  Must Fix (1):   SEC — Hardcoded API key in config.py:42
  Should Fix (4): 2 security, 1 quality, 1 performance
  Consider (7):   3 quality, 2 architecture, 2 performance
  Info (6):       4 architecture, 2 quality
```

---

## Implementation Plan

### Module Structure

```
forge/evaluation/
    __init__.py
    dimensions.py        # DimensionScores, per-dimension check runners
    checks/
        __init__.py
        security.py      # SEC-001 through SEC-012
        reliability.py   # REL-001 through REL-007
        maintainability.py # MNT-001 through MNT-005
        test_quality.py  # TST-001 through TST-007
        performance.py   # PRF-001 through PRF-005
        documentation.py # DOC-001 through DOC-006
        operations.py    # OPS-001 through OPS-006
    quality_gate.py      # QualityGate, QualityGateResult, evaluate_quality_gate
    compliance.py        # ASVS mapping, STRIDE mapping, NIST SSDF
    feedback.py          # Per-agent/per-check FP tracking
    report.py            # JSON + CLI report rendering
```

### Track 1: Core Evaluation Engine

| # | File | Change |
|---|------|--------|
| 1 | `forge/evaluation/dimensions.py` | DimensionScores dataclass, composite calculation, score bands |
| 2 | `forge/evaluation/checks/security.py` | SEC-001 through SEC-012 (regex + AST) |
| 3 | `forge/evaluation/checks/reliability.py` | REL-001 through REL-007 |
| 4 | `forge/evaluation/checks/maintainability.py` | MNT-001 through MNT-005 |
| 5 | `forge/evaluation/checks/test_quality.py` | TST-001 through TST-007 |
| 6 | `forge/evaluation/checks/performance.py` | PRF-001 through PRF-005 |
| 7 | `forge/evaluation/checks/documentation.py` | DOC-001 through DOC-006 |
| 8 | `forge/evaluation/checks/operations.py` | OPS-001 through OPS-006 |

### Track 2: Quality Gate

| # | File | Change |
|---|------|--------|
| 9 | `forge/evaluation/quality_gate.py` | QualityGate, gate profiles, evaluate_quality_gate() |
| 10 | `forge/config.py` | Add `quality_gate`, `evaluation_weights`, `delta_mode` fields |

### Track 3: Compliance & Mapping

| # | File | Change |
|---|------|--------|
| 11 | `forge/evaluation/compliance.py` | ASVS_CHECK_MAP, STRIDE mapping, NIST SSDF, estimate_asvs_level() |

### Track 4: Feedback Loop

| # | File | Change |
|---|------|--------|
| 12 | `forge/evaluation/feedback.py` | Feedback class, load/save, record_scan(), fp_rate calculations |

### Track 5: Report & Integration

| # | File | Change |
|---|------|--------|
| 13 | `forge/evaluation/report.py` | JSON report builder, CLI output formatter |
| 14 | `forge/phases.py` | Wire evaluation after discovery: run deterministic checks → compute scores → evaluate gate → build report |
| 15 | `forge/schemas.py` | Add EvaluationResult, QualityGateResult to ForgeResult |
| 16 | `forge/cli.py` | Add `--gate` flag, display evaluation in scan output |

### Track 6: Tests

| # | File | Change |
|---|------|--------|
| 17 | `tests/unit/test_evaluation_security.py` | 12 tests: one per SEC check |
| 18 | `tests/unit/test_evaluation_reliability.py` | 7 tests: one per REL check |
| 19 | `tests/unit/test_evaluation_maintainability.py` | 5 tests |
| 20 | `tests/unit/test_evaluation_test_quality.py` | 7 tests |
| 21 | `tests/unit/test_evaluation_dimensions.py` | Composite score, bands, weights |
| 22 | `tests/unit/test_quality_gate.py` | Gate evaluation, profiles, custom config |
| 23 | `tests/unit/test_compliance.py` | ASVS level estimation, STRIDE mapping |
| 24 | `tests/unit/test_feedback.py` | FP tracking, threshold warnings, tier downgrade |
| 25 | `tests/integration/test_evaluation_pipeline.py` | Full eval on fixture repo |

---

## Commit Sequence

| # | Scope | Description |
|---|-------|-------------|
| 1 | `forge/evaluation/dimensions.py` | Dimension scoring framework with composite calculation |
| 2 | `forge/evaluation/checks/security.py` | 12 deterministic security checks |
| 3 | `forge/evaluation/checks/reliability.py` | 7 reliability checks |
| 4 | `forge/evaluation/checks/maintainability.py` | 5 maintainability checks (AST-based) |
| 5 | `forge/evaluation/checks/test_quality.py` | 7 test quality checks |
| 6 | `forge/evaluation/checks/performance.py` + `documentation.py` + `operations.py` | Remaining dimension checks |
| 7 | `forge/evaluation/quality_gate.py` + `forge/config.py` | Quality gate with profiles |
| 8 | `forge/evaluation/compliance.py` | ASVS, STRIDE, NIST SSDF mapping |
| 9 | `forge/evaluation/feedback.py` | Per-agent FP tracking |
| 10 | `forge/evaluation/report.py` | JSON + CLI report rendering |
| 11 | `forge/phases.py` + `forge/schemas.py` | Wire evaluation into pipeline |
| 12 | `forge/cli.py` | CLI flags and display |
| 13 | `tests/unit/test_evaluation_*.py` | All unit tests (~50) |
| 14 | `tests/integration/test_evaluation_pipeline.py` | Integration test on fixture repo |

---

## Verification

```bash
# Unit tests
pytest tests/unit/test_evaluation_*.py tests/unit/test_quality_gate.py tests/unit/test_compliance.py tests/unit/test_feedback.py -v

# Full regression
pytest -q

# Determinism test: run eval twice on same repo, scores MUST be identical
vibe2prod scan . --discovery-only 2>/dev/null | grep "Composite Score"
vibe2prod scan . --discovery-only 2>/dev/null | grep "Composite Score"
# Both lines must match exactly

# Gate test
vibe2prod scan . --gate strict
# Expected: FAILED (most repos won't pass strict on first scan)

# ASVS test
vibe2prod scan ./benchmarks/express-api --discovery-only | grep "OWASP ASVS"
# Should show Level 0 with specific failing checks
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| AST checks produce false positives | Each check has a specificity threshold; FP feedback loop auto-adjusts |
| Tree-sitter parsing fails on exotic languages | Fall back to regex-only checks; skip AST-dependent checks gracefully |
| Score gaming (users suppress everything) | Report suppression count prominently; flag unjustified suppressions |
| Dimension weights feel wrong for a use case | Weights are configurable via `evaluation_weights` in ForgeConfig |
| Deterministic checks miss real issues | LLM advisory layer catches what deterministic misses; promotion flywheel adds new checks |
| Quality gate too strict for MVPs | "startup" profile has relaxed thresholds; user can set custom gates |
| ASVS mapping incomplete | Start with Level 1 only; expand coverage incrementally; clearly label as "estimated" |

---

## Relationship to Other Specs

| Spec | Relationship |
|------|-------------|
| **Prompt overhaul** | Layer 2 advisory quality depends on improved prompts. Actionability tiers defined there are consumed here. |
| **Vulnerability pattern library** | Curated patterns feed both deterministic checks (signals) and LLM guidance. VP-001 etc. become SEC checks. |
| **Hive discovery** | Swarm workers produce Layer 2 advisory findings. Evaluation framework consumes them. |
| **SWE-AF remediation** | Remediation targets findings. Evaluation re-scores after remediation to measure improvement. |
| **Finding fingerprints/baseline** (v2) | Baseline comparison feeds quality gate (new findings only) and delta reporting. Already implemented. |
| **CLI integration** | `forge_scan` MCP tool returns evaluation results. `forge_status` shows score progression. |
| **RunTelemetry** | Evaluation runs are tracked in telemetry. Deterministic checks are near-instant (no LLM cost). |
