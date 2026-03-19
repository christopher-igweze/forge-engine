# FORGE v3 Dead Code Cleanup Spec (Updated)

## Status

Most dead code was already removed in a previous session. Only minor cleanup remains.

## What's Left to Clean

### 1. Stale event emission in `forge/phases.py` line 333

```python
emit_phase_complete(cfg, "intent_analyzer", "Intent analysis complete.")
```

Intent analyzer was removed but this event line stayed. Delete it.

### 2. Dead prompt files (not called anywhere)

- `forge/prompts/quality_auditor.py` — replaced by 47 deterministic checks
- `forge/prompts/architecture_reviewer.py` — replaced by deterministic checks MNT-001 to MNT-005
- `forge/prompts/intent_analyzer.py` — replaced by .forgeignore + `<intent_detection>` in security auditor prompt

These are not imported or called by any active code. Delete them.

### 3. `dry_run` config field in `forge/config.py`

```python
dry_run: bool = False  # scan only, no fixes
```

Everything is discovery-only now — there are no fixes to skip. Remove the field and any references to it in `phases.py` or `standalone.py`.

Search: `grep -rn "dry_run" forge/ --include="*.py"`

### 4. Verify no remaining references to deleted modules

Run after cleanup:
```bash
grep -rn "from forge.app\|app_helpers\|remediation\|worktree\|convergence\|sweaf\|hive\|swarm" forge/ --include="*.py" | grep -v __pycache__ | grep -v "# DEPRECATED\|# removed\|# v3"
```

Should return nothing.

## Execution

Single agent, 15 minutes max:

1. Delete line 333 in `forge/phases.py`
2. Delete `forge/prompts/quality_auditor.py`, `architecture_reviewer.py`, `intent_analyzer.py`
3. Remove `dry_run` field from `forge/config.py` and all references
4. Run `grep` verification
5. Run `python3 -m pytest tests/unit/ -q --ignore=tests/unit/test_mcp_server.py`
6. Commit: `🧹 refactor(v3): remove stale prompt files, dead config, and orphaned event`

## What's Already Been Removed (for reference)

All of these were deleted in a previous session:
- `forge/app.py`, `forge/app_helpers.py` (AgentField)
- `forge/reasoners/remediation.py`, `forge/reasoners/validation.py`
- `forge/execution/worktree.py`, `tier1_handlers.py`, `tier1_helpers.py`, `tier_router.py`, `convergence.py`
- `forge/execution/sweaf_adapter.py`, `sweaf_bridge.py`
- `forge/execution/checkpoint.py`
- `forge/swarm/` directory
- `forge/reasoners/hive_discovery.py`
- `forge/learning/` directory
