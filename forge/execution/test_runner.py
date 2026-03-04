"""Test execution infrastructure for FORGE remediation.

Detects the project's test framework, ensures the runner is installed,
executes generated tests in worktrees, and parses results.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TestExecutionResult:
    """Result of running tests in a worktree."""
    success: bool = False
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    error_output: str = ""
    framework: str = ""


def detect_test_framework(worktree_path: str) -> str:
    """Detect the test framework used in the project.

    Returns: "jest" | "mocha" | "vitest" | "pytest" | ""
    """
    root = Path(worktree_path)

    # Check for Python test frameworks
    for name in ("pytest.ini", "setup.cfg", "pyproject.toml"):
        cfg = root / name
        if cfg.exists():
            try:
                text = cfg.read_text(errors="replace")
                if "pytest" in text:
                    return "pytest"
            except OSError:
                pass

    # Check for Node.js test frameworks via package.json
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(errors="replace"))
            all_deps = {
                **data.get("dependencies", {}),
                **data.get("devDependencies", {}),
            }
            # Check scripts for framework hints
            scripts = data.get("scripts", {})
            test_script = scripts.get("test", "")

            # Order matters: check specific frameworks first
            if "vitest" in all_deps or "vitest" in test_script:
                return "vitest"
            if "jest" in all_deps or "jest" in test_script:
                return "jest"
            if "mocha" in all_deps or "mocha" in test_script:
                return "mocha"

            # Check for jest config in package.json
            if data.get("jest"):
                return "jest"
        except (OSError, json.JSONDecodeError):
            pass

    # Check for jest config files
    for name in ("jest.config.js", "jest.config.ts", "jest.config.mjs"):
        if (root / name).exists():
            return "jest"

    # Check for vitest config
    for name in ("vitest.config.js", "vitest.config.ts", "vitest.config.mjs"):
        if (root / name).exists():
            return "vitest"

    # Check for Python test files
    for test_dir in ("tests", "test"):
        if (root / test_dir).is_dir():
            test_files = list((root / test_dir).glob("test_*.py"))
            if test_files:
                return "pytest"

    return ""


def ensure_test_runner(worktree_path: str, framework: str) -> bool:
    """Ensure the test runner is installed. Returns True if ready.

    For Node.js: installs the framework as devDependency if missing.
    For Python: pytest is assumed available in the environment.
    """
    if framework == "pytest":
        return True  # Assume pytest available in env

    if framework in ("jest", "mocha", "vitest"):
        root = Path(worktree_path)
        pkg_json = root / "package.json"
        if not pkg_json.exists():
            return False

        try:
            data = json.loads(pkg_json.read_text(errors="replace"))
            all_deps = {
                **data.get("dependencies", {}),
                **data.get("devDependencies", {}),
            }
            if framework not in all_deps:
                logger.info("Installing %s in worktree", framework)
                subprocess.run(
                    ["npm", "install", "--save-dev", framework],
                    cwd=worktree_path,
                    capture_output=True,
                    timeout=60,
                )
        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            logger.warning("Failed to ensure test runner %s: %s", framework, e)
            return False

    return True


def run_tests_in_worktree(
    worktree_path: str,
    test_files: list[str] | None = None,
    timeout: int = 120,
) -> TestExecutionResult | None:
    """Execute tests in a worktree and return results.

    Args:
        worktree_path: Path to the git worktree.
        test_files: Specific test files to run. If None, runs all tests.
        timeout: Maximum time in seconds for test execution.

    Returns:
        TestExecutionResult or None if no framework detected.
    """
    framework = detect_test_framework(worktree_path)
    if not framework:
        logger.debug("No test framework detected in %s", worktree_path)
        return None

    if not ensure_test_runner(worktree_path, framework):
        return TestExecutionResult(
            success=False,
            error_output=f"Failed to ensure {framework} is installed",
            framework=framework,
        )

    try:
        if framework == "jest":
            return _run_jest(worktree_path, test_files, timeout)
        elif framework == "vitest":
            return _run_vitest(worktree_path, test_files, timeout)
        elif framework == "mocha":
            return _run_mocha(worktree_path, test_files, timeout)
        elif framework == "pytest":
            return _run_pytest(worktree_path, test_files, timeout)
    except subprocess.TimeoutExpired:
        return TestExecutionResult(
            success=False,
            error_output=f"Tests timed out after {timeout}s",
            framework=framework,
        )
    except Exception as e:
        return TestExecutionResult(
            success=False,
            error_output=str(e)[:500],
            framework=framework,
        )

    return None


def _run_jest(
    worktree_path: str,
    test_files: list[str] | None,
    timeout: int,
) -> TestExecutionResult:
    """Run Jest tests and parse JSON output."""
    cmd = ["npx", "jest", "--json", "--forceExit", "--no-coverage"]
    if test_files:
        cmd.extend(test_files)

    proc = subprocess.run(
        cmd,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    # Jest outputs JSON to stdout even on failure
    try:
        result_data = json.loads(proc.stdout)
        return TestExecutionResult(
            success=result_data.get("success", False),
            tests_run=result_data.get("numTotalTests", 0),
            tests_passed=result_data.get("numPassedTests", 0),
            tests_failed=result_data.get("numFailedTests", 0),
            error_output=proc.stderr[:500] if proc.stderr else "",
            framework="jest",
        )
    except json.JSONDecodeError:
        # Fallback: parse text output
        return _parse_text_output(proc, "jest")


def _run_vitest(
    worktree_path: str,
    test_files: list[str] | None,
    timeout: int,
) -> TestExecutionResult:
    """Run Vitest tests."""
    cmd = ["npx", "vitest", "run", "--reporter=json"]
    if test_files:
        cmd.extend(test_files)

    proc = subprocess.run(
        cmd,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    try:
        result_data = json.loads(proc.stdout)
        return TestExecutionResult(
            success=result_data.get("success", proc.returncode == 0),
            tests_run=result_data.get("numTotalTests", 0),
            tests_passed=result_data.get("numPassedTests", 0),
            tests_failed=result_data.get("numFailedTests", 0),
            error_output=proc.stderr[:500] if proc.stderr else "",
            framework="vitest",
        )
    except json.JSONDecodeError:
        return _parse_text_output(proc, "vitest")


def _run_mocha(
    worktree_path: str,
    test_files: list[str] | None,
    timeout: int,
) -> TestExecutionResult:
    """Run Mocha tests."""
    cmd = ["npx", "mocha", "--reporter", "json"]
    if test_files:
        cmd.extend(test_files)

    proc = subprocess.run(
        cmd,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    try:
        result_data = json.loads(proc.stdout)
        stats = result_data.get("stats", {})
        return TestExecutionResult(
            success=stats.get("failures", 1) == 0,
            tests_run=stats.get("tests", 0),
            tests_passed=stats.get("passes", 0),
            tests_failed=stats.get("failures", 0),
            error_output=proc.stderr[:500] if proc.stderr else "",
            framework="mocha",
        )
    except json.JSONDecodeError:
        return _parse_text_output(proc, "mocha")


def _run_pytest(
    worktree_path: str,
    test_files: list[str] | None,
    timeout: int,
) -> TestExecutionResult:
    """Run pytest and parse output."""
    cmd = ["python", "-m", "pytest", "-q", "--tb=short", "--no-header"]
    if test_files:
        cmd.extend(test_files)

    proc = subprocess.run(
        cmd,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    # Parse pytest summary line: "X passed, Y failed"
    import re
    output = proc.stdout + proc.stderr
    passed = 0
    failed = 0

    m = re.search(r'(\d+) passed', output)
    if m:
        passed = int(m.group(1))
    m = re.search(r'(\d+) failed', output)
    if m:
        failed = int(m.group(1))

    return TestExecutionResult(
        success=proc.returncode == 0,
        tests_run=passed + failed,
        tests_passed=passed,
        tests_failed=failed,
        error_output=output[-500:] if proc.returncode != 0 else "",
        framework="pytest",
    )


def _parse_text_output(
    proc: subprocess.CompletedProcess,
    framework: str,
) -> TestExecutionResult:
    """Fallback parser for text output when JSON parsing fails."""
    import re
    output = proc.stdout + proc.stderr

    passed = 0
    failed = 0

    # Try common patterns
    m = re.search(r'(\d+)\s+(?:passing|passed)', output)
    if m:
        passed = int(m.group(1))
    m = re.search(r'(\d+)\s+(?:failing|failed)', output)
    if m:
        failed = int(m.group(1))

    return TestExecutionResult(
        success=proc.returncode == 0,
        tests_run=passed + failed,
        tests_passed=passed,
        tests_failed=failed,
        error_output=output[-500:] if proc.returncode != 0 else "",
        framework=framework,
    )
