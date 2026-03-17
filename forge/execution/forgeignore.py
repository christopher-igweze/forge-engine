"""Parser and filter for .forgeignore suppression rules."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FORGEIGNORE_FILENAME = ".forgeignore"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class IgnoreRule:
    pattern: str | None = None  # regex on title
    category: str | None = None
    path: str | None = None  # glob on file path
    max_severity: str | None = None  # suppress at or below this severity
    reason: str = ""
    expires: str | None = None  # ISO date

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        try:
            return date.fromisoformat(self.expires) < date.today()
        except ValueError:
            return False

    def matches(self, finding: dict) -> bool:
        if self.is_expired():
            return False

        # Category filter
        if self.category and finding.get("category", "") != self.category:
            return False

        # Severity cap
        if self.max_severity:
            finding_sev = SEVERITY_ORDER.get(finding.get("severity", ""), 0)
            cap_sev = SEVERITY_ORDER.get(self.max_severity, 0)
            if finding_sev > cap_sev:
                return False

        # Title pattern
        if self.pattern:
            title = finding.get("title", "")
            if not re.search(self.pattern, title, re.IGNORECASE):
                return False

        # Path glob
        if self.path:
            locs = finding.get("locations", [])
            file_path = locs[0].get("file_path", "") if locs else ""
            if not fnmatch(file_path, self.path):
                return False

        return True


class ForgeIgnore:
    """Loads and applies .forgeignore rules."""

    def __init__(self, rules: list[IgnoreRule] | None = None) -> None:
        self.rules = rules or []

    @classmethod
    def load(cls, repo_path: str) -> ForgeIgnore:
        path = Path(repo_path) / FORGEIGNORE_FILENAME
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, list):
                logger.warning(
                    ".forgeignore should be a YAML list, got %s",
                    type(data).__name__,
                )
                return cls()
            rules = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                rule = IgnoreRule(
                    pattern=item.get("pattern"),
                    category=item.get("category"),
                    path=item.get("path"),
                    max_severity=item.get("max_severity"),
                    reason=item.get("reason", ""),
                    expires=item.get("expires"),
                )
                rules.append(rule)
            return cls(rules)
        except Exception:
            logger.warning(
                "Failed to parse .forgeignore at %s", path, exc_info=True
            )
            return cls()

    def is_suppressed(self, finding: dict) -> tuple[bool, str | None]:
        for rule in self.rules:
            if rule.matches(finding):
                return True, rule.reason
        return False, None

    def apply(
        self, findings: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        kept: list[dict] = []
        suppressed: list[dict] = []
        for finding in findings:
            is_sup, reason = self.is_suppressed(finding)
            if is_sup:
                finding["suppressed"] = True
                finding["suppression_reason"] = reason
                suppressed.append(finding)
            else:
                kept.append(finding)
        return kept, suppressed
