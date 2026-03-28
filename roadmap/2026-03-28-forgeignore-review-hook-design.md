# .forgeignore Review Hook — Design Spec

> Feature: Automatic hygiene audit of .forgeignore after every N new entries
> Date: 2026-03-28
> Status: Draft

## Problem

`.forgeignore` accumulates suppression entries over time. Some become stale — the code was fixed, the expiry date passed, or an accepted risk was never re-evaluated. Without a review trigger, the file grows unchecked and suppressions silently hide findings that should surface again.

## Solution

A Claude Code hook that counts `.forgeignore` entries after each commit. Every 5th new entry triggers an automatic audit that checks for expired, orphaned, and stale entries. Prints a summary with actionable guidance.

## Trigger

**Hook type:** Claude Code hook — `PostToolUse` on `Bash`, filtering for `git commit` commands.

**Trigger condition:** After each commit, read `.forgeignore` and count entries. Compare against `last_reviewed_count` stored in `.forge/review_state.json`. If current count >= `last_reviewed_count + review_interval`, run the audit and update `last_reviewed_count`.

**First run with existing .forgeignore:** If `.forge/review_state.json` doesn't exist and `.forgeignore` already has entries, set `last_reviewed_count` to the current count and run the audit once. Subsequent audits trigger every N new entries from that baseline.

**Installation:** Registered automatically by `vibe2prod setup` or `vibe2prod update` (via the update pipeline's hooks step). Can also be added manually to `.claude/settings.json`.

## Audit Checks

| Check | What It Catches | How It Detects |
|-------|----------------|----------------|
| **Expired entries** | `expires` date has passed — should be re-evaluated or removed | Reuses existing `SuppressionRule.is_expired()` method — already implemented in `forgeignore.py` |
| **Orphaned entries** | Suppresses a finding that no longer appears in the latest scan — safe to remove | See "Orphan Detection" section below |
| **Missing reasons** | Entries without a `reason` field — every suppression should explain why | Parses YAML directly (not via `ForgeIgnore.load()`, which silently drops entries with missing reasons) to find rejected entries |
| **Stale accepted risks** | `accepted_risk` entries older than 90 days without an `expires` date — should be re-evaluated | Check `kind == "accepted_risk"` with no `expires`. Entry age determined from `added` field (see below) |

### Orphan Detection

Orphan detection bridges two different representations: `.forgeignore` rules match via `check_id`, `rule_family + file`, etc., while `baseline.json` stores `BaselineEntry` objects with finding fingerprints.

**Approach:** Load baseline entries and reconstruct minimal finding dicts (with `check_id`, `category`, `file`, `line`, `title`, `cwe` fields) from each `BaselineEntry`. Run each through `SuppressionRule.matches()`. A rule that matches zero baseline findings is orphaned.

If `.artifacts/baseline.json` doesn't exist, skip this check entirely.

### Stale Risk Detection

Instead of fragile `git blame` parsing, use an `added` date field on suppression entries. Several entries in the production `.forgeignore` already have `# Added:` comments. For this feature:

1. Promote `added` to a first-class optional field in the v2 schema (e.g., `added: "2026-03-15"`)
2. The `/forgeignore` skill writes `added` on new entries going forward
3. For existing entries without `added`, fall back to the `.forgeignore` file's last-modified date as a conservative estimate
4. Flag `accepted_risk` entries where `days_since_added > stale_risk_days` and no `expires` is set

## Output

When issues are found:

```
.forgeignore review (15 entries, 3 issues found):
  Expired: sup_012 — "TLS bypass in dev mode" expired 2026-03-01
  Orphaned: sup_008 — finding no longer appears in latest scan, safe to remove
  Stale risk: sup_021 — accepted_risk with no expiry, added 90+ days ago

Run `/forgeignore` to clean up, or ignore if these are intentional.
```

When no issues:

```
.forgeignore review (15 entries, all clean)
```

## Configuration

Settings are constants in the audit function with env var overrides. No new config file or `ForgeConfig` fields needed for a v1:

| Setting | Default | Env Var Override |
|---------|---------|-----------------|
| `review_interval` | 5 | `FORGE_FORGEIGNORE_REVIEW_INTERVAL` |
| `stale_risk_days` | 90 | `FORGE_FORGEIGNORE_STALE_DAYS` |

If these prove useful enough to warrant proper config, they can be added to `ForgeConfig` later.

## Tracking State

Runtime state lives in `.forge/review_state.json` — separate from user configuration:

```json
{
  "last_reviewed_count": 15,
  "last_reviewed_at": "2026-03-28T10:00:00Z"
}
```

This file should be gitignored (it's machine-local state, not user config). Written atomically using the same pattern as `config_io.py`.

## Implementation

The audit logic lives in the existing `forge/execution/forgeignore.py` module as a new function:

```python
def audit_forgeignore(repo_path: str, stale_risk_days: int = 90) -> list[dict]:
    """Audit .forgeignore for expired, orphaned, missing-reason, and stale entries.

    Returns list of issues: [{"type": "expired|orphaned|missing_reason|stale_risk", "id": "sup_XXX", "detail": "..."}]
    """
```

This function:
1. Parses `.forgeignore` YAML directly (to catch entries rejected by `ForgeIgnore.load()`)
2. Also loads via `ForgeIgnore.load(repo_path)` for the valid rules (used for orphan matching)
3. Loads baseline findings from `{repo_path}/.artifacts/baseline.json` (if exists)
4. Runs the 4 checks
5. Returns a list of issues

The hook calls this function and formats the output.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No `.forgeignore` file | Skip silently — nothing to audit |
| No `.artifacts/baseline.json` | Skip orphan check, run other 3 checks only |
| No `.forge/review_state.json` | First run: set `last_reviewed_count` to current entry count, run audit once |
| Entry count decreased (entries removed) | Reset `last_reviewed_count` to current count, no audit triggered |
| All entries are v1 format | Still audits expired and missing-reason checks. Orphan and stale-risk checks may be limited without structured fields. |
| Hook runs outside Claude Code | Not possible — Claude Code hooks only run during sessions. Manual commits checked on next session start via entry count comparison. |

## Scope

**In scope:** Post-commit audit hook, 4 checks (expired, orphaned, missing reason, stale risk), configurable interval and stale threshold, summary output, `added` field promotion in v2 schema.

**Out of scope:** Auto-removing entries (just flags them), integration with `/forgeignore` skill (existing skill handles cleanup), v1 format migration prompts.
