"""Operations dimension checks (OPS-001 through OPS-006)."""

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

_ENV_ACCESS = re.compile(
    r"""os\.(?:getenv|environ\.get|environ\[)\s*\(?\s*["']""",
)
_ENV_VALIDATION = re.compile(
    r"""(?:BaseSettings|Pydantic|pydantic_settings|if\s+not\s+|or\s+["']|\.get\s*\([^,]+,\s*["'])""",
    re.IGNORECASE,
)


def _check_ops001(repo_path: str) -> CheckResult:
    """OPS-001: No CI/CD configuration."""
    repo = Path(repo_path)
    ci_indicators = [
        ".github/workflows",
        ".gitlab-ci.yml",
        "Jenkinsfile",
        ".circleci",
        "bitbucket-pipelines.yml",
        ".travis.yml",
        "azure-pipelines.yml",
    ]
    for ci in ci_indicators:
        p = repo / ci
        if p.exists():
            # For directories, check they contain files
            if p.is_dir():
                if any(p.iterdir()):
                    return CheckResult(
                        check_id="OPS-001",
                        name="No CI/CD configuration",
                        passed=True,
                        severity="high",
                        deduction=0,
                    )
            else:
                return CheckResult(
                    check_id="OPS-001",
                    name="No CI/CD configuration",
                    passed=True,
                    severity="high",
                    deduction=0,
                )
    return CheckResult(
        check_id="OPS-001",
        name="No CI/CD configuration",
        passed=False,
        severity="high",
        deduction=-25,
        details="No .github/workflows, .gitlab-ci.yml, Jenkinsfile, or equivalent found.",
    )


def _check_ops002(repo_path: str) -> CheckResult:
    """OPS-002: No container config."""
    repo = Path(repo_path)
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (repo / name).exists():
            return CheckResult(
                check_id="OPS-002",
                name="No container config",
                passed=True,
                severity="medium",
                deduction=0,
            )
    return CheckResult(
        check_id="OPS-002",
        name="No container config",
        passed=False,
        severity="medium",
        deduction=-10,
        details="No Dockerfile or docker-compose.yml found.",
    )


def _check_ops003(repo_path: str) -> CheckResult:
    """OPS-003: No structured logging (>5 raw prints in non-test source)."""
    print_count = 0
    has_structured = False
    print_locations = []

    for path in iter_source_files(repo_path, extensions=(".py",)):
        if is_test_file(path):
            continue
        content = read_file_safe(path)

        # Check for structured logging
        if re.search(r"""(?:logging\.|structlog\.|loguru\.|getLogger)""", content):
            has_structured = True

        # Count raw prints
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
                print_count += 1
                if len(print_locations) < 10:
                    print_locations.append({
                        "file": str(path),
                        "line": node.lineno,
                        "snippet": f"print() call",
                    })

    passed = print_count <= 5 or has_structured
    return CheckResult(
        check_id="OPS-003",
        name="No structured logging",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -10,
        locations=print_locations if not passed else [],
        details=f"{print_count} raw print() call(s) without structured logging." if not passed else "",
    )


def _check_ops004(repo_path: str) -> CheckResult:
    """OPS-004: No env validation."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _ENV_ACCESS.search(line):
                context = "\n".join(lines[max(0, i - 2):i + 3])
                if not _ENV_VALIDATION.search(context):
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    deduction = max(-15, -5 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="OPS-004",
        name="No env validation",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} env access(es) without validation." if locations else "",
    )


def _check_ops005(repo_path: str) -> CheckResult:
    """OPS-005: No .env.example."""
    repo = Path(repo_path)
    for name in (".env.example", ".env.sample", ".env.template", "env.example"):
        if (repo / name).exists():
            return CheckResult(
                check_id="OPS-005",
                name="No .env.example",
                passed=True,
                severity="low",
                deduction=0,
            )
    return CheckResult(
        check_id="OPS-005",
        name="No .env.example",
        passed=False,
        severity="low",
        deduction=-5,
        details="No .env.example, .env.sample, or .env.template found.",
    )


def _check_ops006(repo_path: str) -> CheckResult:
    """OPS-006: No linter config."""
    repo = Path(repo_path)

    # Check standalone files
    linter_files = [
        ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml", ".eslintrc",
        "ruff.toml", ".flake8", ".pylintrc", "biome.json", "biome.jsonc",
        ".prettierrc", ".prettierrc.json", "eslint.config.js", "eslint.config.mjs",
    ]
    for lf in linter_files:
        if (repo / lf).exists():
            return CheckResult(
                check_id="OPS-006",
                name="No linter config",
                passed=True,
                severity="low",
                deduction=0,
            )

    # Check pyproject.toml for ruff/flake8/pylint config
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        content = read_file_safe(pyproject)
        if re.search(r"\[tool\.(?:ruff|flake8|pylint|isort|black)\]", content):
            return CheckResult(
                check_id="OPS-006",
                name="No linter config",
                passed=True,
                severity="low",
                deduction=0,
            )

    return CheckResult(
        check_id="OPS-006",
        name="No linter config",
        passed=False,
        severity="low",
        deduction=-5,
        details="No linter configuration found.",
    )


def run_operations_checks(repo_path: str) -> list[CheckResult]:
    """Run all 6 operations checks against the repository."""
    return [
        _check_ops001(repo_path),
        _check_ops002(repo_path),
        _check_ops003(repo_path),
        _check_ops004(repo_path),
        _check_ops005(repo_path),
        _check_ops006(repo_path),
    ]
