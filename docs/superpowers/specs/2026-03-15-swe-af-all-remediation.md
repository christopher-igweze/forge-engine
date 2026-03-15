# SWE-AF for All Remediation — Architecture Redesign

**Date:** 2026-03-15
**Status:** Spec complete, ready for implementation
**Scope:** forge-engine remediation phase only (Agents 7-12)

---

## Problem

FORGE's current coder agent is an autonomous agent that receives a full context dump (86-line system prompt + finding JSON + 56-file codebase map + "figure it out") and burns 300K+ tokens per finding at $0.15-0.30 each. A 37-finding scan cost $20+ and took 2.5 hours. This is unusable.

The coder doesn't need to think. It needs to execute pre-planned edits.

## Decision

**Route ALL remediation through SWE-AF's DAG executor.** FORGE handles discovery + triage (cheap, $0.50 with M2.5). SWE-AF handles all code changes via granular task decomposition with parallel M2.5 workers.

Drop FORGE's inner loop coder entirely.

---

## Architecture Change

### Before (current)
```
Discovery (M2.5) → Triage (Haiku) → Fix Strategist (Haiku)
    → Tier 0/1: deterministic
    → Tier 2: FORGE inner loop (Sonnet coder, 300K tokens/finding, $0.15-0.30 each)
    → Tier 3: SWE-AF DAG executor
```

### After (new)
```
Discovery (M2.5) → Triage (Haiku) → Fix Strategist (Haiku)
    → Tier 0/1: deterministic (unchanged)
    → Tier 2 + Tier 3: ALL go to SWE-AF DAG executor
        → Sprint Planner decomposes into granular edit tasks
        → Parallel M2.5 workers execute edit-level instructions
        → Stuck detection kills spinning tasks
        → Debt tracking for unfixable items
```

### Cost comparison (37 findings)

| Approach | Discovery | Remediation | Total | Duration |
|----------|-----------|-------------|-------|----------|
| FORGE coder (Sonnet) | $0.50 | $20+ | $20+ | 160 min |
| FORGE coder (M2.5) | $0.50 | $0.86 | $1.36 | 160 min |
| SWE-AF (M2.5 workers) | $0.50 | ~$1-2 | ~$2-3 | ~15-25 min |

SWE-AF with M2.5 workers should be comparable cost to FORGE M2.5 but **much faster** because:
- Sprint Planner reads code once, generates all edit tasks upfront
- Workers execute in parallel (no serial "read → think → plan → edit" per finding)
- Stuck detection kills looping workers after 3 attempts
- Granular tasks = fewer tokens per worker call

---

## Critical: Cost Circuit Breaker

**This is the first thing to implement. Non-negotiable.**

### Hard cost cap in the executor

```python
# forge/execution/cost_guard.py

class CostGuard:
    """Hard cost cap that kills the run when budget is exceeded."""

    def __init__(self, max_cost_usd: float = 5.0):
        self.max_cost_usd = max_cost_usd
        self._spent = 0.0
        self._lock = asyncio.Lock()

    async def record(self, cost_usd: float) -> None:
        async with self._lock:
            self._spent += cost_usd
            if self._spent >= self.max_cost_usd:
                raise CostLimitExceeded(
                    f"Cost limit exceeded: ${self._spent:.2f} >= ${self.max_cost_usd:.2f}. "
                    f"Run aborted to protect your budget."
                )

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def remaining(self) -> float:
        return max(0, self.max_cost_usd - self._spent)


class CostLimitExceeded(Exception):
    """Raised when the hard cost cap is hit."""
    pass
```

### Wired into every LLM call

```python
# In AgentAI.run() or the standalone dispatcher:
result = await llm_call(...)
await cost_guard.record(result.cost_usd)  # Raises if over budget
```

### Config

```python
# forge/config.py
max_cost_usd: float = 5.0  # Hard cap, kills run immediately
cost_warning_threshold: float = 0.8  # Warn at 80% of budget
```

### CLI flag

```bash
vibe2prod scan . --max-cost 5.00
vibe2prod scan . --max-cost 10.00  # Override for large repos
```

---

## Implementation Plan

### Track 1: Cost Guard (do first, before anything else)

| # | File | Change |
|---|------|--------|
| 1 | `forge/execution/cost_guard.py` | NEW: CostGuard class + CostLimitExceeded |
| 2 | `forge/config.py` | Add `max_cost_usd`, `cost_warning_threshold` |
| 3 | `forge/vendor/agent_ai/client.py` | Wire cost_guard.record() after every LLM call |
| 4 | `forge/standalone.py` | Initialize CostGuard, pass to dispatcher |
| 5 | `forge/cli.py` | Add `--max-cost` flag |
| 6 | `tests/unit/test_cost_guard.py` | NEW: 6 tests (record, exceed, remaining, warning, concurrent) |

### Track 2: Route all remediation to SWE-AF

| # | File | Change |
|---|------|--------|
| 7 | `forge/execution/tier_router.py` | Remove Tier 2/3 split — all AI items go to SWE-AF |
| 8 | `forge/phases.py` | Remove `execute_remediation()` call for Tier 2, route all to `execute_tier3_via_sweaf()` |
| 9 | `forge/execution/sweaf_adapter.py` | Update to handle Tier 2 items (simpler findings) |
| 10 | `forge/config.py` | Remove `sweaf_enabled` flag (SWE-AF is now the only path), add `sweaf_model` default to M2.5 |
| 11 | `forge/execution/forge_executor.py` | Keep inner loop for fallback only (when SWE-AF unavailable) |

### Track 3: Configure SWE-AF for M2.5 granular execution

| # | File | Change |
|---|------|--------|
| 12 | `forge/execution/sweaf_adapter.py` | Set `model_override: "minimax/minimax-m2.5"` in plan_result |
| 13 | `forge/execution/sweaf_bridge.py` | Pass `max_cost_usd` from CostGuard.remaining to SWE-AF |
| 14 | `forge/execution/sweaf_bridge.py` | Add incremental cost tracking from SWE-AF poll responses |

### Track 4: Tests

| # | File | Change |
|---|------|--------|
| 15 | `tests/unit/test_cost_guard.py` | NEW: 6 tests |
| 16 | `tests/unit/test_tier_router.py` | Update: all AI items route to SWE-AF |
| 17 | `tests/unit/test_sweaf_adapter.py` | Update: Tier 2 items handled |
| 18 | `tests/integration/test_sweaf_bridge.py` | Update: cost passthrough, incremental tracking |

### Track 5: Cleanup

| # | File | Change |
|---|------|--------|
| 19 | `forge/prompts/coder.py` | Mark as deprecated (kept for fallback only) |
| 20 | `forge/execution/forge_executor.py` | Simplify: remove Tier 2 inner loop, keep as SWE-AF fallback only |

---

## Commit Sequence

| # | Scope | Description |
|---|-------|-------------|
| 1 | `cost_guard.py` + `config.py` | Add hard cost cap with CostLimitExceeded exception |
| 2 | `client.py` + `standalone.py` | Wire cost guard into every LLM call |
| 3 | `cli.py` | Add --max-cost flag |
| 4 | `test_cost_guard.py` | 6 unit tests for cost guard |
| 5 | `tier_router.py` | Route all AI items to SWE-AF (remove Tier 2/3 split) |
| 6 | `phases.py` | Remove FORGE executor for Tier 2, all to SWE-AF |
| 7 | `sweaf_adapter.py` | Handle Tier 2 items + M2.5 model override |
| 8 | `sweaf_bridge.py` | Pass remaining budget + incremental cost tracking |
| 9 | `test_tier_router.py` + `test_sweaf_*.py` | Update tests for new routing |
| 10 | `forge_executor.py` + `coder.py` | Simplify to fallback-only, mark deprecated |

---

## Verification

```bash
# Unit tests
PYTHONPATH=. pytest tests/unit/test_cost_guard.py tests/unit/test_tier_router.py tests/unit/test_sweaf_adapter.py tests/integration/test_sweaf_bridge.py -v

# Full regression
PYTHONPATH=. pytest -q

# Cost guard smoke test (should abort at $0.01)
OPENROUTER_API_KEY=... vibe2prod scan . --max-cost 0.01
# Expected: "Cost limit exceeded: $0.01 >= $0.01. Run aborted."

# Full scan with safe budget
OPENROUTER_API_KEY=... vibe2prod scan . --max-cost 3.00
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| SWE-AF unavailable | `sweaf_fallback_to_forge=True` falls back to simplified FORGE executor |
| SWE-AF doesn't respect M2.5 override | Pass `model_override` in plan_result config, verify in bridge |
| Cost guard kills mid-fix | CostLimitExceeded is caught at pipeline level, partial results saved |
| Granular tasks still expensive | M2.5 at $0.25/1M input is 12x cheaper than Sonnet — even verbose tasks are cheap |
