"""Load vulnerability patterns from YAML files into a PatternLibrary."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import yaml

from forge.patterns.schema import VulnerabilityPattern

logger = logging.getLogger(__name__)


class PatternLibrary:
    """In-memory collection of loaded vulnerability patterns."""

    def __init__(self, patterns: list[VulnerabilityPattern] | None = None):
        self._patterns: dict[str, VulnerabilityPattern] = {}
        for p in patterns or []:
            self._patterns[p.id] = p

    @classmethod
    def load_from_directory(cls, directory: str | Path) -> PatternLibrary:
        """Load all YAML patterns from a directory tree."""
        directory = Path(directory)
        patterns: list[VulnerabilityPattern] = []
        for yaml_file in sorted(directory.rglob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                if data and isinstance(data, dict) and "id" in data:
                    patterns.append(VulnerabilityPattern(**data))
                    logger.debug("Loaded pattern %s from %s", data["id"], yaml_file)
            except Exception as exc:
                logger.warning("Skipping invalid pattern %s: %s", yaml_file, exc)
        return cls(patterns)

    @classmethod
    def load_default(cls) -> PatternLibrary:
        """Load from the built-in curated library."""
        default_dir = Path(__file__).parent / "library" / "curated"
        if default_dir.is_dir():
            return cls.load_from_directory(default_dir)
        return cls()

    # ── Accessors ────────────────────────────────────────────────────

    def get(self, pattern_id: str) -> VulnerabilityPattern | None:
        """Get a pattern by its ID (e.g. 'VP-001')."""
        return self._patterns.get(pattern_id)

    def get_by_slug(self, slug: str) -> VulnerabilityPattern | None:
        """Get a pattern by its slug (e.g. 'client-writable-server-authority')."""
        for p in self._patterns.values():
            if p.slug == slug:
                return p
        return None

    def all(self) -> list[VulnerabilityPattern]:
        """Return all loaded patterns."""
        return list(self._patterns.values())

    def by_category(self, category: str) -> list[VulnerabilityPattern]:
        """Return patterns filtered by category."""
        return [p for p in self._patterns.values() if p.category == category]

    def __len__(self) -> int:
        return len(self._patterns)

    def __iter__(self) -> Iterator[VulnerabilityPattern]:
        return iter(self._patterns.values())

    def __bool__(self) -> bool:
        return len(self._patterns) > 0
