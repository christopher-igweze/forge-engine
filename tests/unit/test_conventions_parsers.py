"""Tests for forge.conventions.parsers — deterministic config file parsers.

Tests all parsers with present, missing, and malformed config scenarios.
Uses tmp_path pytest fixture for isolated filesystem.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from forge.conventions.parsers import (
    parse_eslint,
    parse_flake8,
    parse_jest_config,
    parse_prettier,
    parse_pylintrc,
    parse_pyproject_toml,
    parse_tsconfig,
)


class TestParseEslint:
    """ESLint config parsing."""

    def test_eslintrc_json_with_disabled_rules(self, tmp_path):
        config = {
            "rules": {
                "no-console": "off",
                "semi": "error",
                "@typescript-eslint/no-unused-vars": "off",
            }
        }
        (tmp_path / ".eslintrc.json").write_text(json.dumps(config))

        result = parse_eslint(str(tmp_path))

        assert result["config_file"] == ".eslintrc.json"
        assert "@typescript-eslint/no-unused-vars" in result["disabled_rules"]
        assert "no-console" in result["disabled_rules"]
        assert "semi" not in result["disabled_rules"]

    def test_json_with_js_comments(self, tmp_path):
        content = textwrap.dedent("""\
        {
            // This is a line comment
            "rules": {
                "no-debugger": "off" /* inline block comment */
            }
        }
        """)
        (tmp_path / ".eslintrc.json").write_text(content)

        result = parse_eslint(str(tmp_path))

        assert result["disabled_rules"] == ["no-debugger"]
        assert result["config_file"] == ".eslintrc.json"

    def test_missing_config_returns_empty(self, tmp_path):
        result = parse_eslint(str(tmp_path))

        assert result["disabled_rules"] == []
        assert result["config_file"] == ""

    def test_numeric_off_zero(self, tmp_path):
        config = {
            "rules": {
                "no-alert": 0,
                "eqeqeq": 2,
                "curly": [0, "multi"],
            }
        }
        (tmp_path / ".eslintrc.json").write_text(json.dumps(config))

        result = parse_eslint(str(tmp_path))

        assert "no-alert" in result["disabled_rules"]
        assert "curly" in result["disabled_rules"]
        assert "eqeqeq" not in result["disabled_rules"]

    def test_package_json_eslint_config(self, tmp_path):
        pkg = {
            "name": "my-app",
            "eslintConfig": {
                "rules": {
                    "no-var": "off",
                    "prefer-const": "warn",
                }
            },
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        result = parse_eslint(str(tmp_path))

        assert result["config_file"] == "package.json[eslintConfig]"
        assert result["disabled_rules"] == ["no-var"]


class TestParseTsconfig:
    """TypeScript tsconfig.json parsing."""

    def test_strict_false(self, tmp_path):
        config = {"compilerOptions": {"strict": False, "target": "es2020"}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(config))

        result = parse_tsconfig(str(tmp_path))

        assert result["strict"] is False
        assert result["target"] == "es2020"
        assert result["config_file"] == "tsconfig.json"

    def test_strict_true(self, tmp_path):
        config = {"compilerOptions": {"strict": True, "jsx": "react-jsx"}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(config))

        result = parse_tsconfig(str(tmp_path))

        assert result["strict"] is True
        assert result["jsx"] == "react-jsx"

    def test_missing_config(self, tmp_path):
        result = parse_tsconfig(str(tmp_path))
        assert result == {}

    def test_tsconfig_with_trailing_commas(self, tmp_path):
        content = textwrap.dedent("""\
        {
            "compilerOptions": {
                "strict": true,
                "noImplicitAny": true,
            },
        }
        """)
        (tmp_path / "tsconfig.json").write_text(content)

        result = parse_tsconfig(str(tmp_path))

        assert result["strict"] is True
        assert result["no_implicit_any"] is True


class TestParsePyprojectToml:
    """pyproject.toml parsing for ruff, pytest, coverage."""

    def test_ruff_config(self, tmp_path):
        content = textwrap.dedent("""\
        [tool.ruff]
        line-length = 120
        target-version = "py311"

        [tool.ruff.lint]
        ignore = ["E501", "W291", "F401"]
        """)
        (tmp_path / "pyproject.toml").write_text(content)

        result = parse_pyproject_toml(str(tmp_path))

        lint = result["lint"]
        assert lint["tool"] == "ruff"
        assert lint["line_length"] == 120
        assert lint["target_version"] == "py311"
        assert set(lint["disabled_rules"]) == {"E501", "W291", "F401"}
        assert lint["config_file"] == "pyproject.toml[tool.ruff]"

    def test_pytest_markers(self, tmp_path):
        content = textwrap.dedent("""\
        [tool.pytest.ini_options]
        markers = [
            "slow: marks tests as slow",
            "integration: integration tests",
        ]
        testpaths = ["tests"]
        """)
        (tmp_path / "pyproject.toml").write_text(content)

        result = parse_pyproject_toml(str(tmp_path))

        test = result["test"]
        assert test["framework"] == "pytest"
        assert "slow" in test["custom_markers"]
        assert "integration" in test["custom_markers"]
        assert test["test_paths"] == ["tests"]

    def test_coverage_threshold(self, tmp_path):
        content = textwrap.dedent("""\
        [tool.pytest.ini_options]
        markers = ["unit: unit tests"]

        [tool.coverage.report]
        fail_under = 85.0
        """)
        (tmp_path / "pyproject.toml").write_text(content)

        result = parse_pyproject_toml(str(tmp_path))

        assert result["test"]["coverage_threshold"] == 85.0

    def test_missing_file(self, tmp_path):
        result = parse_pyproject_toml(str(tmp_path))
        assert result == {}

    def test_empty_file(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")

        result = parse_pyproject_toml(str(tmp_path))

        assert result == {}


class TestParsePylintrc:
    """Pylintrc parsing."""

    def test_disabled_rules(self, tmp_path):
        content = textwrap.dedent("""\
        [MESSAGES CONTROL]
        disable = missing-docstring,
            too-many-arguments,
            broad-exception-caught
        """)
        (tmp_path / ".pylintrc").write_text(content)

        result = parse_pylintrc(str(tmp_path))

        assert result["tool"] == "pylint"
        assert "missing-docstring" in result["disabled_rules"]
        assert "too-many-arguments" in result["disabled_rules"]
        assert "broad-exception-caught" in result["disabled_rules"]
        assert result["config_file"] == ".pylintrc"

    def test_missing_file(self, tmp_path):
        result = parse_pylintrc(str(tmp_path))
        assert result == {}


class TestParseFlake8:
    """Flake8 config parsing."""

    def test_ignore_rules_with_line_length(self, tmp_path):
        content = textwrap.dedent("""\
        [flake8]
        ignore = E501, W503, E203
        max-line-length = 100
        """)
        (tmp_path / ".flake8").write_text(content)

        result = parse_flake8(str(tmp_path))

        assert result["tool"] == "flake8"
        assert set(result["disabled_rules"]) == {"E501", "W503", "E203"}
        assert result["line_length"] == 100
        assert result["config_file"] == ".flake8"

    def test_setup_cfg_fallback(self, tmp_path):
        content = textwrap.dedent("""\
        [flake8]
        ignore = W504
        max-line-length = 88
        """)
        (tmp_path / "setup.cfg").write_text(content)

        result = parse_flake8(str(tmp_path))

        assert result["config_file"] == "setup.cfg[flake8]"
        assert "W504" in result["disabled_rules"]
        assert result["line_length"] == 88


class TestParseJestConfig:
    """Jest config parsing."""

    def test_package_json_with_jest_config(self, tmp_path):
        pkg = {
            "name": "test-app",
            "jest": {
                "roots": ["<rootDir>/src"],
                "coverageThreshold": {
                    "global": {"lines": 80}
                },
            },
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        result = parse_jest_config(str(tmp_path))

        assert result["framework"] == "jest"
        assert result["config_file"] == "package.json[jest]"
        assert result["test_paths"] == ["<rootDir>/src"]
        assert result["coverage_threshold"] == 80.0


class TestParsePrettier:
    """Prettier detection."""

    def test_prettierrc_present(self, tmp_path):
        (tmp_path / ".prettierrc").write_text("{}")

        result = parse_prettier(str(tmp_path))

        assert result == "prettier"

    def test_package_json_dep(self, tmp_path):
        pkg = {
            "name": "my-app",
            "devDependencies": {"prettier": "^3.0.0"},
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        result = parse_prettier(str(tmp_path))

        assert result == "prettier"

    def test_not_present(self, tmp_path):
        result = parse_prettier(str(tmp_path))
        assert result == ""
