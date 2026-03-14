# Building FORGE: A Production CLI for AI Agent Systems

**The optimal architecture for FORGE is a Python CLI built with Typer + Rich, exposed to Claude Code through a thin MCP server wrapper using FastMCP over stdio transport.** This approach keeps the CLI tightly coupled with FORGE's existing 12-agent Python system and Pydantic schemas, avoids a language boundary, and provides first-class Claude Code integration with zero compromise on distribution or user experience. The critical design insight from surveying every major AI coding agent — Aider, OpenHands, Claude Code, Cline, Continue — is that the winning pattern is **SDK-first architecture** with the CLI as one of multiple thin interface layers, not a monolithic command-line application.

---

## Typer is the right framework for a Pydantic-native system

FORGE's architecture — 12 Python agents, Pydantic schemas, AgentField infrastructure — dictates the framework choice more than any feature comparison. **Typer**, built by the FastAPI creator on top of Click, shares Pydantic's philosophy of using Python type hints as the source of truth. A Typer command with `Annotated[str, typer.Option()]` parameters mirrors a Pydantic model definition, enabling shared validation logic between CLI inputs and agent schemas.

The concrete advantages over alternatives are decisive. Typer auto-generates CLI help, shell completions, and argument validation from function signatures — eliminating the decorator boilerplate of Click while inheriting Click's entire ecosystem (including `CliRunner` for testing). **Recent versions support `async def` commands natively**, critical for FORGE's long-running agent orchestration. Combined with Rich (which Typer bundles via `typer[all]`), the framework provides spinners, progress bars, live-updating tables, and styled markdown output — all necessary for reporting multi-agent progress.

The comparison with Node.js frameworks reveals a strong case for staying in Python. While **oclif** (Salesforce's framework powering the Heroku CLI) offers the best plugin architecture and distribution tooling in any ecosystem — runtime-installable npm plugins, standalone platform installers, auto-update channels — it would force FORGE to bridge between TypeScript CLI commands and Python agent execution via subprocess calls or gRPC. This language boundary introduces serialization overhead, error-handling complexity, and a dual-language codebase. **Ink** (React for the terminal) powers Claude Code, Codex CLI, and Gemini CLI with beautiful reactive UIs, but the same argument applies: the rendering benefits don't justify maintaining two language ecosystems when Rich provides comparable terminal output from Python.

The framework landscape as of March 2026:

| Framework | Stars | Best For | Async | Plugin System | Distribution |
|-----------|-------|----------|-------|---------------|-------------|
| **Typer** | ~16k | Modern Python CLI with type hints | ✅ Native | Via Click ecosystem | pip/pipx/brew |
| Click | 17.3k | Maximum Python ecosystem compatibility | ❌ (wrapper) | ✅ setuptools entry points | pip/pipx/brew |
| oclif | 9.4k | Enterprise Node CLI with plugins | ✅ Native | ★★★★★ Runtime npm plugins | npm/brew/standalone |
| Ink | 26k | Rich interactive terminal UIs | ✅ React hooks | Via Pastel framework | npm |
| Commander.js | 28k | Lightweight Node CLI | ✅ | ❌ | npm |

---

## Two paths into Claude Code, and you should use both

Claude Code discovers external capabilities through two mechanisms, and FORGE should leverage both simultaneously. The **primary path** is wrapping FORGE as an MCP (Model Context Protocol) server, giving Claude Code structured tool definitions with typed parameters, progress reporting, and automatic discoverability. The **secondary path** is direct CLI invocation through Claude Code's built-in Bash tool, which requires zero setup but lacks structured output.

**MCP integration is straightforward with FastMCP.** The official Python SDK (`pip install mcp`) provides a decorator-based API that mirrors Typer's simplicity. A minimal MCP wrapper for FORGE looks like this:

```python
from mcp.server.fastmcp import FastMCP
import subprocess, json

mcp = FastMCP("forge", instructions="Use FORGE for codebase analysis, architecture review, and remediation reports.")

@mcp.tool()
def analyze_codebase(path: str, depth: str = "standard") -> str:
    """Run FORGE analysis on a codebase directory. Returns structured report."""
    result = subprocess.run(
        ["forge", "analyze", "--path", path, "--depth", depth, "--json"],
        capture_output=True, text=True
    )
    return result.stdout

@mcp.tool()
async def generate_report(path: str, report_type: str, ctx: Context) -> str:
    """Generate a FORGE report with progress tracking."""
    await ctx.report_progress(progress=0, total=100)
    # ... orchestrate agents with progress updates
    return json.dumps({"status": "complete", "report_path": output_path})
```

Registration with Claude Code is a single command: `claude mcp add forge -- uv run /path/to/forge_mcp.py`. The server communicates over **stdio transport** (JSON-RPC 2.0 over stdin/stdout), meaning it runs as a local subprocess with no network exposure. MCP tools appear in Claude Code with the prefix `mcp__forge__analyze_codebase`, and can be pre-approved in `.claude/settings.json` for frictionless use:

```json
{
  "permissions": {
    "allow": ["mcp__forge__*"]
  }
}
```

**The recommended architecture is CLI-first, MCP-wrapper-second.** Build all core logic in the CLI (testable independently), then create a thin ~200-line MCP server that calls the CLI via `subprocess.run()`. This is the pattern used by the `any-cli-mcp-server` project and recommended by the MCP documentation. The wrapper handles serialization, progress reporting, and error translation while the CLI remains usable standalone.

For team adoption, commit an `.mcp.json` file to the project root and add usage instructions to `CLAUDE.md` — both are automatically loaded by Claude Code at session start, making FORGE discoverable without manual configuration.

---

## Lessons from every major AI coding agent

A systematic survey of Aider, OpenHands, SWE-Agent, Cline, Continue, and Cursor's CLI reveals five architectural patterns that FORGE should adopt.

**Aider's PageRank-based repo mapping is the gold standard for codebase understanding.** Aider uses tree-sitter to parse every file into an AST, builds a NetworkX directed graph of symbol dependencies across files, runs PageRank to rank symbols by importance, then uses binary search to fit the highest-ranked definitions within a configurable token budget. This produces a concise "repo map" — class/function signatures with call patterns — that gives LLMs structural awareness of an entire codebase without consuming excessive context. FORGE should replicate this pattern rather than sending raw file contents to its agents.

**OpenHands V1's SDK-first architecture is the most production-ready pattern.** Their January 2025 rewrite separated the agent core into four composable Python packages (SDK, Tools, Workspace, Server), with the CLI, Web UI, and GitHub App all consuming the same SDK library. The critical insight: **the CLI should not contain business logic** — it should be a thin interface layer over a well-structured SDK that FORGE's 12 agents also use directly. This enables future interfaces (VS Code extension, web dashboard, GitHub Action) without duplicating orchestration code.

**Dual-mode operation — interactive and headless — is becoming mandatory.** Continue's `cn` CLI and Cline CLI 2.0 both support interactive REPL sessions for development and headless JSON-output mode for CI/CD pipelines. FORGE should implement `forge analyze` (interactive with Rich output) and `forge analyze --json --quiet` (machine-readable for automation). SWE-Agent's trajectory files — structured JSON logs of every agent step — provide an excellent template for FORGE's report format, enabling both human review and programmatic analysis.

**The simplicity of mini-swe-agent is a powerful counterpoint.** Princeton's mini-swe-agent achieves **>74% on SWE-bench Verified with ~100 lines of Python** using only bash and `subprocess.run()` — no custom tools, no complex scaffolding. The lesson for FORGE: avoid over-engineering the CLI layer. Let the 12 agents do the heavy lifting; the CLI's job is orchestration, progress display, and report serialization.

**Permission and safety patterns are converging across tools.** Continue uses `permissions.yaml`, Cursor uses `cli-config.json`, Cline uses per-action approval loops. FORGE should implement a similar system — a `.forge/permissions.yaml` that controls which agents can modify files, execute commands, or access external services. Since FORGE runs locally without sending code externally, this is primarily about preventing accidental destructive operations.

---

## Distribution strategy: pipx first, then layer additional channels

For a Python-based CLI, the distribution priority should follow this sequence, optimized for the target audience of developers using Claude Code:

**Primary: PyPI via pipx/uv.** Structure `pyproject.toml` with Hatch as the build backend and `[project.scripts]` for the CLI entry point. Use optional dependencies to keep the core install lightweight:

```toml
[project]
name = "forge-cli"
dependencies = ["typer[all]>=0.9", "pydantic>=2.0", "httpx>=0.25"]

[project.scripts]
forge = "forge.cli:app"

[project.optional-dependencies]
local-models = ["torch>=2.0", "transformers>=4.30"]
all = ["forge-cli[local-models]"]
```

Users install with `pipx install forge-cli` (isolated environment, globally available command) or `uv tool install forge-cli` (faster alternative). **Lazy imports are essential** — defer `torch` and `transformers` imports to the functions that use them, with helpful error messages directing users to `pip install forge-cli[local-models]`.

**Secondary: Homebrew tap for macOS users.** Create a `homebrew-forge` repository with a formula auto-generated by `homebrew-pypi-poet`. Automate formula updates via GitHub Actions triggered on new PyPI releases. This is the pattern used by Simon Willison's `llm` tool and `datasette`.

**Tertiary: GitHub Releases with install script.** Build platform-specific binaries with PyInstaller in CI (Linux amd64, macOS arm64, Windows amd64), attach to GitHub releases, and provide a `curl -fsSL https://forge.dev/install | bash` script that auto-detects platform. This serves users without Python installed.

**For Claude Code specifically**, the most important "distribution" is an npm-compatible install or a documented `claude mcp add` command. Consider publishing a lightweight npm package (`@forge/mcp-server`) that contains only the MCP wrapper and spawns the Python CLI, following the pattern used by `@sentry/cli` (Rust binary distributed via npm with platform-specific optional dependencies). This lets Claude Code users run `npx @forge/mcp-server` without managing Python environments.

---

## Recommended architecture for FORGE CLI

Based on all findings, the recommended stack and structure:

```
forge/
├── pyproject.toml              # Hatch build, [project.scripts] forge = "forge.cli:app"
├── src/forge/
│   ├── cli/
│   │   ├── __init__.py         # Typer app definition
│   │   ├── commands/
│   │   │   ├── analyze.py      # forge analyze <path>
│   │   │   ├── report.py       # forge report generate/view
│   │   │   └── config.py       # forge config set/get
│   │   └── display.py          # Rich output formatting
│   ├── sdk/                    # Core SDK (agents consume this)
│   │   ├── agents/             # 12 FORGE agents
│   │   ├── schemas/            # Pydantic models (shared with CLI)
│   │   └── orchestrator.py     # Agent orchestration
│   ├── scanner/                # Codebase scanning (tree-sitter + repo map)
│   └── reports/                # Report generation (Markdown/JSON to .forge/)
├── mcp_server/
│   └── server.py               # FastMCP wrapper (~200 lines)
├── .forge/                     # Local output directory
│   ├── reports/
│   └── config.yaml
└── CLAUDE.md                   # Instructions for Claude Code integration
```

**Language: Python.** The 12-agent system, Pydantic schemas, and AgentField infrastructure are all Python. Crossing a language boundary for the CLI layer adds complexity without proportional benefit.

**CLI Framework: Typer + Rich.** Type-hint-based commands that mirror Pydantic model patterns. Rich provides spinners, progress bars, live tables, and markdown rendering for agent status display.

**MCP Integration: FastMCP over stdio.** A thin Python wrapper that calls CLI commands via subprocess, exposing `analyze_codebase`, `generate_report`, `view_report`, and `configure` as MCP tools. Register with `claude mcp add forge -- uv run mcp_server/server.py`.

**Codebase Scanning: Tree-sitter + PageRank repo maps** (Aider's pattern). This gives FORGE's agents a token-efficient view of repository structure without sending full file contents.

**Report Output: Local `.forge/` directory** with timestamped Markdown reports and a companion JSON manifest. Reports are viewable via `forge report view` (Rich terminal rendering) or as plain files.

**Distribution: pipx → Homebrew → GitHub Releases → npm MCP wrapper**, in that priority order.

This architecture keeps FORGE's Python ecosystem unified, provides a polished CLI experience, integrates natively with Claude Code through MCP, and follows the SDK-first pattern proven by OpenHands — ensuring the same core can power future interfaces beyond the command line.
