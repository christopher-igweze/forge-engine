"""Vibe2Prod CLI — local code audit and remediation.

Usage:
    vibe2prod scan ./my-app           # Discovery only (scan + triage)
    vibe2prod fix ./my-app            # Full pipeline (scan + fix + validate)
    vibe2prod report ./my-app         # Generate report from last run

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

    typer.echo(f"\n  Artifacts:    {Path(result.forge_run_id).parent if result.forge_run_id else 'N/A'}")
    typer.echo("")


@app.command()
def scan(
    path: str = typer.Argument(..., help="Path to the repository to scan"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="OpenRouter API key (or set OPENROUTER_API_KEY)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Default model override (e.g. anthropic/claude-haiku-4.5)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
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
        "dry_run": True,
        "repo_path": repo_path,
    }
    if model:
        config["models"] = {"default": model}

    from forge.standalone import run_standalone

    typer.echo(f"Scanning {repo_path}...")
    result = asyncio.run(run_standalone(repo_path=repo_path, config=config))

    if json_output:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _print_summary(result)


@app.command()
def fix(
    path: str = typer.Argument(..., help="Path to the repository to fix"),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="OpenRouter API key (or set OPENROUTER_API_KEY)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Default model override"),
    coder_model: str | None = typer.Option(None, "--coder-model", help="Model for coder agents (Tier 2/3)"),
    max_retries: int = typer.Option(3, "--max-retries", help="Max inner loop retries per finding"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan and plan but don't apply fixes"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output result as JSON"),
) -> None:
    """Full remediation pipeline: scan, triage, fix, and validate.

    Runs all 12 FORGE agents. Fixes are applied in isolated git
    worktrees and merged back on success.

    Example:
        vibe2prod fix ./my-app
        vibe2prod fix ./my-app --coder-model anthropic/claude-sonnet-4.6
        vibe2prod fix ./my-app --dry-run  # plan without applying
    """
    _check_api_key(api_key)
    _setup_logging(verbose)
    repo_path = _resolve_path(path)

    config: dict = {
        "mode": "full",
        "dry_run": dry_run,
        "repo_path": repo_path,
        "max_inner_retries": max_retries,
    }
    models_dict: dict = {}
    if model:
        models_dict["default"] = model
    if coder_model:
        models_dict["coder_tier2"] = coder_model
        models_dict["coder_tier3"] = coder_model
    if models_dict:
        config["models"] = models_dict

    from forge.standalone import run_standalone

    typer.echo(f"Running FORGE remediation on {repo_path}...")
    result = asyncio.run(run_standalone(repo_path=repo_path, config=config))

    if json_output:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _print_summary(result)

    raise typer.Exit(0 if result.success else 1)


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
                    status = "FIXED" if item.get("fixed") else "DEFERRED"
                    typer.echo(f"    - [{status}] {item.get('title', '')}")
            typer.echo("")


def main() -> None:
    """Entry point for the vibe2prod CLI."""
    app()


if __name__ == "__main__":
    main()
