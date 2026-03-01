"""Project conventions auto-detection for FORGE.

Deterministic extraction of project conventions from config files
(eslintrc, tsconfig, pyproject.toml, etc.) to reduce false positives
in discovery findings. Zero LLM cost — runs in Layer 0.

Usage:
    from forge.conventions import ConventionsExtractor, build_conventions_context_string

    conventions = ConventionsExtractor(repo_path).extract()
    context_str = build_conventions_context_string(conventions)
"""

from forge.conventions.extractor import ConventionsExtractor
from forge.conventions.formatter import build_conventions_context_string
from forge.conventions.models import ProjectConventions

__all__ = [
    "ConventionsExtractor",
    "ProjectConventions",
    "build_conventions_context_string",
]
