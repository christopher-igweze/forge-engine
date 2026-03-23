# FORGE CLI Installer & Setup Wizard

**Date:** 2026-03-20
**Status:** Draft
**Author:** Christopher + Claude

## Problem

Installing FORGE today requires manual steps: `pip install vibe2prod`, setting env vars, and manually registering the MCP server with Claude Code. There is no guided setup experience for humans, and no standardized headless path for AI coding agents to install and configure FORGE programmatically.

## Goals

1. Provide a polished TUI setup wizard for human users (Rich-powered)
2. Provide a fully headless setup path for AI coding agents (zero prompts)
3. Distribute via all major package managers (pip, npm, brew, curl)
4. Auto-detect and integrate with Claude Code (MCP server + /forge skill)
5. Store configuration and API keys in `~/.vibe2prod/config.json`

## Non-Goals

- Standalone binary distribution (Nuitka) — planned separately
- Supporting non-Claude AI IDE integrations in v1 (Cursor, Copilot, etc.)
- Account creation flow — users bring their own API keys

---

## Design

### 1. Setup Command & Dual Modes

New Typer subcommand: `vibe2prod setup`

**Interactive mode (TUI):**
```bash
vibe2prod setup
```
- Auto-detected via TTY check (`sys.stdin.isatty()`)
- Rich-powered wizard with panels, spinners, styled prompts
- Step-by-step flow with validation at each step

**Headless mode (AI agents):**
```bash
vibe2prod setup --api-key sk-or-... --v2p-key v2p_... --no-interactive
```
- Zero prompts, exits 0 on success, non-zero on failure
- `--json` flag for structured output

**First-run behavior:**
- When `vibe2prod scan` is run without config, prints:
  ```
  No configuration found. Run 'vibe2prod setup' to get started.
  ```
- Does NOT auto-launch the wizard — keeps it explicit
- `vibe2prod setup --reset` re-runs wizard with existing values pre-populated (user can change individual fields without re-entering everything)

### 2. TUI Wizard Flow

Four-step interactive wizard:

**Step 1/4 — OpenRouter API Key (required)**
- Prompt user to paste their OpenRouter API key
- Validate format: must start with `sk-or-`
- Optionally test connectivity with a lightweight API call
- Mask display after entry (show `sk-or-****`)

**Step 2/4 — Vibe2Prod Dashboard (optional)**
- Ask if user wants dashboard sync for scan history/analytics
- If yes: collect `vibe2prod_api_key`, validate format (`v2p_` prefix)
- If no: skip, set `data_sharing: false`

**Step 3/4 — Claude Code Integration (auto-detected)**
- Detect Claude Code: check `which claude` and/or `~/.claude/` directory
- If found:
  - Register MCP server to **user-level scope** (global, not project): `claude mcp add --scope user forge -e OPENROUTER_API_KEY=<key> -- forge-mcp`
  - Install `/forge` skill: copy `SKILL.md` to `~/.claude/commands/forge.md`
  - Show confirmation for each
  - On MCP registration failure: show warning + manual command for user to run themselves
  - On skill install failure: show warning + path to copy manually
- If NOT found:
  - Skip with message: "Claude Code not detected. You can use FORGE via CLI directly."
  - Show manual usage: `vibe2prod scan ./your-project`

**Step 4/4 — Summary**
- Display what was configured (keys masked)
- Show next steps: `vibe2prod scan .` to start
- If Claude Code integrated: mention `/forge` skill is available

### 3. Config Storage & Key Management

**Important:** The `~/.vibe2prod/config.json` file is a **CLI-level** config, separate from `ForgeConfig` (which uses `extra="forbid"` and would crash on unknown fields). Only specific fields (like `quality_gate_profile`, `models`) are forwarded to `ForgeConfig` at runtime. API keys and setup metadata stay at the CLI config level only.

**File:** `~/.vibe2prod/config.json`

```json
{
  "openrouter_api_key": "sk-or-...",
  "auth": {
    "api_key": "v2p_...",
    "url": "https://api.vibe2prod.net"
  },
  "data_sharing": true,
  "quality_gate_profile": "forge-way",
  "setup_completed": true,
  "claude_code_integrated": true
}
```

Note: The `auth` key structure aligns with the existing `auth login/logout` command pattern in `cli.py`. The `vibe2prod setup` wizard writes to `auth.api_key`; the `auth login` command continues to work as before — both write to the same location.

**Key behaviors:**
- Environment variables take precedence over config file values (CI/CD override)
- Existing `_check_api_key()` in `cli.py` is extended: after checking env var, falls back to `config["openrouter_api_key"]` from the config file
- `vibe2prod config set/get` continues to work for individual fields
- File permissions: `0600` (owner-only read/write) — `_save_config()` must call `os.chmod(path, 0o600)` after writing
- Atomic writes: use `tempfile.NamedTemporaryFile` + `os.rename` to prevent corruption from concurrent access

**Resolution order for API keys:**
1. Environment variable (highest priority)
2. `~/.vibe2prod/config.json`
3. Not set → error with setup instructions

**Existing user migration:**
- If `OPENROUTER_API_KEY` env var is already set: wizard pre-populates and asks to confirm
- If `.mcp.json` already has forge registered: wizard detects and skips MCP registration step
- Setup is idempotent — running it twice doesn't break anything

### 4. Install Wrappers

All wrappers follow: ensure Python 3.10+ → pip install → run setup.

**curl (primary):**
```bash
curl -fsSL https://get.vibe2prod.com | bash
```
Shell script that:
- Checks for Python 3.10+, suggests install if missing
- Runs `pip install vibe2prod` (or `pipx install vibe2prod`)
- Launches `vibe2prod setup` (interactive if TTY, headless if piped)

**npm:**
```bash
npm i -g vibe2prod
```
- Thin JS wrapper package on npmjs
- Post-install script: checks Python, pip installs, runs `vibe2prod setup`
- `npx vibe2prod` also works for one-off use
- Note: cross-platform Python detection (Windows paths, `pip` vs `pip3`) is non-trivial. v1 targets macOS/Linux only; Windows support deferred.

**brew:**
```bash
brew install vibe2prod
```
- Homebrew formula with Python dependency
- Installs via pip into brew's Python environment
- Post-install caveat: "Run `vibe2prod setup` to configure"

**All wrappers:**
- Pass `--no-interactive` if stdin is not a TTY
- Fail gracefully with clear message if Python 3.10+ not found
- Exit codes: 0 = success, 1 = missing dependency, 2 = setup failed

---

## Technical Implementation

### New Files
- `forge/setup_wizard.py` — Setup wizard logic (TUI + headless)
- `scripts/install.sh` — curl installer script
- `npm/` — npm wrapper package (package.json + bin script)
- `Formula/vibe2prod.rb` — Homebrew formula (or tap repo)

### Modified Files
- `forge/cli.py` — Add `setup` subcommand, extend `_check_api_key()` to load from config file, add first-run config check
- `forge/cli.py` `_save_config()` — Add `os.chmod(path, 0o600)` and atomic write via tempfile
- `pyproject.toml` — Add `rich>=13.0` as explicit dependency (Typer optionally depends on it but does not guarantee it)
- `pyproject.toml` `[tool.setuptools.package-data]` — Ensure `skills/forge/SKILL.md` is included in distribution

### Dependencies
- `rich>=13.0` — Must be explicit in `pyproject.toml`. Used for: panels, prompts, spinners, syntax highlighting, styled text

### Claude Code Detection Logic
```python
import shutil
from pathlib import Path

def detect_claude_code() -> bool:
    """Check if Claude Code CLI is installed."""
    return (
        shutil.which("claude") is not None
        or Path.home().joinpath(".claude").is_dir()
    )
```

### MCP Registration
```python
import subprocess

def register_mcp(api_key: str, v2p_key: str | None = None) -> bool:
    """Register FORGE MCP server with Claude Code (user-level scope).

    Uses --scope user to write to ~/.claude/ (global), NOT project-level .mcp.json.
    This prevents API keys from landing in version-controlled files.
    """
    cmd = [
        "claude", "mcp", "add", "--scope", "user",
        "forge",
        "-e", f"OPENROUTER_API_KEY={api_key}",
    ]
    if v2p_key:
        cmd.extend(["-e", f"VIBE2PROD_API_KEY={v2p_key}"])
    cmd.extend(["--", "forge-mcp"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0
```

### Skill Installation
```python
import shutil
from pathlib import Path

def install_skill() -> bool:
    """Copy /forge skill to Claude Code commands directory."""
    skill_src = Path(__file__).parent / "skills" / "forge" / "SKILL.md"
    if not skill_src.exists():
        return False
    skill_dst = Path.home() / ".claude" / "commands" / "forge.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_src, skill_dst)
    return skill_dst.exists()
```

---

## Headless Usage Examples

**AI agent installing FORGE:**
```bash
pip install vibe2prod
vibe2prod setup --api-key $OPENROUTER_API_KEY --no-interactive
vibe2prod scan ./target-repo
```

**AI agent with dashboard sync:**
```bash
pip install vibe2prod
vibe2prod setup --api-key $OPENROUTER_API_KEY --v2p-key $V2P_KEY --no-interactive
```

**AI agent with JSON output:**
```bash
vibe2prod setup --api-key $KEY --no-interactive --json
# {"status": "ok", "config_path": "~/.vibe2prod/config.json", "claude_code": false}
```

**Reconfigure:**
```bash
vibe2prod setup --reset
```

---

## Test Plan

### Unit Tests
- `detect_claude_code()` — mock `shutil.which` and `Path.home()` for both found/not-found
- Config loading with precedence: env var > config file > missing
- API key validation: valid `sk-or-` prefix, invalid formats, empty string
- `_save_config()` file permissions are `0600`
- Atomic write: config file not corrupted on simulated failure
- `--reset` pre-populates existing values

### Integration Tests
- Headless mode: `vibe2prod setup --api-key <key> --no-interactive` exits 0, writes config
- Headless mode with `--json`: outputs valid JSON
- First-run detection: `vibe2prod scan` without config prints setup message
- Idempotent: running setup twice doesn't duplicate MCP registration

### Manual Tests
- TUI wizard renders correctly on macOS Terminal, iTerm2, VS Code terminal
- Claude Code MCP registration works end-to-end
- Skill file appears in `~/.claude/commands/` after setup

---

## Future Considerations

- **Standalone binary (Nuitka):** When compiled binaries ship, curl/npm/brew wrappers install the binary directly instead of pip. Setup wizard still works the same.
- **Other AI IDE integrations:** Detect Cursor, Copilot, etc. and register appropriate extensions.
- **Account creation flow:** `vibe2prod setup` could offer to create a vibe2prod account inline.
- **Config migration:** If config schema changes, setup detects old format and migrates.
- **Windows npm support:** Cross-platform Python detection for the npm wrapper.
