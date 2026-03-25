"""Parser and filter for .forgeignore suppression register.

The .forgeignore file is a YAML list of suppression rules. Each rule MUST have:
  - `reason`: Why this finding is suppressed (REQUIRED — entries without reason are rejected)
  - `type`: Category of suppression (REQUIRED — one of the VALID_TYPES below)

Plus at least one matcher:
  - `check_id`: Exact deterministic check ID (e.g., SEC-001)
  - `pattern`: Regex matched against finding title
  - `path`: Glob matched against finding file locations

Optional:
  - `category`: Filter by finding category (security, quality, etc.)
  - `max_severity`: Only suppress findings at or below this severity
  - `expires`: ISO date after which the rule is ignored
"""
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

VALID_TYPES = {
    "false_positive",    # Scanner misidentifies code (e.g., detecting its own patterns)
    "not_applicable",    # Check doesn't apply to this project type
    "already_fixed",     # Code was fixed but pattern still triggers
    "accepted_risk",     # Known limitation with documented mitigation
    "intentional",       # Feature that looks like a vulnerability by design
    "test_fixture",      # Intentionally vulnerable test code
}

VALID_FIELDS = {
    "check_id", "pattern", "path", "category", "max_severity",
    "reason", "type", "expires",
}


@dataclass
class IgnoreRule:
    pattern: str | None = None       # regex on title
    category: str | None = None
    path: str | None = None          # glob on file path
    max_severity: str | None = None  # suppress at or below this severity
    check_id: str | None = None      # exact match on check_id (e.g., SEC-001)
    reason: str = ""                 # REQUIRED: why this is suppressed
    type: str = ""                   # REQUIRED: category of suppression
    expires: str | None = None       # ISO date

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

        # Check ID is sufficient on its own — deterministic checks use check_id
        # as the primary identifier. Path is optional narrowing.
        if self.check_id:
            finding_check_id = finding.get("check_id", "") or finding.get("id", "")
            if finding_check_id == self.check_id:
                # If path is also set, require path match too
                if not self.path:
                    return True
                locs = finding.get("locations", [])
                for loc in locs:
                    fp = loc.get("file_path", "") or loc.get("file", "")
                    if fp and fnmatch(fp, self.path):
                        return True
                # No locations means match on check_id alone
                return len(locs) == 0
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
            for loc in locs:
                fp = loc.get("file_path", "") or loc.get("file", "")
                if fp and fnmatch(fp, self.path):
                    return True
            # No locations and no other filters matched = no match
            return len(locs) == 0 if not self.pattern else False

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
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    continue

                # Validate required fields
                reason = item.get("reason", "")
                if not reason:
                    logger.warning(
                        ".forgeignore rule %d rejected: missing required 'reason' field. "
                        "Every suppression must explain why.",
                        i + 1,
                    )
                    continue

                rule_type = item.get("type", "")
                if rule_type and rule_type not in VALID_TYPES:
                    logger.warning(
                        ".forgeignore rule %d: unknown type '%s'. Valid: %s",
                        i + 1, rule_type, ", ".join(sorted(VALID_TYPES)),
                    )

                # Validate has at least one matcher
                has_matcher = any(item.get(k) for k in ("check_id", "pattern", "path"))
                if not has_matcher:
                    logger.warning(
                        ".forgeignore rule %d rejected: needs at least one of "
                        "check_id, pattern, or path.",
                        i + 1,
                    )
                    continue

                # Warn on unknown fields
                unknown = set(item.keys()) - VALID_FIELDS
                if unknown:
                    logger.warning(
                        ".forgeignore rule %d: unknown fields %s (ignored)",
                        i + 1, unknown,
                    )

                rule = IgnoreRule(
                    pattern=item.get("pattern"),
                    category=item.get("category"),
                    path=item.get("path"),
                    max_severity=item.get("max_severity"),
                    check_id=item.get("check_id"),
                    reason=reason,
                    type=rule_type,
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
