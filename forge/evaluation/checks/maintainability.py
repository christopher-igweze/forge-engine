"""Maintainability dimension checks (MNT-001 through MNT-005)."""

from __future__ import annotations

import ast
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from forge.evaluation.checks import (
    CheckResult,
    iter_source_files,
    read_file_safe,
    parse_ast_safe,
    severity_deduction,
)


def _check_mnt001(repo_path: str) -> CheckResult:
    """MNT-001: God classes (>500 lines)."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                end = getattr(node, "end_lineno", None)
                if end is not None:
                    span = end - node.lineno + 1
                    if span > 500:
                        locations.append({
                            "file": str(path),
                            "line": node.lineno,
                            "snippet": f"class {node.name}: {span} lines",
                        })
    deduction = max(severity_deduction("high") * 2, -20 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="MNT-001",
        name="God classes",
        passed=passed,
        severity="high",
        deduction=0 if passed else deduction,
        locations=locations[:5],
        details=f"{len(locations)} class(es) exceed 500 lines." if locations else "",
        fix_guidance="Split large classes into smaller, focused classes using composition and single responsibility." if not passed else "",
    )


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Count branches contributing to cyclomatic complexity."""
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp)):
            complexity += 1
        elif isinstance(child, ast.For):
            complexity += 1
        elif isinstance(child, ast.While):
            complexity += 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # Each and/or adds a branch
            complexity += len(child.values) - 1
        elif isinstance(child, ast.Match):
            complexity += len(child.cases) - 1
    return complexity


def _check_mnt002(repo_path: str) -> CheckResult:
    """MNT-002: High cyclomatic complexity (>20)."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cc = _cyclomatic_complexity(node)
                if cc > 20:
                    locations.append({
                        "file": str(path),
                        "line": node.lineno,
                        "snippet": f"def {node.name}(): complexity={cc}",
                    })
    deduction = max(severity_deduction("medium") * 2, -10 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="MNT-002",
        name="High cyclomatic complexity",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations[:5],
        details=f"{len(locations)} function(s) exceed complexity threshold of 20." if locations else "",
        fix_guidance="Extract helper functions, use early returns, and simplify conditional logic to reduce complexity." if not passed else "",
    )


def _max_nesting_depth(node: ast.AST) -> int:
    """Get maximum nesting depth within a function."""
    _NESTING_TYPES = (ast.If, ast.For, ast.While, ast.With, ast.Try)

    def _walk_depth(n: ast.AST, depth: int) -> int:
        max_d = depth
        for child in ast.iter_child_nodes(n):
            if isinstance(child, _NESTING_TYPES):
                max_d = max(max_d, _walk_depth(child, depth + 1))
            else:
                max_d = max(max_d, _walk_depth(child, depth))
        return max_d

    return _walk_depth(node, 0)


def _check_mnt003(repo_path: str) -> CheckResult:
    """MNT-003: Deep nesting (>4 levels)."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                depth = _max_nesting_depth(node)
                if depth > 4:
                    locations.append({
                        "file": str(path),
                        "line": node.lineno,
                        "snippet": f"def {node.name}(): nesting depth={depth}",
                    })
    deduction = max(severity_deduction("medium") * 2, -8 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="MNT-003",
        name="Deep nesting",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations[:5],
        details=f"{len(locations)} function(s) exceed 4 levels of nesting." if locations else "",
        fix_guidance="Use guard clauses (early returns), extract inner blocks into functions, or invert conditions." if not passed else "",
    )


def _check_mnt004(repo_path: str) -> CheckResult:
    """MNT-004: Significant code duplication (>20 consecutive identical lines)."""
    BLOCK_SIZE = 20
    block_hashes: dict[str, list[tuple[str, int]]] = defaultdict(list)
    locations = []

    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        if len(lines) < BLOCK_SIZE:
            continue
        for i in range(len(lines) - BLOCK_SIZE + 1):
            block = "\n".join(line.strip() for line in lines[i:i + BLOCK_SIZE])
            if not block.strip():
                continue
            h = hashlib.md5(block.encode()).hexdigest()  # nosec — not used for security, only duplication fingerprinting
            block_hashes[h].append((str(path), i + 1))

    seen_pairs = set()
    for h, locs in block_hashes.items():
        if len(locs) < 2:
            continue
        # Report first duplicate pair
        for j in range(1, len(locs)):
            pair_key = (locs[0][0], locs[0][1], locs[j][0], locs[j][1])
            if pair_key not in seen_pairs:
                seen_pairs.add(pair_key)
                locations.append({
                    "file": locs[0][0],
                    "line": locs[0][1],
                    "snippet": f"Duplicate block also at {locs[j][0]}:{locs[j][1]}",
                })
                if len(locations) >= 5:
                    break
        if len(locations) >= 5:
            break

    deduction = max(severity_deduction("medium") * 2, -8 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="MNT-004",
        name="Code duplication",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations[:5],
        details=f"{len(locations)} duplicate block pair(s) found (>{BLOCK_SIZE} lines)." if locations else "",
        fix_guidance="Extract duplicated code blocks into shared functions or modules." if not passed else "",
    )


def _check_mnt005(repo_path: str) -> CheckResult:
    """MNT-005: Circular imports."""
    import_graph: dict[str, set[str]] = defaultdict(set)
    module_files: dict[str, str] = {}
    repo = Path(repo_path).resolve()

    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        try:
            rel = path.resolve().relative_to(repo)
        except ValueError:
            continue
        module_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        module_files[module_name] = str(path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_graph[module_name].add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    import_graph[module_name].add(node.module)

    # DFS cycle detection
    cycles = []
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def _dfs(mod: str, path_list: list[str]) -> None:
        if len(cycles) >= 3:
            return
        visited.add(mod)
        rec_stack.add(mod)
        for dep in import_graph.get(mod, set()):
            if dep not in import_graph:
                continue  # External dependency
            if dep not in visited:
                _dfs(dep, path_list + [dep])
            elif dep in rec_stack:
                cycle_start = path_list.index(dep) if dep in path_list else -1
                if cycle_start >= 0:
                    cycle = path_list[cycle_start:] + [dep]
                    cycles.append(cycle)
        rec_stack.discard(mod)

    for mod in import_graph:
        if mod not in visited:
            _dfs(mod, [mod])

    locations = []
    for cycle in cycles[:3]:
        locations.append({
            "file": module_files.get(cycle[0], cycle[0]),
            "line": 1,
            "snippet": " -> ".join(cycle),
        })

    deduction = max(severity_deduction("medium") * 2, -10 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="MNT-005",
        name="Circular imports",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations[:5],
        details=f"{len(locations)} circular import cycle(s) detected." if locations else "",
        fix_guidance="Break cycles by extracting shared types into a separate module or using dependency injection." if not passed else "",
    )


def run_maintainability_checks(repo_path: str) -> list[CheckResult]:
    """Run all 5 maintainability checks against the repository."""
    return [
        _check_mnt001(repo_path),
        _check_mnt002(repo_path),
        _check_mnt003(repo_path),
        _check_mnt004(repo_path),
        _check_mnt005(repo_path),
    ]
