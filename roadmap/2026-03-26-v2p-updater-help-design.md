# v2p Updater & Help System — Design Spec

> Feature: Self-update pipeline, auto-check, and rich help command for vibe2prod CLI
> Date: 2026-03-26
> Status: Draft

## Problem

vibe2prod has no update mechanism. Users must manually `pip install --upgrade vibe2prod` and have no way to know if their skills, hooks, MCP registration, or config are stale. There's also no help system beyond Typer's built-in `--help`, which doesn't show examples, config locations, or grouped commands.

Additionally, there is a version mismatch (`forge/__init__.py` says `0.3.1`, `pyproject.toml` says `1.1.0`) and deprecated flags (`--max-cost`, `--max-time`) that need cleanup.

## Solution

Three additions to the CLI:

1. **`vibe2prod update`** — smart pipeline that upgrades the package and syncs all components (skills, hooks, MCP, config), only touching what changed.
2. **Auto-check** — non-blocking PyPI version check on every command with a one-liner notification.
3. **`vibe2prod help`** — rich grouped command listing with examples, config locations, and per-command details.

### Design Principles

- **Fast** — update shows progress bar and summary, not a wizard. Auto-check never blocks the main command.
- **Smart** — only syncs what changed. Uses a version manifest to diff installed vs packaged components.
- **Discoverable** — help command surfaces config locations (API keys, data sharing) alongside flags and examples.

## `vibe2prod update`

### Usage

```
vibe2prod update          # check + upgrade + sync everything
vibe2prod update --check  # dry run — show what would change
vibe2prod update --force  # re-sync everything regardless of version
```

### Pipeline

Runs 5 steps in order. Each step is independent — if one fails, the rest still run.

```
Step 1: Package    Updated vibe2prod 1.1.0 → 1.2.0
Step 2: Skills     Updated forge skill (new audit workflow)
                   forgeignore skill (already up to date)
Step 3: Hooks      Added codebase-guide post-commit hook
                   test-audit hook (already up to date)
Step 4: MCP        MCP server registration (already up to date)
Step 5: Config     Migrated config: added test_audit section

Done. 3 updated, 3 unchanged.
```

### Step Details

| Step | What It Does | How It Detects Changes |
|------|-------------|----------------------|
| **Package** | `pip install --upgrade vibe2prod` | Compares installed version (`importlib.metadata`) with PyPI latest |
| **Skills** | Copies updated SKILL.md files to `~/.claude/commands/` | Compares SHA-256 hash of installed file vs hash in manifest |
| **Hooks** | Adds/updates Claude Code hook entries in `.claude/settings.json` | Compares expected hook config from manifest with current entries |
| **MCP** | Re-registers MCP server via `claude mcp add` | Compares expected MCP args from manifest with `claude mcp list` output |
| **Config** | Migrates `~/.vibe2prod/config.json` to new schema | Compares `config_schema_version` in config file vs manifest |

### Migration Severity

- **Minor** (auto-migrate): New fields with defaults, renamed fields. Migrated silently, shown in summary output.
- **Major** (prompt): Removed features, changed behavior, breaking config changes. Shows what will change, asks "Proceed? [Y/n]" before applying.

Severity is defined per version bump in the manifest's `migrations` section.

### Output Style

Simple Rich output — spinner per step while running, one-line result per step. Summary count at the end. Not a wizard, not interactive (except major migration prompts).

### Headless Mode

`vibe2prod update --json` returns structured JSON for AI agents:

```json
{
  "previous_version": "1.1.0",
  "new_version": "1.2.0",
  "steps": [
    { "name": "package", "status": "updated", "detail": "1.1.0 → 1.2.0" },
    { "name": "skills", "status": "updated", "detail": "forge skill updated" },
    { "name": "hooks", "status": "unchanged" },
    { "name": "mcp", "status": "unchanged" },
    { "name": "config", "status": "migrated", "detail": "schema v1 → v2" }
  ]
}
```

### Error Handling

| Scenario | Behavior |
|----------|----------|
| No internet / PyPI unreachable | Skip package step, continue with local sync (skills, hooks, MCP, config may still need syncing) |
| pip upgrade fails | Report error, continue with remaining steps |
| Skill file write fails (permissions) | Report error with suggested fix (`chmod`), continue |
| `.claude/settings.json` not found | Skip hooks step, note that Claude Code integration is not set up |
| `claude` CLI not found | Skip MCP and hooks steps, note that Claude Code is not installed |
| Config migration fails | Back up current config to `~/.vibe2prod/config.json.bak`, report error |

## Auto-Check

### How It Works

Every time any `vibe2prod` command runs, a non-blocking background version check runs against PyPI.

1. Before executing the command, check if the version cache is fresh (within TTL)
2. If stale or missing: spawn an async check to `https://pypi.org/pypi/vibe2prod/json` (non-blocking — does not delay the command)
3. Cache the result at `~/.vibe2prod/.version_cache.json`
4. If a newer version exists, print a one-liner **after** the command output:

```
Update available: 1.1.0 → 1.2.0 — run `vibe2prod update` to upgrade
```

### Cache File

```json
{
  "current": "1.1.0",
  "latest": "1.2.0",
  "checked_at": "2026-03-26T10:00:00Z",
  "ttl_hours": 24
}
```

### Rules

- **Never blocks** the main command. If the check is slow or fails, silently skip.
- **Prints once per session** — not on every command if piped together.
- **Respects config**: `vibe2prod config set auto_update_check false` disables it.
- **No network call** if cache is fresh (within TTL).
- **No check during `vibe2prod update`** — it already checks PyPI as part of Step 1.

### Implementation

Add a `check_for_update()` function called in the Typer app callback (runs before any command). Uses `threading.Thread(daemon=True)` to avoid blocking. Writes cache atomically (same pattern as `config_io.py`).

## `vibe2prod help`

### Top-Level: `vibe2prod help`

Rich grouped command listing with examples:

```
vibe2prod — AI-powered codebase auditing engine

SCANNING
  scan <path>          Scan a codebase for security, quality & architecture issues
  status <path>        Check progress of a running scan
  report <path>        View the last scan report

SETUP & CONFIG
  setup                Interactive setup wizard (API keys, Claude integration)
  config set <k> <v>   Set a config value
  config get <k>       Get a config value

MAINTENANCE
  update               Check for updates and upgrade all components
  update --check       See what would change without applying

AUTHENTICATION
  auth login           Log in to vibe2prod platform
  auth logout          Log out
  auth status          Check auth status

EXAMPLES
  vibe2prod scan ./my-app                     # full audit
  vibe2prod scan ./my-app --gate strict       # strict quality gate
  vibe2prod report ./my-app --format json     # JSON report
  vibe2prod update                            # upgrade everything

Run `vibe2prod help <command>` for details on a specific command.
Version: 1.2.0
```

### Command-Level: `vibe2prod help <command>`

Detailed help with flags, config locations, and examples:

```
$ vibe2prod help scan

vibe2prod scan <path>

  Scan a codebase for security, quality, and architecture issues.
  Runs: Opengrep SAST → Codebase Analyst → Security Auditor
  → Fix Strategist → Evaluation & Scoring → Quality Gate

OPTIONS
  --api-key TEXT       OpenRouter API key (default: env OPENROUTER_API_KEY)
  --gate TEXT          Quality gate profile: forge-way, strict, startup
  --aivss              Enable AI vulnerability scoring
  --json / -j          Output as JSON
  --verbose / -v       Verbose logging

CONFIGURATION
  API key:       Set via --api-key, OPENROUTER_API_KEY env var, or
                 `vibe2prod config set openrouter_api_key <key>`
  Data sharing:  Configure via `vibe2prod setup` or
                 `vibe2prod config set data_sharing true/false`
  Quality gate:  Default profile set via
                 `vibe2prod config set quality_gate_profile <profile>`

EXAMPLES
  vibe2prod scan ./my-app
  vibe2prod scan ./my-app --gate strict
  vibe2prod scan ./my-app --json | jq '.findings'
```

### Enhanced `--help`

The existing Typer `--help` output is improved:
- Each command gets a `rich_help_panel` grouping (Scanning, Setup & Config, Maintenance, Auth)
- Command descriptions updated to be more descriptive
- `--help` stays terse (flags only), `help` is the rich version with examples and config locations

### Implementation

`help` is a new Typer command. It reads command metadata from a registry (dict mapping command names to their help content). The help content is defined alongside each command, not in a separate file, so it stays in sync.

## Version Manifest

### Location

Packaged inside the v2p distribution at `forge/manifest.json`. Generated at build time from `pyproject.toml` and the skills/hooks/MCP definitions.

### Format

```json
{
  "version": "1.2.0",
  "skills": {
    "forge": {
      "hash": "abc123def456...",
      "file": "skills/forge/SKILL.md",
      "install_to": "~/.claude/commands/forge.md"
    },
    "forgeignore": {
      "hash": "789ghi012jkl...",
      "file": "skills/forgeignore/SKILL.md",
      "install_to": "~/.claude/commands/forgeignore.md"
    }
  },
  "hooks": {
    "codebase-guide": {
      "type": "PostToolUse",
      "tool": "Bash",
      "filter": "git commit",
      "description": "Update codebase guide on structural changes"
    },
    "test-audit": {
      "type": "PostToolUse",
      "tool": "Bash",
      "filter": "git commit",
      "description": "Analyze new test files for quality"
    }
  },
  "mcp": {
    "name": "forge",
    "command": "forge-mcp",
    "args": [],
    "env": ["OPENROUTER_API_KEY"],
    "scope": "user"
  },
  "config_schema_version": 2,
  "migrations": {
    "1→2": {
      "severity": "minor",
      "changes": [
        "Added codebase_guide section with defaults",
        "Added test_audit section with defaults"
      ]
    }
  },
  "deprecated_flags": {
    "--max-cost": { "since": "1.2.0", "message": "Cost budgeting removed — scans are now efficient by default" },
    "--max-time": { "since": "1.2.0", "message": "Time budgeting removed — scans are now efficient by default" }
  }
}
```

### How Each Step Uses the Manifest

- **Skills:** Hash the file at `install_to`. Compare with `hash` in manifest. Different = overwrite.
- **Hooks:** Read `.claude/settings.json`, look for matching hook entries. Missing or different config = add/update.
- **MCP:** Run `claude mcp list`, check for matching name. Missing or different args/env = re-register.
- **Config:** Read `config_schema_version` from `~/.vibe2prod/config.json` (default: 1 if missing). If less than manifest version, run migrations in order.
- **Deprecated flags:** When a deprecated flag is used in any command, print a warning with the deprecation message.

### Build-Time Generation

`forge/manifest.json` is generated during the build process:
- Version from `pyproject.toml`
- Skill hashes computed from SKILL.md files
- Hook/MCP definitions from a static registry in the codebase
- Config schema version incremented manually when schema changes

## Version Source of Truth

### Fix Current Mismatch

`pyproject.toml` becomes the single source of truth. `forge/__init__.py` uses `importlib.metadata.version("vibe2prod")` instead of a hardcoded string. This eliminates the `0.3.1` vs `1.1.0` discrepancy.

```python
# forge/__init__.py
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("vibe2prod")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
```

## Cleanup Notes

### Deprecated Flags to Remove

These flags exist on `vibe2prod scan` but are no longer needed:

| Flag | Location | Action |
|------|----------|--------|
| `--max-cost` | `forge/cli.py:246` | Deprecate in next release (print warning), remove in following release |
| `--max-time` | `forge/cli.py:247` | Same — deprecate then remove |

Both flags were added when the system used more LLM calls. The v3 architecture uses ~5 LLM calls per scan, making cost/time budgeting unnecessary.

### Deprecation Strategy

1. **Next release:** flags still work but print a deprecation warning via manifest's `deprecated_flags` section
2. **Following release:** flags removed from CLI, `vibe2prod help scan` no longer lists them

## Scope Boundaries

### In scope
- `vibe2prod update` command with 5-step pipeline
- `vibe2prod update --check` dry run mode
- `vibe2prod update --force` force re-sync
- `vibe2prod update --json` headless mode
- Auto-check on every command (non-blocking, cached 24h, configurable off)
- `vibe2prod help` with grouped commands and examples
- `vibe2prod help <command>` with flags, config locations, and examples
- Enhanced `--help` with Rich panels and grouping
- Version manifest (`forge/manifest.json`) for change detection
- Config migration system (minor = auto, major = prompt)
- Version source of truth fix (`importlib.metadata`)
- Deprecation of `--max-cost` and `--max-time` flags

### Out of scope
- Automatic updates (always requires user to run `vibe2prod update`)
- Rollback to previous version — future enhancement
- npm/brew distribution channels (separate spec exists)
- Plugin/extension system beyond skills — future enhancement
- Telemetry for update success/failure — future enhancement

## Graceful Degradation

| Scenario | Behavior |
|----------|----------|
| No internet | Skip package check, sync local components only |
| PyPI rate limited | Use cached version info, skip package step |
| Claude Code not installed | Skip hooks and MCP steps, note in output |
| `~/.claude/` doesn't exist | Skip hooks and MCP steps |
| `pip` not available | Try `pip3`, then `pipx`. If all fail, report error with manual install instructions |
| Config file corrupted | Back up to `.bak`, create fresh config with defaults, report what happened |
| Manifest missing (old install) | Force full sync — hash all skills, re-register everything |
