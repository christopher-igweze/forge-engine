"""Tests for ConventionsExtractor and build_conventions_context_string.

Covers extraction orchestration and prompt-injectable formatting.
Uses tmp_path pytest fixture for isolated filesystem.
"""

from __future__ import annotations

import json
import textwrap

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

    def test_test_dirs_auto_detected(self, tmp_path):
        """Test directories are auto-detected even without config."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("# test")
        (tmp_path / "conftest.py").write_text("# conftest")

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert "tests" in conventions.test.test_paths
        assert "test_*.py" in conventions.test.test_file_patterns

    def test_test_file_patterns_js_repo(self, tmp_path):
        """JS/TS test file patterns detected from directory structure."""
        (tmp_path / "__tests__").mkdir()
        (tmp_path / "app.spec.ts").write_text("// test")

        eslintrc = {"rules": {"no-console": "off"}}
        (tmp_path / ".eslintrc.json").write_text(json.dumps(eslintrc))

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert "__tests__" in conventions.test.test_paths
        assert "*.spec.ts" in conventions.test.test_file_patterns

    def test_test_dirs_only_no_framework(self, tmp_path):
        """Test directories detected even without any test framework config."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "e2e").mkdir()
        # Need at least one config file so conventions aren't empty
        eslintrc = {"rules": {"no-console": "off"}}
        (tmp_path / ".eslintrc.json").write_text(json.dumps(eslintrc))

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert "tests" in conventions.test.test_paths
        assert "e2e" in conventions.test.test_paths
        assert conventions.test.config_file == "(auto-detected from directory structure)"

    def test_pyproject_test_paths_enriched(self, tmp_path):
        """test_paths from pyproject enriched with auto-detected file patterns."""
        content = textwrap.dedent("""\
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """)
        (tmp_path / "pyproject.toml").write_text(content)
        (tmp_path / "tests").mkdir()

        extractor = ConventionsExtractor(str(tmp_path))
        conventions = extractor.extract()

        assert conventions.test.test_paths == ["tests"]
        assert "test_*.py" in conventions.test.test_file_patterns


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

    def test_test_file_treatment_section_present(self):
        """Test file treatment instructions appear when test paths exist."""
        conventions = ProjectConventions(
            test=QAConventions(
                framework="pytest",
                test_paths=["tests"],
                test_file_patterns=["test_*.py", "*_test.py", "conftest.py"],
                config_file="pyproject.toml[tool.pytest]",
            ),
            config_files_found=["pyproject.toml[tool.pytest]"],
        )

        result = build_conventions_context_string(conventions)

        assert "Test File Treatment" in result
        assert "INTENTIONALLY incorrect" in result
        assert "Hardcoded credentials" in result
        assert "test fixtures" in result
        assert "Missing error handling" in result
        assert "do NOT flag" in result

    def test_test_file_patterns_in_output(self):
        """Test file patterns are listed in the formatter output."""
        conventions = ProjectConventions(
            test=QAConventions(
                framework="jest",
                test_file_patterns=["*.spec.ts", "*.test.ts"],
                config_file="jest.config.js",
            ),
            config_files_found=["jest.config.js"],
        )

        result = build_conventions_context_string(conventions)

        assert "*.spec.ts" in result
        assert "*.test.ts" in result

    def test_no_test_treatment_without_paths(self):
        """No test treatment section when no test paths or patterns detected."""
        conventions = ProjectConventions(
            test=QAConventions(
                framework="pytest",
                config_file="pyproject.toml[tool.pytest]",
            ),
            config_files_found=["pyproject.toml[tool.pytest]"],
        )

        result = build_conventions_context_string(conventions)

        assert "Framework: pytest" in result
        assert "Test File Treatment" not in result
