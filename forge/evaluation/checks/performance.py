"""Performance dimension checks (PRF-001 through PRF-005)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from forge.evaluation.checks import (
    CheckResult,
    iter_source_files,
    read_file_safe,
    parse_ast_safe,
)

_DB_CALL_PATTERN = re.compile(
    r"""\.(?:query|execute|find|select|filter|all|get|fetch|objects)\s*\(""",
    re.IGNORECASE,
)

_UNBOUNDED_QUERY = re.compile(
    r"""\.(?:all|find|select)\s*\(""",
)
_LIMIT_PATTERN = re.compile(
    r"""(?:\.limit\s*\(|\[\s*:\s*|LIMIT\s+\d|\.paginate\s*\()""",
    re.IGNORECASE,
)

_ROUTE_DECORATOR = re.compile(
    r"""@(?:app|router|api)\.\s*(?:get|post|put|delete|patch)\s*\(""",
    re.IGNORECASE,
)
_PAGINATION_PARAMS = re.compile(
    r"""\b(?:offset|limit|page|skip|page_size|per_page|cursor)\b""",
    re.IGNORECASE,
)

_SYNC_IO_IN_ASYNC = re.compile(
    r"""(?:requests\.\w+\s*\(|time\.sleep\s*\(|open\s*\()""",
)

_CACHE_INDICATORS = re.compile(
    r"""(?:@cache\b|@cached\b|lru_cache|@functools\.cache|Redis|memcache|cachetools|@memoize|cache_page)""",
    re.IGNORECASE,
)


def _check_prf001(repo_path: str) -> CheckResult:
    """PRF-001: N+1 query pattern (DB call inside loop)."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue

        class LoopDBVisitor(ast.NodeVisitor):
            def __init__(self):
                self.in_loop = False

            def visit_For(self, node):
                old = self.in_loop
                self.in_loop = True
                self.generic_visit(node)
                self.in_loop = old

            def visit_While(self, node):
                old = self.in_loop
                self.in_loop = True
                self.generic_visit(node)
                self.in_loop = old

            def visit_Call(self, node):
                if self.in_loop and isinstance(node.func, ast.Attribute):
                    attr = node.func.attr
                    if attr in ("query", "execute", "find", "select", "filter", "get", "fetch"):
                        locations.append({
                            "file": str(path),
                            "line": node.lineno,
                            "snippet": f".{attr}() inside loop — N+1 risk",
                        })
                self.generic_visit(node)

        LoopDBVisitor().visit(tree)

    deduction = max(-20, -10 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="PRF-001",
        name="N+1 query pattern",
        passed=passed,
        severity="high",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} potential N+1 query pattern(s)." if locations else "",
    )


def _check_prf002(repo_path: str) -> CheckResult:
    """PRF-002: Unbounded query (no LIMIT)."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _UNBOUNDED_QUERY.search(line):
                context = "\n".join(lines[max(0, i - 1):i + 4])
                if not _LIMIT_PATTERN.search(context):
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    deduction = max(-15, -5 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="PRF-002",
        name="Unbounded query",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} unbounded query/queries." if locations else "",
    )


def _check_prf003(repo_path: str) -> CheckResult:
    """PRF-003: Missing pagination on list endpoints."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _ROUTE_DECORATOR.search(line):
                # Look at function signature + body (next ~15 lines)
                func_context = "\n".join(lines[i:i + 15])
                # Check if this returns a list
                if re.search(r"(?:list|List|\[\]|\.all\(\)|\.find\()", func_context):
                    if not _PAGINATION_PARAMS.search(func_context):
                        locations.append({
                            "file": str(path),
                            "line": i + 1,
                            "snippet": line.strip()[:120],
                        })
    deduction = max(-10, -5 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="PRF-003",
        name="Missing pagination on list endpoints",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} list endpoint(s) without pagination." if locations else "",
    )


def _check_prf004(repo_path: str) -> CheckResult:
    """PRF-004: Sync I/O in async context."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            # Get source lines for this function
            end_line = getattr(node, "end_lineno", node.lineno + 50)
            func_lines = content.splitlines()[node.lineno - 1:end_line]
            func_src = "\n".join(func_lines)
            for match in _SYNC_IO_IN_ASYNC.finditer(func_src):
                locations.append({
                    "file": str(path),
                    "line": node.lineno,
                    "snippet": f"async def {node.name}() uses sync I/O: {match.group()[:60]}",
                })
                break  # One per function

    deduction = max(-15, -5 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="PRF-004",
        name="Sync I/O in async context",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} async function(s) with sync I/O." if locations else "",
    )


def _check_prf005(repo_path: str) -> CheckResult:
    """PRF-005: No caching present."""
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        if _CACHE_INDICATORS.search(content):
            return CheckResult(
                check_id="PRF-005",
                name="No caching present",
                passed=True,
                severity="low",
                deduction=0,
            )
    return CheckResult(
        check_id="PRF-005",
        name="No caching present",
        passed=False,
        severity="low",
        deduction=-3,
        details="No caching mechanism (lru_cache, Redis, memcache, etc.) found.",
    )


def run_performance_checks(repo_path: str) -> list[CheckResult]:
    """Run all 5 performance checks against the repository."""
    return [
        _check_prf001(repo_path),
        _check_prf002(repo_path),
        _check_prf003(repo_path),
        _check_prf004(repo_path),
        _check_prf005(repo_path),
    ]
