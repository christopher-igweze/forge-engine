"""Vibe2Prod CLI — local code audit and remediation.

Usage:
    vibe2prod scan ./my-app           # Discovery only (scan + triage)
    vibe2prod fix ./my-app            # Full pipeline (scan + fix + validate)
    vibe2prod report ./my-app         # Generate report from last run
    vibe2prod status ./my-app         # Check running scan progress
    vibe2prod config set key value    # Set a config value
    vibe2prod config get key          # Get a config value
    vibe2prod auth login              # Authenticate (coming soon)

Code never leaves your machine — only LLM API calls go to OpenRouter.
Set OPENROUTER_API_KEY in your environment before running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="vibe2prod",
    help="AI-powered code audit and remediation. Turns vibe-coded MVPs into production-ready software.",
    no_args_is_help=True,
    add_completion=False,
)

# ── Config sub-app ───────────────────────────────────────────────────

config_app = typer.Typer(help="Manage FORGE configuration.")
app.add_typer(config_app, name="config")

CONFIG_PATH = Path.home() / ".vibe2prod" / "config.json"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def _save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (e.g. models.default, privacy.telemetry)"),
    value: str = typer.Argument(..., help="Config value"),
) -> None:
    """Set a configuration value."""
    data = _load_config()
    # Support dotted keys
    keys = key.split(".")
    current = data
    for k in keys[:-1]:
        current = current.setdefault(k, {})

    # Try to parse as JSON (for bools, numbers)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed = value

    current[keys[-1]] = parsed
    _save_config(data)
    typer.echo(f"Set {key} = {parsed}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(None, help="Config key to read (omit to show all)"),
) -> None:
    """Get a configuration value."""
    data = _load_config()
    if key is None:
        typer.echo(json.dumps(data, indent=2))
        return

    keys = key.split(".")
    current = data
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            typer.echo(f"Key '{key}' not found.", err=True)
            raise typer.Exit(1)

    typer.echo(f"{key} = {json.dumps(current) if isinstance(current, (dict, list)) else current}")


# ── Helpers ──────────────────────────────────────────────────────────


def _check_api_key(api_key: str | None) -> str:
    """Resolve the OpenRouter API key from flag or environment."""
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        typer.echo(
            "Error: OPENROUTER_API_KEY is not set.\n"
            "Set it in your environment or pass --api-key:\n\n"
            "  export OPENROUTER_API_KEY=sk-or-v1-...\n"
            "  vibe2prod scan ./my-app\n",
            err=True,
        )
        raise typer.Exit(1)
    # Warn on obviously invalid format (OpenRouter keys start with sk-or-)
    if not key.startswith("sk-or-"):
        typer.echo(
            "Warning: API key does not match expected OpenRouter format (sk-or-...).\n"
            "If this is intentional, you can ignore this warning.",
            err=True,
        )
    # Intentional side effect: downstream code (ForgeConfig, AgentAI client,
    # standalone dispatcher) reads OPENROUTER_API_KEY from the environment.
    # This avoids threading the key through every function in the call chain.
    os.environ["OPENROUTER_API_KEY"] = key
    return key


def _setup_logging(verbose: bool) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy loggers in normal mode
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_path(path: str) -> str:
    """Resolve and validate the repo path."""
    resolved = str(Path(path).resolve())
    if not Path(resolved).is_dir():
        typer.echo(f"Error: '{path}' is not a directory.", err=True)
        raise typer.Exit(1)
    if not Path(resolved, ".git").exists():
        typer.echo(
            f"Warning: '{path}' is not a git repository. "
            "Some features (worktree isolation, PR creation) won't work.",
            err=True,
        )
    return resolved


def _print_summary(result) -> None:
    """Print a human-readable summary of the FORGE result."""
    typer.echo("")
    if result.success:
        typer.echo(typer.style("FORGE completed successfully", fg=typer.colors.GREEN, bold=True))
    else:
        typer.echo(typer.style("FORGE completed with errors", fg=typer.colors.RED, bold=True))

    typer.echo(f"  Run ID:       {result.forge_run_id}")
    typer.echo(f"  Mode:         {result.mode.value}")
    typer.echo(f"  Duration:     {result.duration_seconds:.1f}s")
    typer.echo(f"  Findings:     {result.total_findings}")
    typer.echo(f"  Fixed:        {result.findings_fixed}")
    typer.echo(f"  Deferred:     {result.findings_deferred}")
    typer.echo(f"  Invocations:  {result.agent_invocations}")

    if result.cost_usd > 0:
        typer.echo(f"  Est. cost:    ${result.cost_usd:.4f}")

    if result.readiness_report:
        score = result.readiness_report.overall_score
        color = typer.colors.GREEN if score >= 80 else (typer.colors.YELLOW if score >= 60 else typer.colors.RED)
        typer.echo(f"  Readiness:    {typer.style(str(score), fg=color)}/100")

    # v3 evaluation
    if hasattr(result, 'evaluation') and result.evaluation:
        eval_data = result.evaluation
        scores = eval_data.get("scores", {})
        composite = scores.get("composite", "N/A")
        band_letter = scores.get("band", "?")
        band_label = scores.get("band_label", "")
        gate = eval_data.get("quality_gate", {})
        gate_passed = gate.get("passed", None)

        typer.echo("")
        typer.echo(typer.style("  Evaluation (v3)", bold=True))

        if isinstance(composite, (int, float)):
            comp_color = typer.colors.GREEN if composite >= 80 else (typer.colors.YELLOW if composite >= 60 else typer.colors.RED)
        else:
            comp_color = typer.colors.WHITE
        typer.echo(f"    Score:  {typer.style(f'{composite}/100 ({band_letter})', fg=comp_color)} — {band_label}")

        if gate_passed is not None:
            gate_color = typer.colors.GREEN if gate_passed else typer.colors.RED
            gate_text = "PASSED" if gate_passed else "FAILED"
            typer.echo(f"    Gate:   {typer.style(gate_text, fg=gate_color)} ({gate.get('profile', 'forge-way')})")
            if not gate_passed and gate.get("failures"):
                for f in gate["failures"]:
                    typer.echo(f"            x {f}")

    # AIVSS scoring
    if hasattr(result, 'aivss_score') and result.aivss_score:
        aivss = result.aivss_score
        score = aivss.get("score", 0)
        severity = aivss.get("severity", "Unknown")
        aivss_color = typer.colors.RED if score >= 7.0 else (typer.colors.YELLOW if score >= 4.0 else typer.colors.GREEN)
        typer.echo(f"\n  {typer.style('AIVSS Score', bold=True)}: {typer.style(f'{score}/10 ({severity})', fg=aivss_color)}")
        typer.echo(f"    Base: {aivss.get('base_score', '?')}  AI: {aivss.get('ai_metrics_score', '?')}  AARS: {aivss.get('aars_score', '?')}  Impact: {aivss.get('impact_score', '?')}")

    typer.echo(f"\n  Artifacts:    {Path(result.forge_run_id).parent if result.forge_run_id else 'N/A'}")
    typer.echo("")


# ── Commands ─────────────────────────────────────────────────────────


@app.command()
def scan(
    path: str = typer.Argument(..., help="Path to the repository to scan"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="OpenRouter API key (or set OPENROUTER_API_KEY)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Default model override (e.g. anthropic/claude-haiku-4.5)"),
    gate: str = typer.Option("forge-way", "--gate", "-g", help="Quality gate profile: forge-way, strict, startup"),
    max_cost: float = typer.Option(0.0, "--max-cost", help="Max cost in USD before aborting (0 = no limit)"),
    max_time: float = typer.Option(0.0, "--max-time", help="Max duration in seconds before aborting (0 = no limit)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    aivss: bool = typer.Option(False, "--aivss", help="Include OWASP AIVSS scoring in report"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output result as JSON"),
) -> None:
    """Scan a repository for issues (discovery + triage, no fixes).

    Runs FORGE Agents 1-6: codebase analysis, security audit, quality
    audit, architecture review, triage classification, and fix strategy.

    Example:
        vibe2prod scan ./my-app
        vibe2prod scan ./my-app --model anthropic/claude-haiku-4.5
    """
    _check_api_key(api_key)
    _setup_logging(verbose)
    repo_path = _resolve_path(path)

    config: dict = {
        "mode": "discovery",
        "repo_path": repo_path,
        "quality_gate_profile": gate,
    }
    if model:
        config["models"] = {"default": model}
    if max_cost > 0:
        config["max_cost_usd"] = max_cost
    if max_time > 0:
        config["max_duration_seconds"] = max_time
    if aivss:
        config["aivss_enabled"] = True

    from forge.standalone import run_standalone

    typer.echo(f"Scanning {repo_path}...")
    result = asyncio.run(run_standalone(repo_path=repo_path, config=config))

    if json_output:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _print_summary(result)


@app.command()
def fix(
    path: str = typer.Argument(..., help="Path to the repository to scan and plan fixes"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="OpenRouter API key (or set OPENROUTER_API_KEY)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Default model override"),
    max_cost: float = typer.Option(0.0, "--max-cost", help="Max cost in USD before aborting (0 = no limit)"),
    max_time: float = typer.Option(0.0, "--max-time", help="Max duration in seconds before aborting (0 = no limit)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output result as JSON"),
) -> None:
    """Scan, evaluate, and produce a remediation plan.

    Runs discovery + triage to produce a prioritized fix plan.
    Apply fixes using the plan with your preferred coding tool
    (Claude Code, Cursor, etc.).

    Example:
        vibe2prod fix ./my-app
        vibe2prod fix ./my-app --model anthropic/claude-haiku-4.5
    """
    _check_api_key(api_key)
    _setup_logging(verbose)
    repo_path = _resolve_path(path)

    config: dict = {
        "mode": "full",
        "repo_path": repo_path,
    }
    if model:
        config["models"] = {"default": model}
    if max_cost > 0:
        config["max_cost_usd"] = max_cost
    if max_time > 0:
        config["max_duration_seconds"] = max_time

    from forge.standalone import run_standalone

    typer.echo(f"Running FORGE remediation on {repo_path}...")
    result = asyncio.run(run_standalone(repo_path=repo_path, config=config))

    if json_output:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _print_summary(result)

    raise typer.Exit(0 if result.success else 1)


@app.command()
def status(
    path: str = typer.Argument(".", help="Path to the repository"),
) -> None:
    """Show real-time status of a running FORGE scan.

    Reads the live telemetry from <repo>/.artifacts/telemetry/live_status.json.
    Use in a separate terminal while a scan is running.

    Example:
        vibe2prod status ./my-app
    """
    repo_path = _resolve_path(path)
    status_file = Path(repo_path, ".artifacts", "telemetry", "live_status.json")

    if not status_file.exists():
        typer.echo("No active run detected. Start a scan first:", err=True)
        typer.echo("  vibe2prod scan ./my-app", err=True)
        raise typer.Exit(1)

    data = json.loads(status_file.read_text())

    # Format output
    phase = data.get("phase", "unknown")
    elapsed = data.get("elapsed_human", "?")
    budget = data.get("budget", {})
    findings = data.get("findings", {})
    active = data.get("active_agents", [])
    phases_done = data.get("phases_completed", [])

    typer.echo(typer.style("FORGE Run Status", bold=True))
    typer.echo("=" * 40)

    # Phase
    phases_str = " > ".join([typer.style(p, fg=typer.colors.GREEN) for p in phases_done])
    if phases_str:
        phases_str += " > "
    phases_str += typer.style(phase, fg=typer.colors.YELLOW, bold=True)
    typer.echo(f"  Phase:    {phases_str}")

    # Time
    time_pct = budget.get("time_percent", 0)
    time_limit = budget.get("time_limit", 0)
    typer.echo(f"  Time:     {elapsed}" + (f" / {int(time_limit)}s ({time_pct}%)" if time_limit else ""))

    # Cost
    cost_spent = budget.get("cost_spent", 0)
    cost_limit = budget.get("cost_limit", 0)
    cost_pct = budget.get("cost_percent", 0)
    cost_color = typer.colors.RED if cost_pct > 80 else (typer.colors.YELLOW if cost_pct > 50 else typer.colors.GREEN)
    typer.echo(f"  Cost:     {typer.style(f'${cost_spent:.4f}', fg=cost_color)}" +
               (f" / ${cost_limit:.2f} ({cost_pct}%)" if cost_limit else ""))

    # Invocations
    totals = data.get("totals", {})
    typer.echo(f"  Calls:    {totals.get('invocations', 0)} ({totals.get('failed', 0)} failed)")

    # Findings
    if findings.get("total", 0) > 0:
        typer.echo(f"\n  Findings: {findings['total']} total, "
                   f"{findings.get('fixed', 0)} fixed, "
                   f"{findings.get('deferred', 0)} deferred, "
                   f"{findings.get('in_progress', 0)} in progress")

    # Active agents
    if active:
        typer.echo(f"\n  Active agents:")
        for a in active:
            typer.echo(f"    {a.get('name', '?'):40s} {a.get('model', '?'):25s} {a.get('running_for', '?')}")

    typer.echo("")


@app.command()
def auth(
    action: str = typer.Argument("login", help="Auth action: login, logout, status"),
) -> None:
    """Authenticate with the Vibe2Prod platform (optional).

    Enables: scan history sync, cross-repo trends, team sharing, cloud remediation.

    Example:
        vibe2prod auth login
    """
    if action == "login":
        typer.echo("Vibe2Prod platform authentication is coming soon.")
        typer.echo("For now, FORGE runs fully locally with your OpenRouter API key.")
        typer.echo("\nSet up: export OPENROUTER_API_KEY=sk-or-v1-...")
    elif action == "logout":
        config = _load_config()
        config.pop("auth", None)
        _save_config(config)
        typer.echo("Logged out.")
    elif action == "status":
        config = _load_config()
        if config.get("auth", {}).get("api_key"):
            typer.echo("Authenticated with Vibe2Prod platform.")
        else:
            typer.echo("Not authenticated. Run: vibe2prod auth login")
    else:
        typer.echo(f"Unknown action: {action}. Use: login, logout, status", err=True)
        raise typer.Exit(1)


@app.command()
def report(
    path: str = typer.Argument(..., help="Path to the repository"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text, json, html"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Display the report from the last FORGE run.

    Reads the latest report from <repo>/.artifacts/report/.

    Example:
        vibe2prod report ./my-app
        vibe2prod report ./my-app --format json
    """
    _setup_logging(verbose)
    repo_path = _resolve_path(path)

    artifacts_dir = Path(repo_path, ".artifacts", "report")
    if not artifacts_dir.is_dir():
        typer.echo("No report found. Run 'vibe2prod fix' first to generate a report.", err=True)
        raise typer.Exit(1)

    # Find the latest report
    report_files = sorted(artifacts_dir.glob("forge-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not report_files:
        typer.echo("No report files found in .artifacts/report/.", err=True)
        raise typer.Exit(1)

    latest = report_files[0]

    if format == "json":
        typer.echo(latest.read_text())
    elif format == "html":
        html_file = latest.with_suffix(".html")
        if html_file.exists():
            typer.echo(html_file.read_text())
        else:
            typer.echo("No HTML report found. JSON report:", err=True)
            typer.echo(latest.read_text())
    else:
        # Pretty-print the JSON report as text
        data = json.loads(latest.read_text())
        typer.echo(typer.style("FORGE Production Readiness Report", bold=True))
        typer.echo(f"  File: {latest.name}")
        typer.echo(f"  Score: {data.get('overall_score', 'N/A')}/100")
        typer.echo("")

        for section in data.get("sections", []):
            typer.echo(f"  [{section.get('category', '?')}] {section.get('title', '')}")
            if section.get("items"):
                for item in section["items"]:
                    item_status = "FIXED" if item.get("fixed") else "DEFERRED"
                    typer.echo(f"    - [{item_status}] {item.get('title', '')}")
            typer.echo("")


def main() -> None:
    """Entry point for the vibe2prod CLI."""
    app()


if __name__ == "__main__":
    main()
