# Hybrid Remediation: FORGE Tier 2 Fixes + SWE-AF Integration for Tier 3

**Status:** Planned
**Date:** 2026-03-14
**Scope:** Remediation phase (Agents 7-12). Discovery (Agents 1-4) and Triage (Agents 5-6) unchanged.

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Competitive Landscape](#competitive-landscape)
3. [FORGE vs SEC-AF: Discovery Comparison](#forge-vs-sec-af-discovery-comparison)
4. [SWE-AF Architecture Analysis](#swe-af-architecture-analysis)
5. [Current Remediation Bugs](#current-remediation-bugs)
6. [Architecture Decision: Hybrid Approach](#architecture-decision-hybrid-approach)
7. [Implementation Plan](#implementation-plan)
8. [Testing & Verification](#testing--verification)
9. [Risk Mitigation](#risk-mitigation)
10. [Commit Sequence](#commit-sequence)

---

## Problem Statement

FORGE's remediation phase has 5 confirmed bugs that prevent reliable code fixes, especially for complex cross-cutting findings (Tier 3). The 3-loop control system is architecturally sound but has implementation gaps:

1. **SPLIT escalation is silently dropped** — split sub-items are parsed but never executed
2. **No regression testing** — only Agent 9's new tests are run, never the existing suite
3. **Code reviewer prompt is a stub** — marked "Phase 1 stub" with no security criteria
4. **Blind merge conflicts** — `-X theirs` silently overwrites prior fixes
5. **No test coverage** for middle/outer loops

Meanwhile, AgentField's [SWE-AF](https://github.com/Agent-Field/SWE-AF) has a battle-tested coding engine with 3-nested control loops, shared memory, stuck detection, debt tracking, and worktree isolation — and its `execute()` endpoint accepts pre-formed issues, bypassing the full planning pipeline.

**Strategy:** Fix FORGE for Tier 2 (scoped fixes). Route Tier 3 (complex fixes) to SWE-AF's DAG executor. Keep Tier 0/1 deterministic.

---

## Competitive Landscape

### SEC-AF (Agent-Field/sec-af)

Released 2026-03-04. Apache 2.0. AI-native security auditor built on AgentField.

**Key numbers:**
- ~250 coordinated agents per audit
- 94% noise reduction via adversarial HUNT → PROVE pipeline
- $0.18–$0.90 per full audit (Kimi K2.5 via OpenRouter)
- SARIF 2.1.0 output + PCI-DSS/SOC2/OWASP/HIPAA compliance mapping

**Architecture (from source code, not README):**

| Phase | What Happens | Agent Count |
|-------|-------------|-------------|
| RECON | 5 sub-agents: architecture mapper, dependency auditor, config scanner, data flow mapper, security context profiler. Fast 3-way parallel, then deep 2-way. | 5 |
| HUNT | 12 specialized strategy hunters (injection, XSS, DoS, SSRF, auth, crypto, business logic, data exposure, supply chain, config/secrets, API security, language-specific). Semaphore-bounded parallel with incremental dedup. | 12 |
| DEDUP | Fingerprint-based (file + line + CWE hash) → AI semantic dedup → chain correlation | 1-3 |
| PROVE | Per finding: tracer + sanitization analyzer (parallel) → exploit hypothesizer → verdict agent. Structurally adversarial. | 4 per finding |
| REMEDIATION | Generates patch diffs for confirmed/likely findings. No verification. | 1 per finding |

**What SEC-AF does well (verified in code):**
- Adversarial verification is real — tracer and sanitization analyzer run concurrently via `asyncio.gather()`, then exploit hypothesizer, then verdict agent with opposing goals
- Context pruning per strategy via `STRATEGY_CONTEXT_MAP` — injection hunter gets data flows, crypto hunter gets key management
- Flat LLM output schemas (pipe-delimited strings) with coercion validators handle LLM output malformation gracefully
- CWE severity floors prevent LLM underestimation (e.g., CWE-78 can never be below "critical")
- Scoring formula is published and transparent

**What SEC-AF does NOT have:**
- No multi-dimensional analysis (security only, no quality or architecture)
- No intent-aware filtering (no convention extractor, no intent analyzer, no actionability classifier)
- No code graph or community detection (reads repo flat)
- No convergence loop (runs once)
- No learning system (each scan independent)
- Remediation generates patches but does NOT verify they compile or pass tests
- DAST exists as code but is behind a feature flag (`enable_dast=False`) — aspirational

### SWE-AF (Agent-Field/SWE-AF)

Autonomous software engineering team. One API call ships planned, coded, tested, reviewed code.

**Architecture (from source code):**
- 16 execution agents (PM, architect, tech lead, sprint planner, coder, QA, code reviewer, QA synthesizer, merger, integration tester, verifier, issue advisor, replanner, retry advisor, issue writer, git init)
- 3-nested control loops: inner (coder retry + stuck detection), middle (issue advisor: retry/split/accept-with-debt/escalate), outer (replanner: restructure DAG)
- Issue DAG with topological sort into parallel execution levels
- Git worktree isolation per issue for parallel execution
- Shared memory system (conventions, failure patterns, bug patterns, interfaces)
- Checkpoint/resume for crash recovery
- Multi-repo support throughout

**Key design patterns shared with SEC-AF:**
- Same AgentField backbone (`Agent` + `@router.reasoner()`)
- Semaphore-bounded `asyncio.gather` for concurrency
- Structured output via file-based JSON injection + backup schema agent
- Exponential backoff retry for transient errors

**Key differences from SEC-AF:**
- SWE-AF's DAG is dynamic (work items with dependencies). SEC-AF's pipeline is linear (phases).
- SWE-AF has 3 nested control loops. SEC-AF has 1 (hunt → prove).
- SWE-AF has explicit shared memory between agents. SEC-AF agents are stateless.
- SWE-AF has 45+ test files. SEC-AF has 17.

---

## FORGE vs SEC-AF: Discovery Comparison

| Dimension | FORGE | SEC-AF | Verdict |
|-----------|-------|--------|---------|
| **Analysis scope** | Security + quality + architecture (6 parallel audit passes + 1 architecture review + 1 codebase analysis) | Security only (5 recon + 12 hunters) | FORGE wins — multi-dimensional |
| **False positive filtering** | 3-layer: convention extractor (zero-LLM, parses .eslintrc/pyproject/tsconfig) → intent analyzer (12 suppression patterns + LLM batch) → actionability classifier (deterministic) | Adversarial PROVE phase (tracer + sanitization + exploit + verdict) | Different strengths. FORGE filters by developer intent. SEC-AF filters by exploitability. |
| **Code understanding** | Swarm mode: tree-sitter AST → NetworkX graph → Louvain community detection → parallel workers per segment → Wave 2 MoA cross-referencing | Flat repo reading, no graph, no cross-segment analysis | FORGE wins — graph-based segmentation |
| **Adversarial verification** | Security auditor prompt has self-check step ("argue against your own finding") but in same prompt | Structurally separate agents with opposing goals, 4-agent chain per finding | SEC-AF wins — genuine adversarial architecture |
| **Context management** | Keyword-based file scoring per audit pass, 80k token budget | Per-strategy context builders with item limits, priority sorting, relevance filtering | SEC-AF wins — more surgical context pruning |
| **Streaming** | Sequential between phases (discovery completes before triage starts) | HUNT pushes to asyncio.Queue, PROVE consumes incrementally | SEC-AF wins — streaming pipeline |
| **Convergence** | Iterates remediation → delta discovery → re-triage until target score (default 95). Stall detection stops at < 5pt improvement. | Single pass. | FORGE wins — unique capability |
| **Learning** | Textual gradients from AdalFlow/LLM-AutoDiff. Critic generates prompt improvements on failure. Pattern library tracks prevalence and false positive rates. | None. Each scan independent. | FORGE wins — learning loop |
| **Output formats** | ForgeResult only | JSON + SARIF 2.1.0 + Markdown + compliance reports | SEC-AF wins — industry-standard output |
| **Compliance** | None | CWE-to-framework mapping + AI gap analysis (PCI-DSS, SOC2, OWASP, HIPAA) | SEC-AF wins |
| **Cost controls** | Telemetry cost tracking but no budget enforcement | `max_cost_usd`, per-phase budget percentages, time budgets | SEC-AF wins |
| **Remediation** | Full coder agents with 3-loop control, test generation, code review (has bugs, see below) | Generates patch diffs only. No verification. | FORGE wins — actually fixes code |

**Bottom line:** FORGE has 6 capabilities SEC-AF lacks (multi-dimensional, intent filtering, graph segmentation, convergence, learning, actual remediation). SEC-AF has 5 capabilities FORGE lacks (adversarial verification, streaming, per-strategy context pruning, SARIF/compliance, budget controls). These are complementary, not directly competing.

---

## SWE-AF Architecture Analysis

### Why SWE-AF's execute() fits as a remediation engine

SWE-AF's `execute` reasoner (`swe_af/app.py`) accepts a pre-formed `plan_result` dict:

```python
plan_result = {
    "issues": [{"name", "title", "description", "acceptance_criteria",
                "depends_on", "files_to_modify", "guidance", ...}],
    "levels": [["issue-names"], ["issue-names"], ...],
    "artifacts_dir": "...",
    "prd": {},           # can be minimal/empty
    "architecture": {},  # can be minimal/empty
}
```

This **completely bypasses** PM, Architect, Tech Lead, and Sprint Planner. SWE-AF already uses this exact pattern internally for its own verify-fix cycles (app.py lines 506-523).

### Schema alignment

| FORGE `RemediationItem` | SWE-AF `PlannedIssue` | Mapping |
|---|---|---|
| `finding_id` | `name` | `f"fix-{finding_id.lower()}"` |
| `title` | `title` | Direct |
| `approach` + finding's `description`, `data_flow`, `suggested_fix`, `locations` | `description` | Pack all security context into description |
| `acceptance_criteria` | `acceptance_criteria` | Direct |
| `depends_on` (finding IDs) | `depends_on` (issue names) | Map via kebab-case |
| `files_to_modify` | `files_to_modify` | Direct |
| finding's `severity` | `guidance.needs_deeper_qa` | `True` if critical/high |
| FORGE `execution_levels` | `levels` | Direct (list of lists of names) |

### SWE-AF capabilities FORGE remediation is missing

| Capability | SWE-AF | Current FORGE Remediation |
|---|---|---|
| Stuck-loop detection | Window of 3 non-blocking fix cycles → auto-accept with debt | None — loops until exhaustion |
| Shared memory | Conventions, failure patterns, bug patterns propagate between issues | Context broker tracks completions but no convention/pattern sharing |
| Debt tracking | `IssueAdaptation` records every AC modification as structured debt | Deferred findings tracked as IDs only |
| Checkpoint/resume | Saves state per iteration to `artifacts_dir` | No checkpointing |
| Issue splitting | Issue advisor decides SPLIT with depth limit, splits are actually executed | SPLIT is parsed but **silently dropped** (Bug #1) |
| Grace mechanism | If coder produced file changes but reviewer never blocked → `COMPLETED_WITH_DEBT` instead of `FAILED` | Inner loop exhaustion → straight to middle loop escalation |

### Cost trade-off

SWE-AF's coding loop uses 2-4 LLM calls per iteration per issue (coder + reviewer, or coder + QA + reviewer + synthesizer). With retries, a single fix could use 6-12 LLM calls. For 20 findings at Tier 3, that's 120-240 calls.

FORGE's current remediation uses 1-3 calls per finding (coder + optional test gen + optional reviewer).

**Mitigation:** Only route Tier 3 (complex, cross-cutting) to SWE-AF. These are the findings that FORGE struggles with anyway. Tier 2 (simple, scoped) stays in FORGE's lighter-weight inner loop.

---

## Current Remediation Bugs

### Bug 1: SPLIT escalation silently dropped

**File:** `forge/execution/forge_executor.py` (line ~1014)

**Root cause:** In `_execute_single_fix()`, after the middle loop returns, the code handles:
- `EscalationAction.DEFER` (line 984) — stores deferral context
- `EscalationAction.RECLASSIFY` (line 992) — promotes tier, retries inner loop
- `EscalationAction.ESCALATE` (line 1015) — `pass` (outer loop handles)

Missing: `EscalationAction.SPLIT`. The LLM escalation agent (`_llm_escalation`, lines 694-703) correctly parses split items into `decision.split_items`, but `_execute_single_fix` has no handler for it. Split items are created and then discarded.

**Impact:** Tier 3 findings that need decomposition are silently deferred or escalated instead of being split into manageable sub-fixes.

### Bug 2: No existing test suite regression check

**File:** `forge/execution/forge_executor.py` (~line 503)

**Root cause:** `run_inner_loop()` calls `run_tests_in_worktree()` with only the test files generated by Agent 9:
```python
test_files=[tfc.path for tfc in loop_state.test_result.test_file_contents]
```

The existing project test suite is never executed during the inner loop. Agent 11 (Integration Validator) is supposed to catch regressions post-merge, but it depends on the LLM choosing to run tests — not deterministic.

**Impact:** A fix that passes new tests but breaks existing tests won't be caught until (maybe) Agent 11, by which point the fix is already merged.

### Bug 3: Code reviewer prompt is Phase 1 stub

**File:** `forge/prompts/code_reviewer.py` (line 7 comment)

**Root cause:** The `SYSTEM_PROMPT` is functional but minimal. It has generic review criteria with no security-specific checks. Comment at top: "Phase 1 stub -- full implementation in Phase 2."

**Impact:** The reviewer can't catch security anti-patterns (eval with user input, disabled security middleware, hardcoded secrets) and doesn't differentiate between BLOCK-worthy security regressions and REQUEST_CHANGES polish items.

### Bug 4: Blind merge conflict resolution

**File:** `forge/execution/worktree.py` (merge_worktree, ~line 244)

**Root cause:** On merge conflict, `merge_worktree()` uses `git merge -X theirs`, always taking the coder's version. If two parallel fixes in the same execution level modify the same file (e.g., both adding imports to the same module), the second merge silently overwrites the first fix's changes.

**Impact:** Fixes can undo each other when they touch overlapping files.

### Bug 5: No test coverage for middle/outer loops

**Files:** `tests/unit/` and `tests/integration/`

**Root cause:** Inner loop has 7 unit tests. Full executor has 4 integration tests. But `run_middle_loop()`, `run_outer_loop()`, `_llm_escalation()`, `_heuristic_escalation()`, and `run_convergence_loop()` have zero dedicated tests.

**Impact:** The SPLIT bug (Bug 1) has existed since initial implementation because there are no tests exercising the middle loop paths.

---

## Architecture Decision: Hybrid Approach

### Options Considered

| Option | Pros | Cons |
|---|---|---|
| **A. Fix FORGE only** | Full control, lower per-fix cost, no external dependency | More engineering work, still can't match SWE-AF's coding loop sophistication for complex fixes |
| **B. Replace with SWE-AF only** | Get 3-loop control, shared memory, stuck detection for free | 2-12x cost increase for all fixes, no security vocabulary in prompts, overkill for simple Tier 2 fixes |
| **C. Hybrid (chosen)** | Light fixes stay fast/cheap in FORGE, complex fixes get SWE-AF's full machinery | Two code paths to maintain, Tier 3 depends on external service |

### Decision: Option C — Hybrid

- **Tier 0:** No-op (unchanged)
- **Tier 1:** Deterministic template fixes (unchanged)
- **Tier 2:** Fixed FORGE inner loop (scoped, 1-3 files, Sonnet coder, max 15 turns)
- **Tier 3:** SWE-AF DAG executor (cross-cutting, 5-15 files, 3-loop control with shared memory)

```
┌─────────────────────────────────────────────────────────┐
│                    Tier Router                           │
│  route_plan_items() → (handled, tier2_items, tier3_items)│
└──────┬───────────────────┬──────────────────┬───────────┘
       │                   │                  │
  ┌────▼────┐        ┌────▼────┐       ┌─────▼──────┐
  │ Tier 0/1│        │ Tier 2  │       │  Tier 3    │
  │ (determ)│        │ (FORGE) │       │  (SWE-AF)  │
  └────┬────┘        └────┬────┘       └─────┬──────┘
       │                  │                   │
       │            ┌─────▼──────┐     ┌──────▼───────┐
       │            │ Inner Loop │     │ sweaf_adapter │
       │            │ + regress  │     │ → HTTP POST   │
       │            │ + reviewer │     │ → poll         │
       │            └─────┬──────┘     │ → map results  │
       │                  │            └──────┬────────┘
       │                  │                   │
       └──────────────────┴───────────────────┘
                          │
                  ┌───────▼──────┐
                  │ Validation   │
                  │ (unchanged)  │
                  └──────────────┘
```

Fallback: If SWE-AF is unavailable (`sweaf_enabled=False` or connection error with `sweaf_fallback_to_forge=True`), Tier 3 items fall back to FORGE's executor.

---

## Implementation Plan

### Track A: Fix FORGE Tier 2 (5 changes)

#### A1. Config additions
**File:** `forge/config.py`

Add to `ForgeConfig`:
```python
# Regression check
enable_regression_check: bool = True
regression_test_timeout: int = 180  # seconds for full suite run

# SWE-AF integration for Tier 3
sweaf_enabled: bool = False
sweaf_agentfield_url: str = ""
sweaf_api_key: str = ""
sweaf_node_id: str = "swe-planner"
sweaf_max_coding_iterations: int = 3
sweaf_max_concurrent_issues: int = 3
sweaf_runtime: Literal["claude_code", "open_code"] = "claude_code"
sweaf_timeout_seconds: int = 1800  # 30 min per Tier 3 batch
sweaf_fallback_to_forge: bool = True
sweaf_max_cost_usd: float = 10.0
```

#### A2. Wire SPLIT escalation execution
**File:** `forge/execution/forge_executor.py` (~line 1014)

Add handler between RECLASSIFY (line 992) and ESCALATE (line 1015):

```python
elif escalation.action == EscalationAction.SPLIT and escalation.split_items:
    logger.info("Middle loop: SPLIT %s into %d sub-items", finding.id, len(escalation.split_items))
    for split_item in escalation.split_items:
        # Create synthetic finding inheriting parent's metadata
        split_finding = AuditFinding(
            id=split_item.finding_id,
            title=split_item.title,
            description=finding.description,
            category=finding.category,
            severity=finding.severity,
            locations=finding.locations,
            suggested_fix=finding.suggested_fix,
            data_flow=finding.data_flow,
            cwe_id=finding.cwe_id,
            owasp_ref=finding.owasp_ref,
        )
        split_inner = await run_inner_loop(
            app, node_id, split_item, split_finding,
            worktree_path, codebase_map, cfg, resolved_models,
            prior_changes=prior_changes,
        )
        if split_inner.coder_result and split_inner.coder_result.outcome == FixOutcome.COMPLETED:
            if worktree_path != state.repo_path:
                merged = merge_worktree(state.repo_path, worktree_path,
                    target_branch=get_current_branch(state.repo_path))
                if not merged:
                    split_inner.coder_result.outcome = FixOutcome.COMPLETED_WITH_DEBT
            state.completed_fixes.append(split_inner.coder_result)
        else:
            state.outer_loop.deferred_findings.append(split_item.finding_id)
            _store_deferral_context(state, split_item.finding_id, split_inner, escalation)
    # Parent finding was split — track it
    state.outer_loop.deferred_findings.append(finding.id)
```

#### A3. Add existing test suite regression check
**File:** `forge/execution/forge_executor.py` (after Agent 9 test execution, ~line 503)

After running generated tests, add:

```python
# Regression check: run existing test suite
if cfg.enable_regression_check:
    try:
        from forge.execution.test_runner import detect_test_framework, run_tests_in_worktree
        if detect_test_framework(worktree_path):
            regression_exec = run_tests_in_worktree(
                worktree_path, test_files=None, timeout=cfg.regression_test_timeout,
            )
            if regression_exec and not regression_exec.success:
                regression_class = _classify_test_failure(regression_exec)
                if regression_class == "code_bug":
                    logger.warning("Regression: existing tests failing for %s", finding.title)
                    if loop_state.review_result and loop_state.review_result.decision == ReviewDecision.APPROVE:
                        loop_state.review_result.decision = ReviewDecision.REQUEST_CHANGES
                        loop_state.review_result.summary = (
                            f"Fix regresses existing tests ({regression_exec.tests_failed}/"
                            f"{regression_exec.tests_run} failed): {regression_exec.error_output[:300]}"
                        )
    except Exception as e:
        logger.warning("Regression check failed (non-fatal): %s", e)
```

#### A4. Upgrade code reviewer prompt
**File:** `forge/prompts/code_reviewer.py`

Replace Phase 1 stub SYSTEM_PROMPT with security-aware version:
- Security review checklist: input validation, auth boundaries, error exposure, secrets
- BLOCK triggers: `eval()`/`exec()` with user input, disabled security middleware, hardcoded secrets
- REQUEST_CHANGES triggers: silent catch blocks, string concatenation for SQL/commands, raw error details to clients
- Decision bias: lean APPROVE (partial fix > no fix), only BLOCK for severe security regressions
- Required response format: `{"decision", "summary", "issues", "suggestions", "regression_risk"}`

#### A5. Improve merge conflict strategy
**File:** `forge/execution/worktree.py` (merge_worktree, ~line 244)

Replace blind `-X theirs` with rebase-first strategy:
1. On conflict → abort merge → attempt `git rebase` of coder branch onto target
2. If rebase succeeds → retry merge
3. If rebase fails → fall back to `-X theirs` BUT log warning + mark as `COMPLETED_WITH_DEBT`

---

### Track B: Integrate SWE-AF for Tier 3 (5 changes)

#### B1. SWE-AF adapter module
**New file:** `forge/execution/sweaf_adapter.py`

Core functions:

**`finding_to_planned_issue(item, finding) -> dict`**
- Maps `RemediationItem` + `AuditFinding` to SWE-AF `PlannedIssue` dict
- Packs ALL security context into `description`: data_flow, locations with line numbers and code snippets, CWE/OWASP, suggested_fix, remediation approach
- Sets `guidance.needs_deeper_qa = True` for critical/high severity
- Sets `guidance.review_focus` to security-specific review instruction

**`write_issue_files(issues, artifacts_dir) -> str`**
- Writes `.md` files that SWE-AF's coder reads from `issues_dir`
- Format: title, description, acceptance criteria checklist, files to modify

**`compute_execution_levels(issues) -> list[list[str]]`**
- Topological sort from dependency graph (Kahn's algorithm)
- Falls back to single level if circular dependencies detected

**`sweaf_result_to_coder_fix_results(sweaf_result, finding_map) -> list[CoderFixResult]`**
- Maps SWE-AF `DAGState` issue outcomes to FORGE `CoderFixResult`
- Status mapping: completed → COMPLETED, partial → COMPLETED_WITH_DEBT, failed → FAILED_RETRYABLE

#### B2. SWE-AF HTTP bridge
**New file:** `forge/execution/sweaf_bridge.py`

Follows same HTTP pattern as `vibe2prod/backend/services/forge_bridge.py`:

**`execute_tier3_via_sweaf(tier3_items, findings, state, cfg) -> list[CoderFixResult]`**
1. Convert items via adapter
2. Write issue `.md` files to `{artifacts_dir}/sweaf-issues/`
3. Build synthetic `plan_result` with just `issues` and `levels` (no PM/architect)
4. POST to `{sweaf_agentfield_url}/api/v1/execute/async/{sweaf_node_id}.execute`
5. Poll `GET /api/v1/executions/{id}` every 10s until complete/failed/timeout
6. Map results back to `CoderFixResult` via adapter
7. On any failure: return `FAILED_RETRYABLE` results, log error

#### B3. Tier routing split
**File:** `forge/execution/tier_router.py`

Change `route_plan_items()` return signature:
```python
# Before:
def route_plan_items(...) -> tuple[list[RemediationItem], list[RemediationItem]]:
    ...
    return handled, ai_items

# After:
def route_plan_items(...) -> tuple[list[RemediationItem], list[RemediationItem], list[RemediationItem]]:
    ...
    return handled, tier2_items, tier3_items
```

Split the `elif item.tier in (TIER_2, TIER_3)` (line 158) into two branches:
```python
elif item.tier == RemediationTier.TIER_2:
    tier2_items.append(item)
elif item.tier == RemediationTier.TIER_3:
    tier3_items.append(item)
```

#### B4. Wire into phases.py
**File:** `forge/phases.py` (lines 492-548)

Update `_run_remediation()`:
```python
handled, tier2_items, tier3_items = route_plan_items(...)

# Tier 2 → FORGE executor (existing)
if tier2_items:
    ai_plan = RemediationPlan(items=tier2_items, ...)
    state.remediation_plan = ai_plan
    await execute_remediation(app, NODE_ID, state, cfg, resolved_models)

# Tier 3 → SWE-AF (with FORGE fallback)
if tier3_items:
    if cfg.sweaf_enabled:
        try:
            from forge.execution.sweaf_bridge import execute_tier3_via_sweaf
            results = await execute_tier3_via_sweaf(tier3_items, state.all_findings, state, cfg)
            state.completed_fixes.extend(results)
        except Exception:
            if cfg.sweaf_fallback_to_forge:
                # Fall back to FORGE for Tier 3
                await _run_tier3_via_forge(app, state, cfg, resolved_models, tier3_items)
    else:
        await _run_tier3_via_forge(app, state, cfg, resolved_models, tier3_items)
```

#### B5. Tests

**New file: `tests/unit/test_sweaf_adapter.py`** (10 tests)
1. `test_finding_to_planned_issue_basic` — basic conversion
2. `test_finding_to_planned_issue_security_context` — data_flow, CWE, OWASP packed into description
3. `test_finding_to_planned_issue_guidance_critical` — needs_deeper_qa=True for critical
4. `test_finding_to_planned_issue_guidance_low` — needs_deeper_qa=False for low
5. `test_compute_levels_no_deps` — all issues in one level
6. `test_compute_levels_chain` — A → B → C
7. `test_compute_levels_diamond` — D depends on B and C, both depend on A
8. `test_result_mapping_success` — completed → COMPLETED
9. `test_result_mapping_partial` — partial → COMPLETED_WITH_DEBT
10. `test_write_issue_files` — .md files written with correct content

**New file: `tests/integration/test_sweaf_bridge.py`** (4 tests, mocked HTTP)
1. `test_execute_success` — mock POST + poll, verify full flow
2. `test_execute_timeout` — verify FAILED_RETRYABLE on timeout
3. `test_execute_connection_error` — verify error handling
4. `test_execute_partial_success` — some issues complete, some fail

**New file: `tests/unit/test_executor_loops.py`** (8 tests)
1. `test_split_escalation_executes` — SPLIT handler runs sub-items
2. `test_split_defers_failed_sub_items` — failed splits are deferred
3. `test_regression_overrides_approve` — existing tests fail → REQUEST_CHANGES
4. `test_regression_ignores_environment` — environment noise doesn't affect decision
5. `test_tier3_routed_to_sweaf` — Tier 3 dispatched when enabled
6. `test_tier3_falls_back` — SWE-AF failure triggers FORGE fallback
7. `test_heuristic_escalation_tier2` — Tier 2 → RECLASSIFY
8. `test_heuristic_escalation_tier3` — Tier 3 → DEFER

**Update: `tests/unit/test_tier_router.py`** (2 new tests)
1. `test_splits_three_ways` — 3-tuple return
2. `test_tier3_not_in_tier2_list` — Tier 3 in third return value

---

## Testing & Verification

### Automated

```bash
# Unit tests (new + existing)
cd forge-engine && PYTHONPATH=. pytest tests/unit/test_executor_loops.py tests/unit/test_sweaf_adapter.py tests/unit/test_tier_router.py -v

# Integration tests (new)
PYTHONPATH=. pytest tests/integration/test_sweaf_bridge.py -v

# Full regression (all 531+ tests must pass)
PYTHONPATH=. pytest -q
```

### Manual

1. **Tier 2 smoke test:** Run FORGE standalone against `benchmarks/discovery_triage_001/express-api` with `sweaf_enabled=False`. Verify:
   - SPLIT escalation creates and executes sub-items (if triggered)
   - Regression check runs existing tests after each fix
   - Reviewer uses security-aware criteria
   - Merge conflicts attempt rebase first

2. **Tier 3 SWE-AF test:** With SWE-AF running on AgentField, run FORGE with `sweaf_enabled=True` against a repo with architectural findings. Verify:
   - Tier 3 items route to SWE-AF
   - Issue .md files written correctly
   - SWE-AF returns results
   - Results map back to CoderFixResult

3. **Fallback test:** Kill SWE-AF, re-run with `sweaf_fallback_to_forge=True`. Verify Tier 3 falls back to FORGE executor gracefully.

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| SWE-AF unavailable | `sweaf_fallback_to_forge=True` (default) — Tier 3 falls back to FORGE executor |
| SWE-AF cost explosion | `sweaf_max_cost_usd=10.0` limit, only Tier 3 (typically 2-5 findings) routes to SWE-AF |
| Git workflow conflicts | SWE-AF creates its own worktrees. After execution, FORGE fetches branches. Separate clone if needed. |
| `route_plan_items()` signature break | Update all callers simultaneously (phases.py + test_tier_router.py) |
| SWE-AF coder lacks security vocabulary | Pack ALL security context into issue description (data_flow, CWE, OWASP, locations, suggested_fix). Set `guidance.review_focus` to security-specific instruction. |
| Regression check too slow | `regression_test_timeout=180s` cap. Gated behind `enable_regression_check` flag. |

---

## Commit Sequence

Micro-commits, each independently valid:

| # | Scope | Description |
|---|-------|-------------|
| 1 | `forge/config.py` | Add regression check + SWE-AF config fields |
| 2 | `forge/prompts/code_reviewer.py` | Upgrade reviewer prompt (replace Phase 1 stub) |
| 3 | `forge/execution/forge_executor.py` | Wire SPLIT escalation execution |
| 4 | `forge/execution/forge_executor.py` | Add existing test suite regression check |
| 5 | `forge/execution/worktree.py` | Rebase-first merge conflict strategy |
| 6 | `forge/execution/sweaf_adapter.py` | New: FORGE ↔ SWE-AF data model translation |
| 7 | `forge/execution/sweaf_bridge.py` | New: AgentField HTTP client for SWE-AF |
| 8 | `forge/execution/tier_router.py` + `forge/phases.py` | 3-way routing + SWE-AF dispatch |
| 9 | `tests/unit/test_executor_loops.py` | New: 8 middle/outer loop tests |
| 10 | `tests/unit/test_sweaf_adapter.py` | New: 10 adapter tests |
| 11 | `tests/integration/test_sweaf_bridge.py` | New: 4 bridge tests (mocked HTTP) |
| 12 | `tests/unit/test_tier_router.py` | Update: 2 tests for 3-way split |

---

## File Reference

| File | Change Type | Purpose |
|------|-------------|---------|
| `forge/config.py` | Modify | 12 new config fields |
| `forge/execution/forge_executor.py` | Modify | SPLIT handler + regression check |
| `forge/execution/tier_router.py` | Modify | 2-tuple → 3-tuple return |
| `forge/execution/worktree.py` | Modify | Rebase-first merge strategy |
| `forge/prompts/code_reviewer.py` | Modify | Replace Phase 1 stub |
| `forge/phases.py` | Modify | Wire SWE-AF into `_run_remediation()` |
| `forge/execution/sweaf_adapter.py` | **New** | FORGE ↔ SWE-AF translation layer |
| `forge/execution/sweaf_bridge.py` | **New** | AgentField HTTP client for SWE-AF |
| `tests/unit/test_executor_loops.py` | **New** | 8 loop tests |
| `tests/unit/test_sweaf_adapter.py` | **New** | 10 adapter tests |
| `tests/integration/test_sweaf_bridge.py` | **New** | 4 bridge tests |
| `tests/unit/test_tier_router.py` | Update | +2 tests for 3-way split |
