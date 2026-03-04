"""Unit tests for forge.execution.test_runner."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from forge.execution.test_runner import (
    TestExecutionResult,
    detect_test_framework,
    ensure_test_runner,
    run_tests_in_worktree,
    _parse_text_output,
)


class TestDetectFramework:
    """Tests for detect_test_framework()."""

    def test_detects_jest_from_package_json_deps(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "jest"

    def test_detects_jest_from_config_file(self, tmp_path):
        (tmp_path / "jest.config.js").write_text("module.exports = {}")
        assert detect_test_framework(str(tmp_path)) == "jest"

    def test_detects_jest_from_package_json_jest_key(self, tmp_path):
        pkg = {"jest": {"testEnvironment": "node"}, "dependencies": {}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "jest"

    def test_detects_vitest_from_deps(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^1.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "vitest"

    def test_detects_vitest_from_config(self, tmp_path):
        (tmp_path / "vitest.config.ts").write_text("export default {}")
        assert detect_test_framework(str(tmp_path)) == "vitest"

    def test_detects_mocha_from_deps(self, tmp_path):
        pkg = {"devDependencies": {"mocha": "^10.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "mocha"

    def test_detects_pytest_from_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
        assert detect_test_framework(str(tmp_path)) == "pytest"

    def test_detects_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\n')
        assert detect_test_framework(str(tmp_path)) == "pytest"

    def test_detects_pytest_from_test_files(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_example.py").write_text("def test_foo(): pass")
        assert detect_test_framework(str(tmp_path)) == "pytest"

    def test_detects_jest_from_test_script(self, tmp_path):
        pkg = {"scripts": {"test": "jest --coverage"}, "dependencies": {}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "jest"

    def test_returns_empty_for_no_framework(self, tmp_path):
        assert detect_test_framework(str(tmp_path)) == ""

    def test_vitest_preferred_over_jest(self, tmp_path):
        """When both vitest and jest are deps, vitest wins."""
        pkg = {"devDependencies": {"vitest": "^1.0.0", "jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(str(tmp_path)) == "vitest"


class TestEnsureTestRunner:
    """Tests for ensure_test_runner()."""

    def test_pytest_always_returns_true(self, tmp_path):
        assert ensure_test_runner(str(tmp_path), "pytest") is True

    def test_jest_returns_false_no_package_json(self, tmp_path):
        assert ensure_test_runner(str(tmp_path), "jest") is False

    @patch("subprocess.run")
    def test_jest_installs_if_missing(self, mock_run, tmp_path):
        pkg = {"dependencies": {}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        ensure_test_runner(str(tmp_path), "jest")
        mock_run.assert_called_once()
        assert "jest" in mock_run.call_args[0][0]

    def test_jest_skips_install_if_present(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        with patch("subprocess.run") as mock_run:
            ensure_test_runner(str(tmp_path), "jest")
            mock_run.assert_not_called()


class TestRunTests:
    """Tests for run_tests_in_worktree()."""

    def test_returns_none_no_framework(self, tmp_path):
        result = run_tests_in_worktree(str(tmp_path))
        assert result is None

    @patch("forge.execution.test_runner._run_jest")
    def test_dispatches_to_jest(self, mock_jest, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        mock_jest.return_value = TestExecutionResult(success=True, framework="jest")

        result = run_tests_in_worktree(str(tmp_path))
        assert result.success is True
        mock_jest.assert_called_once()

    @patch("forge.execution.test_runner._run_pytest")
    def test_dispatches_to_pytest(self, mock_pytest, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        mock_pytest.return_value = TestExecutionResult(success=True, framework="pytest")

        result = run_tests_in_worktree(str(tmp_path))
        assert result.success is True
        mock_pytest.assert_called_once()


class TestParseTextOutput:
    """Tests for _parse_text_output fallback."""

    def test_parses_passing_count(self):
        proc = MagicMock()
        proc.stdout = "3 passing (1s)\n"
        proc.stderr = ""
        proc.returncode = 0

        result = _parse_text_output(proc, "mocha")
        assert result.tests_passed == 3
        assert result.success is True

    def test_parses_failing_count(self):
        proc = MagicMock()
        proc.stdout = "2 passing\n1 failing\n"
        proc.stderr = ""
        proc.returncode = 1

        result = _parse_text_output(proc, "jest")
        assert result.tests_passed == 2
        assert result.tests_failed == 1
        assert result.success is False

    def test_handles_empty_output(self):
        proc = MagicMock()
        proc.stdout = ""
        proc.stderr = ""
        proc.returncode = 1

        result = _parse_text_output(proc, "jest")
        assert result.success is False
        assert result.tests_run == 0
