"""Tests for ConventionsExtractor and build_conventions_context_string.

Covers extraction orchestration and prompt-injectable formatting.
Uses tmp_path pytest fixture for isolated filesystem.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from forge.conventions.extractor import ConventionsExtractor
from forge.conventions.formatter import build_conventions_context_string
from forge.conventions.models import (
    LintConventions,
    ProjectConventions,
    QAConventions,
    TypeScriptConventions,
)


class TestConventionsExtractor:
    """Orchestration of all parsers via ConventionsExtractor."""

    def test_empty_repo(self, tmp_path):
        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert conventions.is_empty
        assert conventions.config_files_found == []
        assert conventions.lint.tool == ""
        assert conventions.test.framework == ""

    def test_full_python_repo(self, tmp_path):
        content = textwrap.dedent("""\
        [tool.ruff]
        line-length = 120

        [tool.ruff.lint]
        ignore = ["E501", "W291"]

        [tool.pytest.ini_options]
        markers = [
            "slow: marks tests as slow",
            "integration: integration tests",
        ]
        testpaths = ["tests"]

        [tool.coverage.report]
        fail_under = 90
        """)
        (tmp_path / "pyproject.toml").write_text(content)

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert not conventions.is_empty
        assert conventions.lint.tool == "ruff"
        assert "E501" in conventions.lint.disabled_rules
        assert "W291" in conventions.lint.disabled_rules
        assert conventions.lint.line_length == 120

        assert conventions.test.framework == "pytest"
        assert "slow" in conventions.test.custom_markers
        assert "integration" in conventions.test.custom_markers
        assert conventions.test.coverage_threshold == 90.0

    def test_js_ts_repo(self, tmp_path):
        eslintrc = {
            "rules": {
                "no-console": "off",
                "@typescript-eslint/no-explicit-any": "off",
            }
        }
        (tmp_path / ".eslintrc.json").write_text(json.dumps(eslintrc))

        tsconfig = {
            "compilerOptions": {
                "strict": True,
                "noImplicitAny": True,
                "target": "es2022",
                "jsx": "react-jsx",
            }
        }
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert not conventions.is_empty
        assert conventions.lint.tool == "eslint"
        assert "no-console" in conventions.lint.disabled_rules

        assert conventions.typescript.strict is True
        assert conventions.typescript.no_implicit_any is True
        assert conventions.typescript.target == "es2022"
        assert conventions.typescript.jsx == "react-jsx"

    def test_conftest_fallback_detection(self, tmp_path):
        (tmp_path / "conftest.py").write_text("# conftest")

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert conventions.test.framework == "pytest"
        assert conventions.test.config_file == "(detected from conftest.py)"

    def test_conftest_in_tests_subdir(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "conftest.py").write_text("# conftest")

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert conventions.test.framework == "pytest"


class TestBuildConventionsContextString:
    """Formatter that builds prompt-injectable context strings."""

    def test_empty_conventions_returns_empty_string(self):
        conventions = ProjectConventions()
        result = build_conventions_context_string(conventions)
        assert result == ""

    def test_xml_wrapper_tags_present(self):
        conventions = ProjectConventions(
            lint=LintConventions(tool="ruff", config_file="pyproject.toml[tool.ruff]"),
            config_files_found=["pyproject.toml[tool.ruff]"],
        )

        result = build_conventions_context_string(conventions)

        assert result.startswith("<project_conventions>")
        assert result.endswith("</project_conventions>")

    def test_disabled_rules_appear_in_output(self):
        conventions = ProjectConventions(
            lint=LintConventions(
                tool="eslint",
                disabled_rules=["no-console", "no-debugger"],
                config_file=".eslintrc.json",
            ),
            config_files_found=[".eslintrc.json"],
        )

        result = build_conventions_context_string(conventions)

        assert "no-console" in result
        assert "no-debugger" in result

    def test_typescript_strict_false_output(self):
        conventions = ProjectConventions(
            typescript=TypeScriptConventions(
                strict=False,
                config_file="tsconfig.json",
            ),
            config_files_found=["tsconfig.json"],
        )

        result = build_conventions_context_string(conventions)

        assert "strict mode: disabled" in result
        assert "do NOT flag" in result

    def test_test_markers_in_output(self):
        conventions = ProjectConventions(
            test=QAConventions(
                framework="pytest",
                custom_markers=["slow", "integration"],
                config_file="pyproject.toml[tool.pytest]",
            ),
            config_files_found=["pyproject.toml[tool.pytest]"],
        )

        result = build_conventions_context_string(conventions)

        assert "slow" in result
        assert "integration" in result
        assert "Framework: pytest" in result

    def test_do_not_flag_instruction_present(self):
        conventions = ProjectConventions(
            lint=LintConventions(
                tool="ruff",
                disabled_rules=["E501"],
                config_file="pyproject.toml[tool.ruff]",
            ),
            config_files_found=["pyproject.toml[tool.ruff]"],
        )

        result = build_conventions_context_string(conventions)

        assert "DO NOT flag" in result

    def test_rules_capped_at_20(self):
        many_rules = [f"RULE{i:03d}" for i in range(30)]
        conventions = ProjectConventions(
            lint=LintConventions(
                tool="ruff",
                disabled_rules=many_rules,
                config_file="pyproject.toml[tool.ruff]",
            ),
            config_files_found=["pyproject.toml[tool.ruff]"],
        )

        result = build_conventions_context_string(conventions)

        # First 20 should be present, the rest should not
        assert "RULE000" in result
        assert "RULE019" in result
        assert "RULE020" not in result
        assert "RULE029" not in result
