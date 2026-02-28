"""Conventions extractor — orchestrates all config file parsers.

Runs during Layer 0 (deterministic, zero LLM cost). Walks the repo
for known config files and extracts project conventions that inform
discovery agents about intentional patterns.

Usage:
    extractor = ConventionsExtractor(repo_path="/path/to/repo")
    conventions = extractor.extract()
"""

from __future__ import annotations

import logging
from pathlib import Path

from forge.conventions.models import (
    LintConventions,
    ProjectConventions,
    QAConventions,
    TypeScriptConventions,
)
from forge.conventions.parsers import (
    parse_eslint,
    parse_flake8,
    parse_jest_config,
    parse_prettier,
    parse_pylintrc,
    parse_pyproject_toml,
    parse_pytest_ini,
    parse_tsconfig,
)

logger = logging.getLogger(__name__)


class ConventionsExtractor:
    """Extract project conventions from config files.

    Runs all parsers and merges results into a ProjectConventions model.
    Defensive — never raises, returns empty conventions on failure.
    """

    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    def extract(self) -> ProjectConventions:
        """Run all parsers and produce merged ProjectConventions."""
        conventions = ProjectConventions()
        config_files: list[str] = []

        try:
            # ── Linting conventions (first match wins) ──────────────
            lint = self._extract_lint()
            if lint:
                conventions.lint = lint
                if lint.config_file:
                    config_files.append(lint.config_file)

            # ── Testing conventions ─────────────────────────────────
            test = self._extract_test()
            if test:
                conventions.test = test
                if test.config_file:
                    config_files.append(test.config_file)

            # ── TypeScript conventions ──────────────────────────────
            ts = self._extract_typescript()
            if ts:
                conventions.typescript = ts
                if ts.config_file:
                    config_files.append(ts.config_file)

            conventions.config_files_found = config_files

        except Exception as e:
            logger.warning("Conventions extraction failed (non-fatal): %s", e)

        if config_files:
            logger.info(
                "Conventions extracted: %d config files parsed — %s",
                len(config_files),
                ", ".join(config_files),
            )
        else:
            logger.debug("No convention config files found in %s", self.repo_path)

        return conventions

    def _extract_lint(self) -> LintConventions | None:
        """Extract linting conventions, prioritizing pyproject > eslint > pylint > flake8."""

        # Python: pyproject.toml has highest priority
        pyproject = parse_pyproject_toml(self.repo_path)
        lint_data = pyproject.get("lint")
        if lint_data:
            conv = LintConventions(**lint_data)
            conv.formatter = parse_prettier(self.repo_path)
            return conv

        # JavaScript/TypeScript: ESLint
        eslint = parse_eslint(self.repo_path)
        if eslint.get("disabled_rules"):
            conv = LintConventions(
                tool="eslint",
                disabled_rules=eslint["disabled_rules"],
                config_file=eslint.get("config_file", ""),
            )
            conv.formatter = parse_prettier(self.repo_path)
            return conv

        # Python fallback: .pylintrc
        pylintrc = parse_pylintrc(self.repo_path)
        if pylintrc.get("disabled_rules"):
            return LintConventions(
                tool="pylint",
                disabled_rules=pylintrc["disabled_rules"],
                config_file=pylintrc.get("config_file", ""),
            )

        # Python fallback: .flake8
        flake8 = parse_flake8(self.repo_path)
        if flake8.get("disabled_rules"):
            return LintConventions(
                tool=flake8.get("tool", "flake8"),
                disabled_rules=flake8["disabled_rules"],
                line_length=flake8.get("line_length"),
                config_file=flake8.get("config_file", ""),
            )

        # ESLint without disabled rules (just detect config exists)
        if eslint.get("config_file"):
            conv = LintConventions(
                tool="eslint",
                config_file=eslint["config_file"],
            )
            conv.formatter = parse_prettier(self.repo_path)
            return conv

        return None

    def _extract_test(self) -> QAConventions | None:
        """Extract testing conventions from pyproject/pytest.ini/jest."""

        # Python: pyproject.toml [tool.pytest]
        pyproject = parse_pyproject_toml(self.repo_path)
        test_data = pyproject.get("test")
        if test_data:
            return QAConventions(**test_data)

        # Python: pytest.ini / setup.cfg
        pytest_data = parse_pytest_ini(self.repo_path)
        if pytest_data:
            return QAConventions(**pytest_data)

        # JavaScript: Jest
        jest_data = parse_jest_config(self.repo_path)
        if jest_data:
            return QAConventions(**jest_data)

        # Detect pytest by presence of conftest.py
        root = Path(self.repo_path)
        if (root / "conftest.py").exists() or (root / "tests" / "conftest.py").exists():
            return QAConventions(
                framework="pytest",
                config_file="(detected from conftest.py)",
            )

        return None

    def _extract_typescript(self) -> TypeScriptConventions | None:
        """Extract TypeScript conventions from tsconfig.json."""
        ts_data = parse_tsconfig(self.repo_path)
        if ts_data:
            return TypeScriptConventions(**ts_data)
        return None
