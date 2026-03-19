# FORGE v3 Implementation Spec

## Overview

Strip FORGE down to the v3 architecture: 5 LLM calls, deterministic scoring, 3 active agents. Remove deprecated agents from the pipeline, build AIVSS scoring, fix Python 3.9 compat.

Reference architecture: `docs/architecture-v3.md`

## Phase 1: Strip Deprecated Agents from Pipeline

### What to Remove from `forge/phases.py`

In `_run_discovery()` (classic path), the pipeline currently calls these agents that should be REMOVED:

1. **Quality Auditor** — Find where quality auditor is dispatched (look for `quality_auditor` or `quality_audit` calls). Remove the LLM call. The 47 deterministic checks in `forge/evaluation/` cover this.

2. **Architecture Reviewer** — Find where architecture reviewer is dispatched. Remove the LLM call. Deterministic checks MNT-001 to MNT-005 cover structural analysis.

3. **Triage Classifier** — Find where triage classifier runs (likely in `_run_triage()` or within discovery). Remove it. Severity calibration (`forge/execution/severity.py`) + actionability classification (`forge/execution/actionability.py`) handle this deterministically.

4. **Intent Analyzer** — Find where intent analyzer runs. Remove it. `.forgeignore` + convention detection in the security auditor prompt handle this. The `<intent_detection>` block in the security auditor prompt covers test files, ADR comments, naming conventions.

### What to KEEP in the pipeline

1. **Codebase Analyst** (Agent 1) — keep as-is
2. **Opengrep scan** — keep as-is
3. **Security Auditor** (3 passes) — keep as-is
4. **Fix Strategist** — keep as-is
5. **All post-processing** (fingerprint, baseline, forgeignore, severity, quality gate, feedback, readiness, deterministic evaluation) — keep as-is

### How to Remove Safely

Don't delete the agent code files — just stop calling them in the pipeline. In `forge/phases.py`:

```python
# BEFORE (calls 7+ agents):
# Agent 2: Security Auditor (3 passes) — KEEP
# Agent 3: Quality Auditor (3 passes) — REMOVE
# Agent 4: Architecture Reviewer (1 pass) — REMOVE
# Intent Analyzer — REMOVE
# Triage Classifier — REMOVE (from _run_triage or wherever it's called)

# AFTER (calls 3 agents):
# Agent 1: Codebase Analyst (1 call)
# Agent 2: Security Auditor (3 calls)
# Agent 3: Fix Strategist (1 call)
```

For each removed agent:
1. Find the dispatch/call in `phases.py`
2. Comment it out with `# DEPRECATED: covered by deterministic checks`
3. Remove the `invocations +=` line for that agent
4. Make sure `state.all_findings` still collects findings from Opengrep + deterministic checks + security auditor
5. Make sure the fix strategist still receives all findings

### Update `_run_triage()`

If triage is a separate phase called after discovery, either:
- Skip it entirely (fix strategist handles planning)
- Or keep ONLY the fix strategist part, remove the classifier

Read `forge/phases.py` carefully to understand how `_run_triage()` is called and what it does.

### Verification

After removing agents:
1. Run: `python3 -m pytest tests/unit/ -q --ignore=tests/unit/test_mcp_server.py`
2. The scan should produce: CodebaseMap + Opengrep findings + deterministic check results + security auditor findings + remediation plan
3. Cost per scan should drop from ~$0.70 to ~$0.21

## Phase 2: Build AIVSS Scoring

### Spec

Full spec at: `docs/superpowers/specs/aivss-integration-spec.md`

### Files to Create

```
forge/evaluation/aivss.py           — AIVSS calculator (formulas, scoring)
forge/evaluation/aivss_detector.py  — Auto-detect AARS factors from code
tests/unit/test_aivss.py            — Calculator tests
tests/unit/test_aivss_detector.py   — Detection tests
```

### `forge/evaluation/aivss.py`

Implement the AIVSS scoring formula:

```python
AIVSS = ((CVSS_Base + AARS) / 2) × Threat_Multiplier
```

Where:
- `CVSS_Base = min(10, AV × AC × PR × UI × S)` — 5 base metrics
- `AARS = sum(10_factors) / 10 × 10` — normalized to 0-10
- `Threat_Multiplier` — default 1.0

The 10 AARS factors (each 0.0, 0.5, or 1.0):
1. Execution Autonomy
2. External Tool Control Surface
3. Natural Language Interface
4. Contextual Awareness
5. Behavioral Non-Determinism
6. Opacity & Reflexivity
7. Persistent State Retention
8. Dynamic Identity
9. Multi-Agent Interactions
10. Self-Modification

Also implement the weighted formula:
```python
AIVSS_weighted = (0.25 × Base) + (0.45 × AI_Normalized) + (0.30 × Impact)
```

Where Impact = average of Confidentiality, Integrity, Availability, Safety (each 0.0-1.0).

Severity bands: 0=None, 0.1-3.9=Low, 4.0-6.9=Medium, 7.0-8.9=High, 9.0-10.0=Critical.

### `forge/evaluation/aivss_detector.py`

Auto-detect AARS factors by analyzing the codebase. Use the CodebaseMap + file contents:

| Factor | Detection Heuristic |
|--------|-------------------|
| Execution Autonomy | Look for human-confirmation patterns, approval flows. No confirmation = 1.0 |
| Tool Control Surface | Count tool registrations, MCP tool definitions, subprocess calls. 0 = 0.0, 1-5 = 0.5, 6+ = 1.0 |
| Natural Language Interface | Check for prompt/LLM input handling, chat interfaces. Sanitization present = 0.5, raw input = 1.0 |
| Contextual Awareness | Check for os.environ, file system access, network calls. None = 0.0, limited = 0.5, full = 1.0 |
| Non-Determinism | Check for LLM calls, random/sampling, temperature settings. Deterministic = 0.0, low temp = 0.5, high temp = 1.0 |
| Opacity | Check for logging, tracing, audit trail. Full trace = 0.0, partial = 0.5, none = 1.0 |
| Persistent State | Check for database state, session storage, memory systems. Stateless = 0.0, session = 0.5, persistent = 1.0 |
| Dynamic Identity | Check for role switching, identity delegation, impersonation. Fixed = 0.0, role-based = 0.5, arbitrary = 1.0 |
| Multi-Agent | Check for agent spawning, message passing, orchestration. Single = 0.0, supervised = 0.5, unsupervised = 1.0 |
| Self-Modification | Check for code generation, config mutation, prompt modification. None = 0.0, config = 0.5, code = 1.0 |

Use regex/AST patterns on the codebase — keep it deterministic (no LLM needed).

For non-agentic codebases (no LLM calls, no agents), all AARS factors = 0.0 and AIVSS score = just the base CVSS + impact.

### Pipeline Integration

In `forge/phases.py`, after the deterministic evaluation runs (Step 5g in architecture):

```python
# AIVSS scoring
try:
    from forge.evaluation.aivss import calculate_aivss
    from forge.evaluation.aivss_detector import detect_aars_factors

    aars_factors = detect_aars_factors(state.codebase_map, repo_path)
    aivss_result = calculate_aivss(aars_factors, all_findings)
    # Add to report
except Exception as e:
    logger.warning("AIVSS scoring failed (non-fatal): %s", e)
```

Add `aivss_score` to `ForgeResult` in `forge/schemas.py`.

### Report Output

```
AIVSS Score: 6.2/10 (Medium)

AARS Factors:
  Execution Autonomy       ████████░░  0.5  Human-in-the-loop
  Tool Control Surface     ██████████  1.0  Unrestricted tools
  Natural Language Input   ████████░░  0.5  Validated NL input
  Contextual Awareness     ██████████  1.0  Full env access
  Non-Determinism          ██████████  1.0  High temperature
  Opacity                  ████████░░  0.5  Partial logging
  Persistent State         ░░░░░░░░░░  0.0  Stateless
  Dynamic Identity         ░░░░░░░░░░  0.0  Fixed identity
  Multi-Agent              ██████████  1.0  Multi-agent system
  Self-Modification        ░░░░░░░░░░  0.0  No self-modification
```

## Phase 3: Python 3.9 Compatibility

### The Problem

Files in `forge/evaluation/` use Python 3.10+ syntax:
```python
# This breaks on 3.9:
def foo(x: dict[str, float] | None = None): ...

# Fix:
from __future__ import annotations
def foo(x: dict[str, float] | None = None): ...
```

### Files to Fix

Add `from __future__ import annotations` to the top of every file in:
- `forge/evaluation/__init__.py`
- `forge/evaluation/dimensions.py`
- `forge/evaluation/quality_gate.py`
- `forge/evaluation/compliance.py`
- `forge/evaluation/feedback.py`
- `forge/evaluation/report.py`
- `forge/evaluation/checks/*.py` (all 7 check files)

Also ensure `pyyaml` is in `pyproject.toml` dependencies (needed for `.forgeignore`).

### Verification

```bash
# Test with Python 3.9 if available, or just ensure annotations import is present
python3 -m pytest tests/unit/ -q --ignore=tests/unit/test_mcp_server.py
# Should see 0 collection errors
```

## Execution Order

1. **Phase 1 first** — strip deprecated agents. This simplifies the pipeline and reduces scan cost immediately.
2. **Phase 3 second** — Python 3.9 compat. Quick fix, unblocks full test suite.
3. **Phase 2 last** — AIVSS scoring. New feature, takes longest, but builds on clean pipeline.

## Commit Strategy

Phase 1:
- `🔧 refactor(phases): remove quality auditor from discovery pipeline`
- `🔧 refactor(phases): remove architecture reviewer from discovery pipeline`
- `🔧 refactor(phases): remove triage classifier from pipeline`
- `🔧 refactor(phases): remove intent analyzer from pipeline`

Phase 2:
- `✨ feat(aivss): implement OWASP AIVSS scoring calculator`
- `✨ feat(aivss): add AARS factor auto-detection from code analysis`
- `🔧 feat(phases): wire AIVSS scoring into discovery pipeline`
- `🧪 test(aivss): add unit tests for calculator and detector`

Phase 3:
- `🐛 fix(compat): add future annotations for Python 3.9 compatibility`
- `📦 fix(deps): add pyyaml to project dependencies`

## Validation

After all phases:
1. `python3 -m pytest tests/unit/ -q` — all tests pass, 0 collection errors
2. Run a scan on vibe2prod — should complete in 5 LLM calls, ~$0.21
3. Report should show: deterministic score + AIVSS score + security findings + remediation plan
4. No quality auditor, architecture reviewer, triage classifier, or intent analyzer invocations in telemetry
