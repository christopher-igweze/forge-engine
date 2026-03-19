"""Deterministic evaluation checks for FORGE v3."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class CheckResult:
    check_id: str
    name: str
    passed: bool
    severity: str
    deduction: int
    locations: list[dict] = field(default_factory=list)
    details: str = ""
    stride: str = ""
    asvs_ref: str = ""


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "vendor",
    ".forge-artifacts", ".forge-worktrees", "dist", "build", ".next",
    ".tox", ".mypy_cache", ".pytest_cache", "egg-info",
}

_SOURCE_EXTENSIONS = (".py", ".js", ".ts", ".jsx", ".tsx")


def iter_source_files(
    repo_path: str,
    extensions: tuple[str, ...] = (".py",),
    include_tests: bool = False,
) -> Iterator[Path]:
    """Yield source files, skipping vendor/build dirs."""
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if any(f.endswith(ext) for ext in extensions):
                p = Path(root) / f
                if include_tests or not is_test_file(p):
                    yield p


def is_test_file(path: Path) -> bool:
    name = path.name
    parts = path.parts
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".spec." in name
        or ".test." in name
        or "__tests__" in parts
        or "tests" in parts
        or "test" in parts
    )


def read_file_safe(path: Path) -> str:
    """Read file contents, returning empty string on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def parse_ast_safe(source: str, filename: str = "<unknown>"):
    """Parse Python source to AST, returning None on failure."""
    import ast
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError:
        return None


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

from forge.evaluation.checks.security import run_security_checks
from forge.evaluation.checks.reliability import run_reliability_checks
from forge.evaluation.checks.maintainability import run_maintainability_checks
from forge.evaluation.checks.test_quality import run_test_quality_checks
from forge.evaluation.checks.performance import run_performance_checks
from forge.evaluation.checks.documentation import run_documentation_checks
from forge.evaluation.checks.operations import run_operations_checks

__all__ = [
    "CheckResult",
    "iter_source_files",
    "is_test_file",
    "read_file_safe",
    "parse_ast_safe",
    "run_security_checks",
    "run_reliability_checks",
    "run_maintainability_checks",
    "run_test_quality_checks",
    "run_performance_checks",
    "run_documentation_checks",
    "run_operations_checks",
]
