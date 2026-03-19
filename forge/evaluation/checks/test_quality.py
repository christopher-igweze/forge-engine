"""Test quality dimension checks (TST-001 through TST-007)."""

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
    _SKIP_DIRS,
)


def _find_test_files(repo_path: str) -> list[Path]:
    """Find all test files in the repository."""
    test_files = []
    for root, dirs, files in __import__("os").walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix in (".py", ".js", ".ts", ".jsx", ".tsx") and is_test_file(p):
                test_files.append(p)
    return test_files


def _check_tst001(repo_path: str) -> CheckResult:
    """TST-001: No test files present."""
    test_files = _find_test_files(repo_path)
    passed = len(test_files) > 0
    return CheckResult(
        check_id="TST-001",
        name="No test files",
        passed=passed,
        severity="critical",
        deduction=0 if passed else -40,
        details=f"Found {len(test_files)} test file(s)." if passed else "No test files found.",
    )


def _check_tst002(repo_path: str) -> CheckResult:
    """TST-002: Only one test type (need both unit and integration)."""
    has_unit = False
    has_integration = False
    repo = Path(repo_path)

    # Check directory structure
    for pattern in ["tests/unit", "test/unit", "tests/unit_tests", "__tests__/unit"]:
        if (repo / pattern).is_dir():
            has_unit = True
            break

    for pattern in ["tests/integration", "test/integration", "tests/e2e", "tests/functional"]:
        if (repo / pattern).is_dir():
            has_integration = True
            break

    # Fallback: check file naming
    if not has_unit or not has_integration:
        for tf in _find_test_files(repo_path):
            name_lower = str(tf).lower()
            if "unit" in name_lower:
                has_unit = True
            if any(k in name_lower for k in ("integration", "e2e", "functional")):
                has_integration = True

    passed = has_unit and has_integration
    return CheckResult(
        check_id="TST-002",
        name="Only one test type",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -10,
        details="" if passed else "Missing unit and/or integration test separation.",
    )


def _check_tst003(repo_path: str) -> CheckResult:
    """TST-003: Empty test functions (no assert/expect/mock)."""
    locations = []
    _ASSERT_PATTERNS = {"assert", "Assert", "expect", "should", "mock", "patch", "raises", "assertEqual"}

    for tf in _find_test_files(repo_path):
        if not tf.suffix == ".py":
            continue
        content = read_file_safe(tf)
        tree = parse_ast_safe(content, str(tf))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                # Check body for assertions
                body_src = ast.dump(node)
                if not any(pat in body_src for pat in _ASSERT_PATTERNS):
                    # Also check for assert statements
                    has_assert = any(isinstance(s, ast.Assert) for s in ast.walk(node))
                    if not has_assert:
                        locations.append({
                            "file": str(tf),
                            "line": node.lineno,
                            "snippet": f"def {node.name}() — no assertions",
                        })
    deduction = max(-15, -5 * len(locations))
    passed = len(locations) == 0
    return CheckResult(
        check_id="TST-003",
        name="Empty test functions",
        passed=passed,
        severity="medium",
        deduction=0 if passed else deduction,
        locations=locations,
        details=f"{len(locations)} test function(s) with no assertions." if locations else "",
    )


def _check_tst004(repo_path: str) -> CheckResult:
    """TST-004: No tests for critical paths."""
    critical_keywords = {"auth", "login", "payment", "checkout", "password", "register"}
    critical_sources = []
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        name_lower = path.stem.lower()
        if any(kw in name_lower for kw in critical_keywords):
            critical_sources.append(path)

    if not critical_sources:
        return CheckResult(
            check_id="TST-004",
            name="No tests for critical paths",
            passed=True,
            severity="high",
            deduction=0,
            details="No critical path source files detected.",
        )

    test_files = _find_test_files(repo_path)
    test_names = {tf.stem.lower() for tf in test_files}
    test_content = "\n".join(read_file_safe(tf).lower() for tf in test_files)

    missing = []
    for src in critical_sources:
        stem = src.stem.lower()
        # Check if there's a test file for this or if test content mentions it
        has_test = (
            f"test_{stem}" in test_names
            or f"{stem}_test" in test_names
            or stem in test_content
        )
        if not has_test:
            missing.append({
                "file": str(src),
                "line": 1,
                "snippet": f"No tests found for critical file: {src.name}",
            })

    passed = len(missing) == 0
    return CheckResult(
        check_id="TST-004",
        name="No tests for critical paths",
        passed=passed,
        severity="high",
        deduction=0 if passed else -10,
        locations=missing,
        details=f"{len(missing)} critical source file(s) lack test coverage." if missing else "",
    )


def _check_tst005(repo_path: str) -> CheckResult:
    """TST-005: Low test-to-source ratio (<0.3)."""
    source_count = sum(1 for _ in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")))
    test_count = len(_find_test_files(repo_path))

    if source_count == 0:
        return CheckResult(
            check_id="TST-005",
            name="Low test-to-source ratio",
            passed=True,
            severity="medium",
            deduction=0,
            details="No source files found.",
        )

    ratio = test_count / source_count
    passed = ratio >= 0.3
    return CheckResult(
        check_id="TST-005",
        name="Low test-to-source ratio",
        passed=passed,
        severity="medium",
        deduction=0 if passed else -5,
        details=f"Test-to-source ratio: {ratio:.2f} ({test_count} tests / {source_count} sources).",
    )


def _check_tst006(repo_path: str) -> CheckResult:
    """TST-006: No test configuration."""
    repo = Path(repo_path)
    config_files = [
        "pytest.ini", "jest.config.js", "jest.config.ts", "jest.config.mjs",
        ".mocharc.yml", ".mocharc.json", "vitest.config.ts", "vitest.config.js",
    ]
    for cf in config_files:
        if (repo / cf).exists():
            return CheckResult(
                check_id="TST-006",
                name="No test configuration",
                passed=True,
                severity="low",
                deduction=0,
            )

    # Check pyproject.toml and setup.cfg
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        content = read_file_safe(pyproject)
        if "[tool.pytest" in content:
            return CheckResult(
                check_id="TST-006",
                name="No test configuration",
                passed=True,
                severity="low",
                deduction=0,
            )
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        content = read_file_safe(setup_cfg)
        if "[tool:pytest]" in content:
            return CheckResult(
                check_id="TST-006",
                name="No test configuration",
                passed=True,
                severity="low",
                deduction=0,
            )

    return CheckResult(
        check_id="TST-006",
        name="No test configuration",
        passed=False,
        severity="low",
        deduction=-3,
        details="No pytest.ini, jest.config, pyproject.toml [tool.pytest], or equivalent found.",
    )


def _check_tst007(repo_path: str) -> CheckResult:
    """TST-007: Low coverage threshold (<60%)."""
    repo = Path(repo_path)

    # Check pyproject.toml
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        content = read_file_safe(pyproject)
        match = re.search(r"fail_under\s*=\s*(\d+)", content)
        if match:
            threshold = int(match.group(1))
            passed = threshold >= 60
            return CheckResult(
                check_id="TST-007",
                name="Low coverage threshold",
                passed=passed,
                severity="medium",
                deduction=0 if passed else -5,
                details=f"Coverage threshold: {threshold}%.",
            )

    # Check pytest.ini
    pytest_ini = repo / "pytest.ini"
    if pytest_ini.exists():
        content = read_file_safe(pytest_ini)
        match = re.search(r"--cov-fail-under[=\s]+(\d+)", content)
        if match:
            threshold = int(match.group(1))
            passed = threshold >= 60
            return CheckResult(
                check_id="TST-007",
                name="Low coverage threshold",
                passed=passed,
                severity="medium",
                deduction=0 if passed else -5,
                details=f"Coverage threshold: {threshold}%.",
            )

    # Check jest config for coverageThreshold
    for jest_file in ["jest.config.js", "jest.config.ts", "jest.config.mjs"]:
        p = repo / jest_file
        if p.exists():
            content = read_file_safe(p)
            match = re.search(r"coverageThreshold.*?global.*?(?:lines|statements)\s*:\s*(\d+)", content, re.DOTALL)
            if match:
                threshold = int(match.group(1))
                passed = threshold >= 60
                return CheckResult(
                    check_id="TST-007",
                    name="Low coverage threshold",
                    passed=passed,
                    severity="medium",
                    deduction=0 if passed else -5,
                    details=f"Coverage threshold: {threshold}%.",
                )

    # No coverage threshold configured at all
    return CheckResult(
        check_id="TST-007",
        name="Low coverage threshold",
        passed=False,
        severity="medium",
        deduction=-5,
        details="No coverage threshold configured.",
    )


def run_test_quality_checks(repo_path: str) -> list[CheckResult]:
    """Run all 7 test quality checks against the repository."""
    return [
        _check_tst001(repo_path),
        _check_tst002(repo_path),
        _check_tst003(repo_path),
        _check_tst004(repo_path),
        _check_tst005(repo_path),
        _check_tst006(repo_path),
        _check_tst007(repo_path),
    ]
