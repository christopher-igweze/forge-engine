"""Rich help content for vibe2prod commands.

Each command has: description, options table, config section, examples.
Content is defined here (alongside the commands it describes) so it stays in sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CommandHelp:
    """Rich help content for a single command."""
    name: str
    usage: str
    description: str
    options: list[tuple[str, str]] = field(default_factory=list)  # (flag, description)
    config: list[str] = field(default_factory=list)  # config hints
    examples: list[str] = field(default_factory=list)


# ── Help Registry ────────────────────────────────────────────────────

COMMANDS: dict[str, CommandHelp] = {
    "scan": CommandHelp(
        name="scan",
        usage="vibe2prod scan <path>",
        description=(
            "Scan a codebase for security, quality, and architecture issues.\n"
            "Runs: Opengrep SAST → Codebase Analyst → Security Auditor\n"
            "→ Fix Strategist → Evaluation & Scoring → Quality Gate"
        ),
        options=[
            ("--api-key TEXT", "OpenRouter API key (default: env OPENROUTER_API_KEY)"),
            ("--model TEXT", "Default model override (e.g. anthropic/claude-haiku-4.5)"),
            ("--gate TEXT", "Quality gate profile: forge-way, strict, startup"),
            ("--aivss", "Enable AI vulnerability scoring"),
            ("--json / -j", "Output as JSON"),
            ("--verbose / -v", "Verbose logging"),
        ],
        config=[
            "API key:       Set via --api-key, OPENROUTER_API_KEY env var, or",
            "               `vibe2prod config set openrouter_api_key <key>`",
            "Data sharing:  Configure via `vibe2prod setup` or",
            "               `vibe2prod config set share_forgeignore true/false`",
            "Quality gate:  Default profile set via",
            "               `vibe2prod config set quality_gate_profile <profile>`",
        ],
        examples=[
            "vibe2prod scan ./my-app",
            "vibe2prod scan ./my-app --gate strict",
            "vibe2prod scan ./my-app --json | jq '.findings'",
        ],
    ),
    "status": CommandHelp(
        name="status",
        usage="vibe2prod status <path>",
        description="Show real-time progress of a running FORGE scan.",
        examples=[
            "vibe2prod status ./my-app",
        ],
    ),
    "report": CommandHelp(
        name="report",
        usage="vibe2prod report <path>",
        description="View the last scan report.",
        options=[
            ("--format TEXT", "Output format: text, json, html"),
            ("--verbose / -v", "Verbose logging"),
        ],
        examples=[
            "vibe2prod report ./my-app",
            "vibe2prod report ./my-app --format json",
            "vibe2prod report ./my-app --format html",
        ],
    ),
    "setup": CommandHelp(
        name="setup",
        usage="vibe2prod setup",
        description=(
            "Interactive setup wizard — configure API keys, Claude Code\n"
            "integration, dashboard sync, and data sharing."
        ),
        options=[
            ("--api-key TEXT", "OpenRouter API key (optional)"),
            ("--v2p-key TEXT", "Vibe2Prod dashboard API key"),
            ("--no-interactive", "Headless mode (no prompts)"),
            ("--scope TEXT", "Claude Code MCP scope: user or project"),
            ("--share-forgeignore/--no-share-forgeignore", "Share anonymized suppression data"),
            ("--json / -j", "Output as JSON"),
        ],
        config=[
            "Config file:   ~/.vibe2prod/config.json",
            "MCP config:    ~/.claude/settings.json",
            "Skills:        ~/.claude/commands/forge.md, forgeignore.md",
        ],
        examples=[
            "vibe2prod setup",
            "vibe2prod setup --api-key sk-or-... --no-interactive",
            "vibe2prod setup --reset",
        ],
    ),
    "config": CommandHelp(
        name="config",
        usage="vibe2prod config <set|get> [key] [value]",
        description="Manage FORGE configuration.",
        examples=[
            "vibe2prod config get",
            "vibe2prod config get models.default",
            "vibe2prod config set models.default anthropic/claude-haiku-4.5",
            "vibe2prod config set quality_gate_profile strict",
        ],
        config=[
            "Config file:   ~/.vibe2prod/config.json",
        ],
    ),
    "update": CommandHelp(
        name="update",
        usage="vibe2prod update",
        description=(
            "Check for updates and upgrade all components.\n"
            "Syncs: package, skills, hooks, MCP registration, config schema."
        ),
        options=[
            ("--check", "Dry run — show what would change without applying"),
            ("--force", "Force re-sync everything regardless of version"),
            ("--json / -j", "Output as JSON"),
        ],
        examples=[
            "vibe2prod update",
            "vibe2prod update --check",
            "vibe2prod update --force",
        ],
    ),
    "auth": CommandHelp(
        name="auth",
        usage="vibe2prod auth <login|logout|status>",
        description="Authenticate with the Vibe2Prod platform (optional).",
        examples=[
            "vibe2prod auth login",
            "vibe2prod auth status",
            "vibe2prod auth logout",
        ],
    ),
}


# ── Top-level help ───────────────────────────────────────────────────

GROUPS = [
    ("SCANNING", ["scan", "status", "report"]),
    ("SETUP & CONFIG", ["setup", "config"]),
    ("MAINTENANCE", ["update"]),
    ("AUTHENTICATION", ["auth"]),
]

EXAMPLES = [
    "vibe2prod scan ./my-app                     # full audit",
    "vibe2prod scan ./my-app --gate strict       # strict quality gate",
    "vibe2prod report ./my-app --format json     # JSON report",
    "vibe2prod update                            # upgrade everything",
]


def format_top_level_help(version: str) -> str:
    """Format the top-level help output."""
    lines = [
        "vibe2prod — AI-powered codebase auditing engine",
        "",
    ]

    for group_name, cmd_names in GROUPS:
        lines.append(f"  {group_name}")
        for name in cmd_names:
            cmd = COMMANDS.get(name)
            if cmd:
                # First line of description only
                desc = cmd.description.split("\n")[0]
                lines.append(f"    {cmd.usage:<24s} {desc}")
        lines.append("")

    lines.append("  EXAMPLES")
    for ex in EXAMPLES:
        lines.append(f"    {ex}")
    lines.append("")
    lines.append("  Run `vibe2prod help <command>` for details on a specific command.")
    lines.append(f"  Version: {version}")

    return "\n".join(lines)


def format_command_help(name: str) -> str | None:
    """Format detailed help for a specific command."""
    cmd = COMMANDS.get(name)
    if cmd is None:
        return None

    lines = [
        cmd.usage,
        "",
        f"  {cmd.description}",
        "",
    ]

    if cmd.options:
        lines.append("  OPTIONS")
        for flag, desc in cmd.options:
            lines.append(f"    {flag:<45s} {desc}")
        lines.append("")

    if cmd.config:
        lines.append("  CONFIGURATION")
        for line in cmd.config:
            lines.append(f"    {line}")
        lines.append("")

    if cmd.examples:
        lines.append("  EXAMPLES")
        for ex in cmd.examples:
            lines.append(f"    {ex}")
        lines.append("")

    return "\n".join(lines)
