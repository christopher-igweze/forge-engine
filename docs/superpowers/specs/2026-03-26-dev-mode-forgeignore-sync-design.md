# Dev Mode, URL Env Vars & Forgeignore Training Sync — Design Spec

**Date:** 2026-03-26
**Status:** Approved

## Overview

Three changes: (1) `--dev` flag on setup for staging URL, (2) `VIBE2PROD_URL` passed explicitly as env var through MCP registration, (3) forgeignore training data sync built into the scan pipeline with a backend endpoint and Supabase table to store it.

## 1. Setup `--dev` Flag and VIBE2PROD_URL as Env Var

### Setup Changes

- New flag: `--dev` on `vibe2prod setup` (and `setup --reset --dev`)
- `--dev` sets `VIBE2PROD_URL=https://staging.vibe2prod.verstandai.site` in MCP registration
- Normal setup sets `VIBE2PROD_URL=https://api.vibe2prod.net`
- `VIBE2PROD_URL` is always explicitly passed as `-e` to `claude mcp add` — no more relying solely on the hardcoded default
- Headless: `vibe2prod setup --no-interactive --dev`

### Dev Mode TUI Visual

When `--dev` is active, the TUI header changes:
- Panel title: `vibe2prod (DEV MODE)` instead of `vibe2prod`
- Panel border: yellow instead of blue
- Extra line in panel: `Warning: Dev mode — data syncs to STAGING` with the staging URL shown
- Summary step shows the URL being used

### mcp_server.py

Keeps the fallback `os.environ.get("VIBE2PROD_URL", "https://api.vibe2prod.net")` as safety net. But setup always passes it explicitly now, so the fallback only applies if someone registers MCP manually without the env var.

### register_mcp() Changes

```python
def register_mcp(api_key: str, v2p_key: str | None = None, scope: str = "user", dev: bool = False) -> bool:
    # ...
    vibe2prod_url = "https://staging.vibe2prod.verstandai.site" if dev else "https://api.vibe2prod.net"
    cmd.extend(["-e", f"VIBE2PROD_URL={vibe2prod_url}"])
    # ...
```

### Files Affected (forge-engine)

- `forge/cli.py` — add `--dev` flag to `setup` command, pass through
- `forge/setup_wizard.py` — accept `dev` param in both interactive and headless, change TUI visuals, pass to `register_mcp()`

## 2. forge_config MCP Tool

New MCP tool so skills/Claude can read the current configuration:

```python
@mcp.tool()
def forge_config() -> dict:
    """Get current FORGE configuration — URL, sharing preferences, version."""
    return {
        "vibe2prod_url": _VIBE2PROD_URL,
        "share_forgeignore": os.environ.get("VIBE2PROD_DATA_SHARING", "false") == "true",
        "version": _get_version(),
    }
```

### Files Affected (forge-engine)

- `forge/mcp_server.py` — add `forge_config` tool

## 3. Forgeignore Training Data Sync

### Pipeline Integration

After scan completes in both `mcp_server.py` (MCP path) and `standalone.py` (CLI path), sync forgeignore entries to the training endpoint. Fire-and-forget, same pattern as existing telemetry.

```python
async def _sync_forgeignore(repo_path: str) -> None:
    """Share anonymized .forgeignore entries to training endpoint.

    Non-blocking, non-fatal. Only runs if share_forgeignore consent is set.
    """
    if not _should_share_forgeignore():
        return

    forgeignore = ForgeIgnore.load(repo_path)
    if not forgeignore.rules:
        return

    entries = []
    for rule in forgeignore.rules:
        entries.append({
            "pattern": rule.pattern or rule.check_id or "",
            "category": rule.category or "",
            "reason": rule.reason,
            "type": rule.type or "false_positive",
            "check_id": rule.check_id,
            "path": rule.path,
            "max_severity": rule.max_severity,
        })

    repo_hash = _hash_repo_identity(repo_path)

    await _post_training_data({
        "repo_hash": repo_hash,
        "entries": entries,
        "scan_mode": "full",  # or "deterministic_only"
        "version": _get_version(),
    })
```

**Consent check:** Reads `share_forgeignore` from `~/.vibe2prod/config.json`. Also respects `VIBE2PROD_DATA_SHARING` env var for MCP server context.

### /forgeignore Skill Update

Skill instructions updated to:
- Call `forge_config()` MCP tool to get URL and sharing consent
- Use the returned URL for any manual POST (instead of hardcoded URL)
- Note that automatic sync happens after scans — manual POST only needed for standalone `/forgeignore` usage outside a scan cycle

### Files Affected (forge-engine)

- `forge/mcp_server.py` — add `_sync_forgeignore()`, call after `forge_scan` completes
- `forge/standalone.py` — add forgeignore sync after scan completes
- `forge/skills/forgeignore/SKILL.md` — update to use `forge_config()` for URL

## 4. Backend Endpoint

### Route

`POST /api/training/forgeignore` in vibe2prod backend.

New file: `backend/api/routes/training.py`

```python
@router.post("/training/forgeignore")
async def ingest_forgeignore(request: Request):
    """Ingest anonymized .forgeignore entries for training data."""
    body = await request.json()

    # Optional auth — anonymous submissions allowed
    api_key = request.headers.get("X-API-Key")
    user_id = None
    if api_key:
        user_id = await resolve_user_id(api_key)

    repo_hash = body["repo_hash"]
    entries = body["entries"]
    scan_mode = body.get("scan_mode", "full")
    version = body.get("version", "")

    accepted = 0
    duplicates = 0

    for entry in entries:
        fingerprint = sha256(f"{entry['pattern']}:{entry['category']}:{entry['type']}".encode()).hexdigest()

        inserted = await training_repo.insert_forgeignore_entry(
            fingerprint=fingerprint,
            user_id=user_id,
            repo_hash=repo_hash,
            pattern=entry["pattern"],
            category=entry["category"],
            reason=entry["reason"],
            type=entry.get("type", "false_positive"),
            check_id=entry.get("check_id"),
            path_glob=entry.get("path"),
            max_severity=entry.get("max_severity"),
            scan_mode=scan_mode,
            version=version,
        )

        if inserted:
            accepted += 1
        else:
            duplicates += 1

    return {"accepted": accepted, "duplicates": duplicates}
```

### Repository

New file: `backend/services/repositories/training_repository.py`

```python
async def insert_forgeignore_entry(self, **kwargs) -> bool:
    """Insert a forgeignore entry. Returns False if duplicate (fingerprint conflict)."""
    # INSERT ... ON CONFLICT (fingerprint) DO NOTHING
    # Returns True if inserted, False if duplicate
```

### Register Route

In `backend/main.py`, add:
```python
from api.routes.training import router as training_router
app.include_router(training_router, prefix="/api")
```

### Files Affected (vibe2prod backend)

- `backend/api/routes/training.py` — new endpoint
- `backend/services/repositories/training_repository.py` — new repository
- `backend/main.py` — register training router

## 5. Supabase Migration

### Table: `forgeignore_entries`

```sql
CREATE TABLE public.forgeignore_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fingerprint TEXT NOT NULL UNIQUE,
  user_id TEXT,
  repo_hash TEXT NOT NULL,
  pattern TEXT NOT NULL,
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'false_positive',
  check_id TEXT,
  path_glob TEXT,
  max_severity TEXT,
  scan_mode TEXT DEFAULT 'full',
  version TEXT DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS: service role full access
ALTER TABLE public.forgeignore_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on forgeignore_entries"
  ON public.forgeignore_entries
  FOR ALL
  USING (true)
  WITH CHECK (true);

-- Indexes
CREATE INDEX idx_forgeignore_user_id ON public.forgeignore_entries (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX idx_forgeignore_category ON public.forgeignore_entries (category);
CREATE INDEX idx_forgeignore_type ON public.forgeignore_entries (type);
```

**Dedup:** `fingerprint` is `SHA-256(pattern + ":" + category + ":" + type)`. `UNIQUE` constraint on `fingerprint`. Inserts use `ON CONFLICT (fingerprint) DO NOTHING`.

**Column breakdown:**
- `pattern`, `category`, `reason` — always populated (NOT NULL), the core training data
- `type` — suppression type (false_positive, not_applicable, accepted_risk, etc.)
- `user_id` — optional linkage when API key provided
- `check_id`, `path_glob`, `max_severity` — optional enrichments
- `repo_hash` — anonymized repo identity
- `scan_mode`, `version` — context metadata

### Files Affected (vibe2prod)

- `supabase/migrations/YYYYMMDDHHMMSS_forgeignore_training.sql` — new migration

## Summary of All Files

### forge-engine
- `forge/cli.py` — `--dev` flag on setup
- `forge/setup_wizard.py` — dev mode TUI, URL env var in MCP registration
- `forge/mcp_server.py` — `forge_config` tool, `_sync_forgeignore()` after scan
- `forge/standalone.py` — forgeignore sync after CLI scan
- `forge/skills/forgeignore/SKILL.md` — use `forge_config()` for URL

### vibe2prod backend
- `backend/api/routes/training.py` — new POST endpoint
- `backend/services/repositories/training_repository.py` — new repository
- `backend/main.py` — register training router
- `supabase/migrations/20260326120000_forgeignore_training.sql` — new table
