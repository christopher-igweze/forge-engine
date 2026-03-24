"""Documentation dimension checks (DOC-001 through DOC-006)."""

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


def _check_doc001(repo_path: str) -> CheckResult:
    """DOC-001: No README."""
    repo = Path(repo_path)
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md"):
        if (repo / name).exists():
            return CheckResult(
                check_id="DOC-001",
                name="No README",
                passed=True,
                severity="high",
                deduction=0,
            )
    return CheckResult(
        check_id="DOC-001",
        name="No README",
        passed=False,
        severity="high",
        deduction=-30,
        details="No README.md, README.rst, or README.txt found.",
        fix_guidance="Create a README.md with project description, setup instructions, and usage examples.",
    )


def _check_doc002(repo_path: str) -> CheckResult:
    """DOC-002: README too short (<10 non-empty lines)."""
    repo = Path(repo_path)
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md"):
        p = repo / name
        if p.exists():
            content = read_file_safe(p)
            non_empty = [l for l in content.splitlines() if l.strip()]
            passed = len(non_empty) >= 10
            return CheckResult(
                check_id="DOC-002",
                name="README too short",
                passed=passed,
                severity="medium",
                deduction=0 if passed else -15,
                locations=[{"file": str(p), "line": 1}] if not passed else [],
                details=f"README has {len(non_empty)} non-empty lines." if not passed else "",
                fix_guidance="Expand README to cover installation, usage, and contribution guidelines (>=10 substantive lines)." if not passed else "",
            )
    # No README at all — DOC-001 covers this
    return CheckResult(
        check_id="DOC-002",
        name="README too short",
        passed=True,
        severity="medium",
        deduction=0,
        details="No README found (covered by DOC-001).",
    )


def _check_doc003(repo_path: str) -> CheckResult:
    """DOC-003: No API documentation."""
    indicators = re.compile(
        r"""(?:openapi|swagger|@api_view|@api\.doc|@apispec|redoc)""",
        re.IGNORECASE,
    )
    # Check for OpenAPI/Swagger files
    repo = Path(repo_path)
    for name in ("openapi.json", "openapi.yaml", "swagger.json", "swagger.yaml"):
        if (repo / name).exists() or (repo / "docs" / name).exists():
            return CheckResult(
                check_id="DOC-003",
                name="No API documentation",
                passed=True,
                severity="medium",
                deduction=0,
            )

    # Check source files for API doc decorators or route docstrings
    has_routes = False
    has_docs = False
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        if re.search(r"""@(?:app|router|api)\.\s*(?:get|post|put|delete)""", content):
            has_routes = True
        if indicators.search(content):
            has_docs = True
            break
        # Check if route handlers have docstrings
        if has_routes and '"""' in content:
            tree = parse_ast_safe(content, str(path))
            if tree:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if ast.get_docstring(node) and any(
                            isinstance(d, ast.Call) for d in getattr(node, "decorator_list", [])
                        ):
                            has_docs = True
                            break

    if not has_routes:
        # No API routes found — not applicable
        return CheckResult(
            check_id="DOC-003",
            name="No API documentation",
            passed=True,
            severity="medium",
            deduction=0,
            details="No API routes detected.",
        )

    return CheckResult(
        check_id="DOC-003",
        name="No API documentation",
        passed=has_docs,
        severity="medium",
        deduction=0 if has_docs else -10,
        details="" if has_docs else "API routes found but no OpenAPI/Swagger docs.",
        fix_guidance="Add API documentation using OpenAPI/Swagger auto-generated from route definitions." if not has_docs else "",
    )


def _check_doc004(repo_path: str) -> CheckResult:
    """DOC-004: Undocumented public functions (>50% undocumented)."""
    total_public = 0
    undocumented = 0

    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    total_public += 1
                    if not ast.get_docstring(node):
                        undocumented += 1
            elif isinstance(node, ast.ClassDef):
                for method in ast.iter_child_nodes(node):
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not method.name.startswith("_"):
                            total_public += 1
                            if not ast.get_docstring(method):
                                undocumented += 1

    if total_public == 0:
        return CheckResult(
            check_id="DOC-004",
            name="Undocumented public functions",
            passed=True,
            severity="medium",
            deduction=0,
            details="No public functions found.",
        )

    ratio = undocumented / total_public
    passed = ratio <= 0.5
    return CheckResult(
        check_id="DOC-004",
        name="Undocumented public functions",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -10,
        details=f"{undocumented}/{total_public} ({ratio:.0%}) public functions lack docstrings.",
        fix_guidance="Add docstrings to public functions with parameter descriptions and return types." if not passed else "",
    )


def _check_doc005(repo_path: str) -> CheckResult:
    """DOC-005: No ADR directory."""
    repo = Path(repo_path)
    for pattern in ("docs/adr", "docs/decisions", "adr", "decisions", "doc/adr", "doc/decisions"):
        if (repo / pattern).is_dir():
            return CheckResult(
                check_id="DOC-005",
                name="No ADR directory",
                passed=True,
                severity="low",
                deduction=0,
            )
    return CheckResult(
        check_id="DOC-005",
        name="No ADR directory",
        passed=False,
        severity="low",
        deduction=-5,
        details="No docs/adr/, docs/decisions/, or equivalent directory found.",
        fix_guidance="Create a docs/adr/ directory with at least one Architecture Decision Record.",
    )


def _check_doc006(repo_path: str) -> CheckResult:
    """DOC-006: No CHANGELOG."""
    repo = Path(repo_path)
    for name in ("CHANGELOG.md", "CHANGELOG", "CHANGES.md", "HISTORY.md", "changelog.md"):
        if (repo / name).exists():
            return CheckResult(
                check_id="DOC-006",
                name="No CHANGELOG",
                passed=True,
                severity="low",
                deduction=0,
            )
    return CheckResult(
        check_id="DOC-006",
        name="No CHANGELOG",
        passed=False,
        severity="low",
        deduction=-3,
        details="No CHANGELOG.md, CHANGES.md, or HISTORY.md found.",
        fix_guidance="Create a CHANGELOG.md using Keep a Changelog format with Added, Changed, Fixed sections.",
    )


def run_documentation_checks(repo_path: str) -> list[CheckResult]:
    """Run all 6 documentation checks against the repository."""
    return [
        _check_doc001(repo_path),
        _check_doc002(repo_path),
        _check_doc003(repo_path),
        _check_doc004(repo_path),
        _check_doc005(repo_path),
        _check_doc006(repo_path),
    ]
