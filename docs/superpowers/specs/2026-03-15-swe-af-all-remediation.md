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

## Critical: Real-Time Telemetry + Circuit Breakers

**This is the first thing to implement. Non-negotiable. No more blind runs.**

### Design Principle

Every resource metric (cost, time, tokens, agent status) must be:
1. **Updated in real-time** — after every single LLM call, not at checkpoints
2. **Accessible at any moment** — queryable while the run is in progress, not just after completion
3. **Written to disk incrementally** — survives crashes, kills, and interruptions
4. **Protected by circuit breakers** — hard caps on cost AND time that kill the run immediately

### RunTelemetry — Real-Time Observable State

```python
# forge/execution/run_telemetry.py

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentStatus:
    """Current state of a single agent invocation."""
    agent_name: str
    model: str
    status: str  # "running" | "completed" | "failed" | "killed"
    started_at: float
    completed_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finding_id: str = ""
    error: str = ""


class RunTelemetry:
    """Real-time telemetry for a FORGE run. Always accessible, always current.

    Writes to disk after every update so state survives crashes.
    Readable by external tools (MCP server, CLI status, monitoring) at any time.
    """

    def __init__(
        self,
        artifacts_dir: str,
        max_cost_usd: float = 5.0,
        max_duration_seconds: float = 1800.0,  # 30 min default
    ):
        self._dir = Path(artifacts_dir) / "telemetry"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._start_time = time.time()

        # Budget limits
        self.max_cost_usd = max_cost_usd
        self.max_duration_seconds = max_duration_seconds

        # Cumulative counters (updated after every LLM call)
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_invocations: int = 0
        self.failed_invocations: int = 0

        # Per-agent breakdown
        self.cost_by_agent: dict[str, float] = {}
        self.cost_by_model: dict[str, float] = {}
        self.tokens_by_agent: dict[str, int] = {}

        # Active agents (currently running)
        self.active_agents: dict[str, AgentStatus] = {}

        # Phase tracking
        self.current_phase: str = "initializing"
        self.phases_completed: list[str] = []

        # Findings progress
        self.total_findings: int = 0
        self.findings_fixed: int = 0
        self.findings_deferred: int = 0
        self.findings_in_progress: int = 0

        # Write initial state
        self._flush()

    # ── Circuit Breakers ──────────────────────────────────────────

    def check_budget(self) -> None:
        """Raise immediately if cost or time budget exceeded."""
        if self.total_cost_usd >= self.max_cost_usd:
            self._flush()
            raise CostLimitExceeded(
                f"BUDGET EXCEEDED: ${self.total_cost_usd:.2f} >= "
                f"${self.max_cost_usd:.2f} limit. Run killed."
            )
        elapsed = time.time() - self._start_time
        if elapsed >= self.max_duration_seconds:
            self._flush()
            raise TimeLimitExceeded(
                f"TIME EXCEEDED: {elapsed:.0f}s >= "
                f"{self.max_duration_seconds:.0f}s limit. Run killed."
            )

    # ── Recording (called after every LLM call) ──────────────────

    async def record_invocation(
        self,
        agent_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        success: bool = True,
        finding_id: str = "",
    ) -> None:
        """Record a single LLM call. Updates all counters and flushes to disk."""
        async with self._lock:
            self.total_cost_usd += cost_usd
            self.total_tokens += input_tokens + output_tokens
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_invocations += 1
            if not success:
                self.failed_invocations += 1

            self.cost_by_agent[agent_name] = (
                self.cost_by_agent.get(agent_name, 0) + cost_usd
            )
            self.cost_by_model[model] = (
                self.cost_by_model.get(model, 0) + cost_usd
            )
            self.tokens_by_agent[agent_name] = (
                self.tokens_by_agent.get(agent_name, 0) + input_tokens + output_tokens
            )

            # Append to invocations log (survives crashes)
            self._append_invocation({
                "ts": time.time(),
                "agent": agent_name,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "success": success,
                "finding_id": finding_id,
                "cumulative_cost": self.total_cost_usd,
                "budget_remaining": self.max_cost_usd - self.total_cost_usd,
                "elapsed_seconds": time.time() - self._start_time,
            })

            # Flush summary to disk
            self._flush()

        # Check circuit breakers AFTER recording
        self.check_budget()

    # ── Agent lifecycle tracking ──────────────────────────────────

    def agent_started(self, agent_id: str, agent_name: str, model: str, finding_id: str = "") -> None:
        self.active_agents[agent_id] = AgentStatus(
            agent_name=agent_name,
            model=model,
            status="running",
            started_at=time.time(),
            finding_id=finding_id,
        )
        self._flush()

    def agent_completed(self, agent_id: str, cost_usd: float = 0) -> None:
        if agent_id in self.active_agents:
            self.active_agents[agent_id].status = "completed"
            self.active_agents[agent_id].completed_at = time.time()
            self.active_agents[agent_id].cost_usd = cost_usd
        self._flush()

    def agent_failed(self, agent_id: str, error: str = "") -> None:
        if agent_id in self.active_agents:
            self.active_agents[agent_id].status = "failed"
            self.active_agents[agent_id].completed_at = time.time()
            self.active_agents[agent_id].error = error
        self._flush()

    # ── Phase tracking ────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        if self.current_phase != "initializing":
            self.phases_completed.append(self.current_phase)
        self.current_phase = phase
        self._flush()

    def update_findings_progress(self, total: int = 0, fixed: int = 0, deferred: int = 0, in_progress: int = 0) -> None:
        if total: self.total_findings = total
        if fixed: self.findings_fixed = fixed
        if deferred: self.findings_deferred = deferred
        if in_progress: self.findings_in_progress = in_progress
        self._flush()

    # ── Snapshot (readable by external tools at any time) ─────────

    def snapshot(self) -> dict:
        """Current state as a dict. Safe to call from any thread/process."""
        elapsed = time.time() - self._start_time
        return {
            "status": "running",
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_human": f"{int(elapsed//60)}m {int(elapsed%60)}s",
            "budget": {
                "cost_spent": round(self.total_cost_usd, 4),
                "cost_limit": self.max_cost_usd,
                "cost_remaining": round(self.max_cost_usd - self.total_cost_usd, 4),
                "cost_percent": round(self.total_cost_usd / self.max_cost_usd * 100, 1) if self.max_cost_usd > 0 else 0,
                "time_spent": round(elapsed, 0),
                "time_limit": self.max_duration_seconds,
                "time_remaining": round(max(0, self.max_duration_seconds - elapsed), 0),
                "time_percent": round(elapsed / self.max_duration_seconds * 100, 1) if self.max_duration_seconds > 0 else 0,
            },
            "totals": {
                "invocations": self.total_invocations,
                "failed": self.failed_invocations,
                "tokens": self.total_tokens,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
            },
            "cost_by_agent": dict(sorted(self.cost_by_agent.items(), key=lambda x: -x[1])),
            "cost_by_model": dict(sorted(self.cost_by_model.items(), key=lambda x: -x[1])),
            "phase": self.current_phase,
            "phases_completed": self.phases_completed,
            "findings": {
                "total": self.total_findings,
                "fixed": self.findings_fixed,
                "deferred": self.findings_deferred,
                "in_progress": self.findings_in_progress,
            },
            "active_agents": [
                {
                    "name": a.agent_name,
                    "model": a.model,
                    "status": a.status,
                    "running_for": f"{time.time() - a.started_at:.0f}s",
                    "finding_id": a.finding_id,
                }
                for a in self.active_agents.values()
                if a.status == "running"
            ],
        }

    # ── Disk persistence ──────────────────────────────────────────

    def _flush(self) -> None:
        """Write current snapshot to disk. Called after every state change."""
        try:
            snapshot = self.snapshot()
            # Atomic write via temp file
            tmp = self._dir / "live_status.tmp"
            target = self._dir / "live_status.json"
            tmp.write_text(json.dumps(snapshot, indent=2))
            tmp.rename(target)
        except Exception:
            pass  # Never fail on telemetry write

    def _append_invocation(self, record: dict) -> None:
        """Append a single invocation to the JSONL log."""
        try:
            with open(self._dir / "invocations.jsonl", "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass


class CostLimitExceeded(Exception):
    """Hard cost cap hit. Run must stop immediately."""
    pass


class TimeLimitExceeded(Exception):
    """Hard time cap hit. Run must stop immediately."""
    pass
```

### How external tools read it

The `live_status.json` file is updated after **every single LLM call**. Any tool can read it at any time:

```bash
# CLI: check status of running scan
cat .artifacts/telemetry/live_status.json | python3 -m json.tool

# MCP server: forge_status tool reads this file
# Monitoring: watch -n 1 cat .artifacts/telemetry/live_status.json
```

Example `live_status.json` during a run:
```json
{
  "status": "running",
  "elapsed_seconds": 142.3,
  "elapsed_human": "2m 22s",
  "budget": {
    "cost_spent": 0.4821,
    "cost_limit": 5.0,
    "cost_remaining": 4.5179,
    "cost_percent": 9.6,
    "time_spent": 142,
    "time_limit": 1800,
    "time_remaining": 1658,
    "time_percent": 7.9
  },
  "totals": {
    "invocations": 12,
    "failed": 0,
    "tokens": 284102,
    "input_tokens": 241000,
    "output_tokens": 43102
  },
  "cost_by_agent": {
    "security_auditor/infrastructure": 0.14,
    "security_auditor/auth_flow": 0.12,
    "architecture_reviewer": 0.09
  },
  "phase": "discovery",
  "findings": {
    "total": 0,
    "fixed": 0,
    "deferred": 0,
    "in_progress": 0
  },
  "active_agents": [
    {"name": "quality_auditor/code_patterns", "model": "minimax/minimax-m2.5", "status": "running", "running_for": "23s", "finding_id": ""},
    {"name": "quality_auditor/performance", "model": "minimax/minimax-m2.5", "status": "running", "running_for": "18s", "finding_id": ""}
  ]
}
```

### MCP integration

Add a `forge_status` tool that reads `live_status.json`:

```python
@mcp.tool()
def forge_status(path: str) -> dict:
    """Get real-time status of a running FORGE scan including cost, time, and agent activity."""
    status_file = Path(path) / ".artifacts" / "telemetry" / "live_status.json"
    if not status_file.exists():
        return {"status": "no_active_run"}
    return json.loads(status_file.read_text())
```

### CLI integration

```bash
# While a scan is running in another terminal:
vibe2prod status .
# Output:
# FORGE Run Status
# ================
# Phase: remediation (discovery ✓, triage ✓)
# Time:  2m 22s / 30m 0s (7.9%)
# Cost:  $0.48 / $5.00 (9.6%)
# Calls: 12 (0 failed)
#
# Findings: 25 total, 3 fixed, 0 deferred, 8 in progress
#
# Active agents:
#   quality_auditor/code_patterns  minimax-m2.5  running 23s
#   quality_auditor/performance    minimax-m2.5  running 18s
```

### Config

```python
# forge/config.py
max_cost_usd: float = 5.0          # Hard cap — kills run immediately
max_duration_seconds: float = 1800  # 30 min hard cap
cost_warning_threshold: float = 0.8 # Log warning at 80% of budget
```

### CLI flags

```bash
vibe2prod scan . --max-cost 5.00 --max-time 1800
vibe2prod scan . --max-cost 10.00 --max-time 3600  # Override for large repos
vibe2prod status .  # Check running scan status
```

---

## Implementation Plan

### Track 1: Real-Time Telemetry + Circuit Breakers (do first, before anything else)

| # | File | Change |
|---|------|--------|
| 1 | `forge/execution/run_telemetry.py` | NEW: RunTelemetry class with real-time state, disk flush, circuit breakers |
| 2 | `forge/config.py` | Add `max_cost_usd`, `max_duration_seconds`, `cost_warning_threshold` |
| 3 | `forge/vendor/agent_ai/client.py` | Wire `telemetry.record_invocation()` after every LLM call |
| 4 | `forge/standalone.py` | Initialize RunTelemetry, pass to dispatcher, catch CostLimitExceeded/TimeLimitExceeded |
| 5 | `forge/cli.py` | Add `--max-cost`, `--max-time` flags + `vibe2prod status .` command |
| 6 | `forge/mcp_server.py` | Add `forge_status` tool that reads live_status.json |
| 7 | `forge/phases.py` | Wire `set_phase()`, `update_findings_progress()` at phase transitions |
| 8 | `tests/unit/test_run_telemetry.py` | NEW: 10 tests (record, cost exceed, time exceed, snapshot, flush, agents, phases, concurrent) |

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
| 1 | `run_telemetry.py` + `config.py` | Real-time telemetry with cost + time circuit breakers |
| 2 | `client.py` + `standalone.py` | Wire telemetry into every LLM call + phase transitions |
| 3 | `cli.py` | Add --max-cost, --max-time flags + `status` command |
| 4 | `mcp_server.py` | Add forge_status tool reading live_status.json |
| 5 | `phases.py` | Wire set_phase() and update_findings_progress() |
| 6 | `test_run_telemetry.py` | 10 unit tests for telemetry + circuit breakers |
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
