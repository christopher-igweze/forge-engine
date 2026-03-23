"""CLI for FORGE learning loop optimization.

Entry point for running optimization cycles, viewing reports, and
extracting patterns from findings history.

Usage:
    python -m forge.learning.cli optimize --mode conservative
    python -m forge.learning.cli report --json
    python -m forge.learning.cli extract --min-occurrences 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


async def run_optimize(args: argparse.Namespace) -> None:
    """Run one optimization cycle: graph -> gradients -> patches -> validate."""
    from forge.learning.backward import generate_gradients_for_failures
    from forge.learning.graph import ForgeGraph
    from forge.learning.optimizer import (
        OptimizationMode,
        generate_prompt_patch,
        load_patch,
        save_patches,
    )
    from forge.learning.validation import ab_test, load_golden_tests, save_ab_result

    artifacts_dir: Path = args.artifacts_dir
    golden_dir: Path = args.golden_dir
    mode = OptimizationMode(args.mode)

    # If applying a specific patch, load and validate it
    if args.apply_patch:
        patch_path = Path(args.apply_patch)
        if not patch_path.exists():
            print(f"Patch file not found: {patch_path}", file=sys.stderr)
            sys.exit(1)

        patch = load_patch(patch_path)
        print(f"Loaded patch for agent '{patch.agent_name}' ({len(patch.changes)} changes)")
        print(f"Mode: {patch.mode.value}, estimated change: {patch.estimated_change_pct}%")

        if args.validate_only:
            golden_tests = load_golden_tests(golden_dir)
            result = await ab_test(
                baseline_prompts={},
                patched_prompts={patch.agent_name: "\n".join(
                    c.replacement for c in patch.changes if c.replacement
                )},
                golden_tests=golden_tests,
            )
            print(f"Verdict: {result.verdict}")
            print(f"Summary: {result.summary}")
        else:
            print("Patch application to live prompts is not yet implemented.")
            print("Review the patch file and apply changes manually.")
        return

    # Step 1: Build computation graph from telemetry
    telemetry_path = artifacts_dir / "telemetry" / "invocations.jsonl"
    if not telemetry_path.exists():
        print(f"No telemetry data found at {telemetry_path}", file=sys.stderr)
        print("Run a FORGE scan first to generate telemetry data.")
        sys.exit(1)

    print("Building computation graph from telemetry...")
    graph = ForgeGraph.from_telemetry(telemetry_path)
    print(f"  Nodes: {len(graph.nodes)}, Edges: {len(graph.edges)}")

    # Step 2: Find failed nodes
    failed = graph.get_failed_nodes()
    if not failed:
        print("No failed nodes found — nothing to optimize.")
        return

    print(f"  Failed nodes: {len(failed)}")
    for node in failed:
        print(f"    - {node.node_id} ({node.phase}): {node.error or 'unknown error'}")

    # Step 3: Generate textual gradients
    if not args.validate_only:
        print("\nGenerating textual gradients...")
        gradients = await generate_gradients_for_failures(failed)
        print(f"  Generated {len(gradients)} gradients")

        for g in gradients:
            print(f"    - {g.target_node}: confidence={g.confidence:.2f}")
            for s in g.suggested_prompt_changes:
                print(f"      > {s[:80]}...")

        # Step 4: Generate prompt patches
        print(f"\nGenerating prompt patches (mode={mode.value})...")
        patches = generate_prompt_patch(graph, gradients, mode)
        print(f"  Generated {len(patches)} patches")

        if patches:
            patches_dir = artifacts_dir / "patches"
            saved = save_patches(patches, patches_dir)
            for p in saved:
                print(f"    Saved: {p}")
    else:
        # Validate-only mode: load existing patches
        patches_dir = artifacts_dir / "patches"
        if not patches_dir.is_dir():
            print("No patches found to validate.", file=sys.stderr)
            sys.exit(1)
        patches = []
        for p in patches_dir.glob("patch_*.json"):
            patches.append(load_patch(p))

    # Step 5: A/B validation
    golden_tests = load_golden_tests(golden_dir)
    if golden_tests and patches:
        print("\nRunning A/B validation...")
        patched_prompts = {}
        for patch in patches:
            additions = "\n".join(
                c.replacement for c in patch.changes if c.replacement
            )
            if additions:
                patched_prompts[patch.agent_name] = additions

        result = await ab_test(
            baseline_prompts={},
            patched_prompts=patched_prompts,
            golden_tests=golden_tests,
        )
        print(f"  Verdict: {result.verdict}")
        print(f"  Summary: {result.summary}")

        result_path = artifacts_dir / "ab_result.json"
        save_ab_result(result, result_path)
    else:
        print("\nSkipping A/B validation (no golden tests or no patches).")


def run_report(args: argparse.Namespace) -> None:
    """Show learning loop status report."""
    from forge.learning.report import generate_learning_report

    artifacts_dir: Path = args.artifacts_dir
    report = generate_learning_report(artifacts_dir)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_terminal())


def run_extract(args: argparse.Namespace) -> None:
    """Extract patterns from findings history."""
    from forge.patterns.learner import extract_proposed_patterns

    artifacts_dir: Path = args.artifacts_dir
    min_occ: int = args.min_occurrences

    patterns = extract_proposed_patterns(artifacts_dir, min_occurrences=min_occ)

    if not patterns:
        print("No recurring patterns found.")
        return

    print(f"Extracted {len(patterns)} proposed patterns:")
    for p in patterns:
        print(f"  {p.id}: {p.name} (detected {p.times_detected}x)")


def main() -> None:
    """CLI entry point for FORGE learning loop optimization."""
    parser = argparse.ArgumentParser(
        description="FORGE Learning Loop Optimization",
        prog="forge-learn",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="command")

    # forge-learn optimize
    opt = sub.add_parser("optimize", help="Run one optimization cycle")
    opt.add_argument(
        "--mode", choices=["conservative", "moderate", "aggressive"],
        default="conservative",
    )
    opt.add_argument("--golden-dir", type=Path, default=Path("tests/golden/codebases"))
    opt.add_argument("--artifacts-dir", type=Path, default=Path(".artifacts"))
    opt.add_argument("--validate-only", action="store_true",
                     help="Only validate existing patches, don't generate new ones")
    opt.add_argument("--apply-patch", type=Path,
                     help="Apply a specific patch file")

    # forge-learn report
    rpt = sub.add_parser("report", help="Show learning loop status")
    rpt.add_argument("--artifacts-dir", type=Path, default=Path(".artifacts"))
    rpt.add_argument("--json", action="store_true", help="Output as JSON")

    # forge-learn extract
    ext = sub.add_parser("extract", help="Extract patterns from findings history")
    ext.add_argument("--artifacts-dir", type=Path, default=Path(".artifacts"))
    ext.add_argument("--min-occurrences", type=int, default=3)

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "optimize":
        asyncio.run(run_optimize(args))
    elif args.command == "report":
        run_report(args)
    elif args.command == "extract":
        run_extract(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
