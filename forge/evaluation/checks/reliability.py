"""Reliability dimension checks (REL-001 through REL-007)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from forge.evaluation.checks import (
    CheckResult,
    iter_source_files,
    is_test_file,
    read_file_safe,
    parse_ast_safe,
)

_ROUTE_DECORATOR = re.compile(
    r"""@(?:app|router|api)\.\s*(?:get|post|put|delete|patch|options|head|route)\s*\(""",
    re.IGNORECASE,
)

_HEALTH_ENDPOINTS = re.compile(
    r"""["'](/health|/healthz|/ready|/liveness|/readiness)["']""",
    re.IGNORECASE,
)

_GRACEFUL_SHUTDOWN = re.compile(
    r"""(?:signal\.signal\s*\(|SIGTERM|atexit\.register|lifespan|on_shutdown|on_event\s*\(\s*["']shutdown["']\))""",
    re.IGNORECASE,
)

_HTTP_CALL_NO_TIMEOUT = re.compile(
    r"""(?:requests|httpx)\.\s*(?:get|post|put|delete|patch|head|options)\s*\("""
)
_TIMEOUT_PARAM = re.compile(r"""timeout\s*=""")

_RETRY_INDICATORS = re.compile(
    r"""(?:retry|backoff|tenacity|Retry|@retry|RetryPolicy|max_retries)""",
    re.IGNORECASE,
)

_RAW_DB_CONNECT = re.compile(
    r"""(?:psycopg2\.connect|pymongo\.MongoClient|create_engine|AsyncEngine|asyncpg\.connect)\s*\("""
)
_POOL_CONFIG = re.compile(
    r"""(?:pool_size|max_connections|pool_recycle|pool_pre_ping|maxPoolSize|minPoolSize)"""
)


def _check_rel001(repo_path: str) -> CheckResult:
    """REL-001: No error handling at API boundary."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Check for route decorator
            has_route = False
            for dec in node.decorator_list:
                dec_src = ast.dump(dec)
                if any(kw in dec_src for kw in ("get", "post", "put", "delete", "patch", "route")):
                    has_route = True
                    break
            if not has_route:
                continue
            # Check for try/except in body
            has_try = any(isinstance(stmt, ast.Try) for stmt in ast.walk(node))
            if not has_try:
                locations.append({
                    "file": str(path),
                    "line": node.lineno,
                    "snippet": f"def {node.name}() — route handler without try/except",
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="REL-001",
        name="No error handling at API boundary",
        passed=passed,
        severity="high",
        deduction=0 if passed else -15,
        locations=locations,
        details=f"{len(locations)} route handler(s) lack error handling." if locations else "",
    )


def _check_rel002(repo_path: str) -> CheckResult:
    """REL-002: No health check endpoint."""
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        if _HEALTH_ENDPOINTS.search(content):
            return CheckResult(
                check_id="REL-002",
                name="No health check endpoint",
                passed=True,
                severity="high",
                deduction=0,
            )
    return CheckResult(
        check_id="REL-002",
        name="No health check endpoint",
        passed=False,
        severity="high",
        deduction=-10,
        details="No /health, /healthz, /ready, /liveness, or /readiness endpoint found.",
    )


def _check_rel003(repo_path: str) -> CheckResult:
    """REL-003: No graceful shutdown."""
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        if _GRACEFUL_SHUTDOWN.search(content):
            return CheckResult(
                check_id="REL-003",
                name="No graceful shutdown",
                passed=True,
                severity="medium",
                deduction=0,
            )
    return CheckResult(
        check_id="REL-003",
        name="No graceful shutdown",
        passed=False,
        severity="medium",
        deduction=-8,
        details="No signal handler, atexit, lifespan, or on_shutdown found.",
    )


def _check_rel004(repo_path: str) -> CheckResult:
    """REL-004: Silent exception swallowing."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                locations.append({
                    "file": str(path),
                    "line": node.lineno,
                    "snippet": "except: pass — silent exception swallowing",
                })
            elif len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                # except: "docstring only" or except: ...
                locations.append({
                    "file": str(path),
                    "line": node.lineno,
                    "snippet": "except: <no-op> — silent exception swallowing",
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="REL-004",
        name="Silent exception swallowing",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -8,
        locations=locations,
        details=f"{len(locations)} silent except block(s) found." if locations else "",
    )


def _check_rel005(repo_path: str) -> CheckResult:
    """REL-005: No timeout on HTTP calls."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _HTTP_CALL_NO_TIMEOUT.search(line):
                # Check current line and next few lines for timeout=
                context = "\n".join(lines[i:i + 5])
                if not _TIMEOUT_PARAM.search(context):
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    passed = len(locations) == 0
    return CheckResult(
        check_id="REL-005",
        name="No timeout on HTTP calls",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -5,
        locations=locations,
        details=f"{len(locations)} HTTP call(s) without timeout." if locations else "",
    )


def _check_rel006(repo_path: str) -> CheckResult:
    """REL-006: No retry logic."""
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        if _RETRY_INDICATORS.search(content):
            return CheckResult(
                check_id="REL-006",
                name="No retry logic",
                passed=True,
                severity="low",
                deduction=0,
            )
    return CheckResult(
        check_id="REL-006",
        name="No retry logic",
        passed=False,
        severity="low",
        deduction=-3,
        details="No retry/backoff/tenacity patterns found in source.",
    )


def _check_rel007(repo_path: str) -> CheckResult:
    """REL-007: Missing connection pool config."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _RAW_DB_CONNECT.search(line):
                context = "\n".join(lines[max(0, i - 2):i + 5])
                if not _POOL_CONFIG.search(context):
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    passed = len(locations) == 0
    return CheckResult(
        check_id="REL-007",
        name="Missing connection pool config",
        passed=passed,
        severity="low",
        deduction=0 if passed else -3,
        locations=locations,
        details=f"{len(locations)} DB connection(s) without pool config." if locations else "",
    )


def run_reliability_checks(repo_path: str) -> list[CheckResult]:
    """Run all 7 reliability checks against the repository."""
    return [
        _check_rel001(repo_path),
        _check_rel002(repo_path),
        _check_rel003(repo_path),
        _check_rel004(repo_path),
        _check_rel005(repo_path),
        _check_rel006(repo_path),
        _check_rel007(repo_path),
    ]
