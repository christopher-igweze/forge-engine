# .forgeignore Review Hook — Design Spec

> Feature: Automatic hygiene audit of .forgeignore after every N new entries
> Date: 2026-03-28
> Status: Draft

## Problem

`.forgeignore` accumulates suppression entries over time. Some become stale — the code was fixed, the expiry date passed, or an accepted risk was never re-evaluated. Without a review trigger, the file grows unchecked and suppressions silently hide findings that should surface again.

## Solution

A Claude Code hook that counts `.forgeignore` entries after each commit. Every 5th new entry triggers an automatic audit that checks for expired, orphaned, missing-reason, and stale entries. Prints a summary with actionable guidance.

## Trigger

**Hook type:** Claude Code hook — `PostToolUse` on `Bash`, filtering for `git commit` commands.

**Trigger condition:** After each commit, read `.forgeignore` and count entries. Compare against `last_reviewed_count` stored in `.forge/config.yml`. If current count >= `last_reviewed_count + review_interval`, run the audit and update `last_reviewed_count`.

**Installation:** Registered automatically by `vibe2prod setup` or `vibe2prod update` (via the update pipeline's hooks step). Can also be added manually to `.claude/settings.json`.

## Audit Checks

| Check | What It Catches | How It Detects |
|-------|----------------|----------------|
| **Expired entries** | `expires` date has passed — should be re-evaluated or removed | Compare `expires` field against current date |
| **Orphaned entries** | Suppresses a finding that no longer appears in the latest scan — safe to remove | Match each rule against findings in `.artifacts/baseline.json`. No match = orphaned. |
| **Missing reasons** | Entries without a `reason` field — every suppression should explain why | Check for missing or empty `reason` field |
| **Stale accepted risks** | `accepted_risk` entries older than 90 days without an `expires` date — should be re-evaluated | Check `kind == "accepted_risk"` with no `expires`, cross-reference git blame for entry age |

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

Added to `.forge/config.yml`:

```yaml
forgeignore_review:
  review_interval: 5       # trigger audit every N new entries (default: 5)
  stale_risk_days: 90      # flag accepted_risk entries older than this (default: 90)
```

Defaults apply when config is absent.

## Tracking State

`last_reviewed_count` is stored in `.forge/config.yml`:

```yaml
forgeignore_review:
  review_interval: 5
  stale_risk_days: 90
  last_reviewed_count: 10   # updated after each audit run
```

This is a local state field, not something the user configures. Updated automatically after each audit.

## Implementation

The audit logic lives in the existing `forge/execution/forgeignore.py` module as a new function:

```python
def audit_forgeignore(repo_path: str, stale_risk_days: int = 90) -> list[dict]:
    """Audit .forgeignore for expired, orphaned, missing-reason, and stale entries.

    Returns list of issues: [{"type": "expired|orphaned|missing_reason|stale_risk", "id": "sup_XXX", "detail": "..."}]
    """
```

This function:
1. Loads `.forgeignore` via `ForgeIgnore.load(repo_path)`
2. Loads baseline findings from `{repo_path}/.artifacts/baseline.json` (if exists)
3. Runs the 4 checks against each suppression rule
4. Returns a list of issues

The hook calls this function and formats the output.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No `.forgeignore` file | Skip silently — nothing to audit |
| No `.artifacts/baseline.json` | Skip orphan check, run other 3 checks only |
| No `.forge/config.yml` | Use defaults (interval: 5, stale_risk_days: 90), create config on first audit |
| Entry count decreased (entries removed) | Reset `last_reviewed_count` to current count, no audit triggered |
| Hook runs outside Claude Code | Not possible — Claude Code hooks only run during sessions. Manual commits checked on next session start via entry count comparison. |

## Scope

**In scope:** Post-commit audit hook, 4 checks (expired, orphaned, missing reason, stale risk), configurable interval and stale threshold, summary output.

**Out of scope:** Auto-removing entries (just flags them), integration with `/forgeignore` skill (existing skill handles cleanup), v1 format migration prompts.
