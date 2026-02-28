"""Data models for auto-detected project conventions.

Zero LLM cost — these are populated by deterministic config file parsing.
Used to tell discovery agents which patterns are intentional project choices
rather than findings to report.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LintConventions(BaseModel):
    """Linting/formatting conventions extracted from config files."""

    tool: str = ""
    disabled_rules: list[str] = Field(default_factory=list)
    line_length: int | None = None
    target_version: str = ""
    formatter: str = ""
    config_file: str = ""


class TestConventions(BaseModel):
    """Testing conventions extracted from config files."""

    framework: str = ""
    custom_markers: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    coverage_threshold: float | None = None
    config_file: str = ""


class TypeScriptConventions(BaseModel):
    """TypeScript conventions from tsconfig.json."""

    strict: bool | None = None
    no_implicit_any: bool | None = None
    target: str = ""
    jsx: str = ""
    config_file: str = ""


class ProjectConventions(BaseModel):
    """All auto-detected conventions from a repository's config files.

    Populated by ConventionsExtractor during Layer 0 (deterministic, zero LLM).
    Consumed by the formatter to produce a prompt-injectable string.
    """

    lint: LintConventions = Field(default_factory=LintConventions)
    test: TestConventions = Field(default_factory=TestConventions)
    typescript: TypeScriptConventions = Field(default_factory=TypeScriptConventions)

    detected_patterns: list[str] = Field(default_factory=list)

    config_files_found: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True if no conventions were detected."""
        return not self.config_files_found and not self.detected_patterns
