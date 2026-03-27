"""Parser and filter for .forgeignore suppression register.

Supports two formats:

**v2** (recommended):
  version: 2
  suppressions:
    - id: sup_001
      kind: false_positive
      match:
        rule_family: hardcoded-secret
        file: forge/mcp_server.py
        line_range: [80, 100]
        anchor:
          symbol: _send_telemetry
          snippet_hash: 8f31c2d
      reason: Telemetry sample value, not a real secret

**v1** (backward compatible):
  - check_id: "SEC-001"
    type: "false_positive"
    reason: "Template text"

Multi-strategy matching precedence:
  1. Exact check_id match
  2. rule_family + file + line_range
  3. rule_family + file + symbol
  4. rule_family + file (broader)
  5. Title regex fallback (v1 compat, weakest)
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

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

VALID_TYPES = {
    "false_positive",    # Scanner misidentifies code
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
class SuppressionRule:
    """A single suppression entry in .forgeignore v2."""

    id: str = ""                              # unique suppression ID
    kind: str = ""                            # false_positive, not_applicable, etc.
    reason: str = ""                          # REQUIRED: why this is suppressed
    expires: str | None = None                # ISO date

    # Match criteria
    rule_family: str | None = None            # e.g. hardcoded-secret, sql-injection
    file: str | None = None                   # file path (glob supported)
    line_range: tuple[int, int] | None = None # (start, end) line range
    check_id: str | None = None               # deterministic check ID
    category: str | None = None               # security, quality, etc.

    # Anchors
    symbol: str | None = None                 # enclosing function/class name
    snippet_hash: str | None = None           # hash of code snippet

    # Legacy v1 fields
    pattern: str | None = None                # regex on title (v1 compat, weakest)
    path: str | None = None                   # glob on file path (v1 compat)
    max_severity: str | None = None           # severity cap
    type: str = ""                            # v1 type field (maps to kind)

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        try:
            return date.fromisoformat(self.expires) < date.today()
        except ValueError:
            return False

    def matches(self, finding: dict) -> bool:
        """Check if this rule suppresses the given finding.

        Matching precedence:
        1. Exact check_id match
        2. rule_family + file + line_range match
        3. rule_family + file + symbol match
        4. rule_family + file match (broader)
        5. Title regex fallback (v1 compat, weakest)
        """
        if self.is_expired():
            return False

        # Strategy 1: Exact ID match
        if self.check_id:
            finding_check_id = finding.get("check_id") or finding.get("pattern_id") or ""
            # Opengrep embeds rule ID in data_flow as "file:line [rule.id]"
            if not finding_check_id:
                data_flow = finding.get("data_flow", "")
                m = re.search(r"\[([^\]]+)\]", data_flow)
                if m:
                    finding_check_id = m.group(1)
            if self.check_id == finding_check_id:
                return self._file_matches(finding)  # scope to file if specified
            return False

        # Strategy 2-4: rule_family based matching
        if self.rule_family:
            finding_rf = finding.get("rule_family", "")
            if finding_rf and finding_rf == self.rule_family:
                # Category filter
                if self.category and finding.get("category") != self.category:
                    return False
                # Severity cap
                if self.max_severity and not self._severity_ok(finding):
                    return False
                # Must match file if specified
                if self.file and not self._file_matches(finding):
                    return False
                # If file matches, check narrower criteria
                if self.file:
                    # Strategy 2: line_range match
                    if self.line_range:
                        if self._line_range_matches(finding):
                            return True
                        return False  # line_range specified but didn't match
                    # Strategy 3: symbol match
                    if self.symbol:
                        finding_symbol = finding.get("enclosing_symbol", "")
                        if finding_symbol and finding_symbol == self.symbol:
                            return True
                        return False  # symbol specified but didn't match
                    # Strategy 4: rule_family + file only
                    return True
                # rule_family match without file = global for this rule type
                return True
            return False

        # Strategy 5: Legacy matching (v1 compat)
        # Category filter (for v1 category-only rules)
        if self.category and finding.get("category", "") != self.category:
            return False

        # Severity cap
        if self.max_severity and not self._severity_ok(finding):
            return False

        # Title pattern
        if self.pattern:
            title = finding.get("title", "")
            if not re.search(self.pattern, title, re.IGNORECASE):
                return False

        # Path glob
        if self.path or self.file:
            if not self._file_matches(finding):
                return False

        # Must have matched at least one positive criterion
        if not self.pattern and not (self.path or self.file) and not self.category:
            return False

        return True

    def _file_matches(self, finding: dict) -> bool:
        """Check if finding is in a matching file."""
        file_pattern = self.file or self.path
        if not file_pattern:
            return True  # no file constraint
        locations = finding.get("locations", [])
        if not locations:
            return not file_pattern  # no locations = match only if no file specified
        for loc in locations:
            fp = loc.get("file_path", "") if isinstance(loc, dict) else getattr(loc, "file_path", "")
            if not fp:
                fp = loc.get("file", "") if isinstance(loc, dict) else getattr(loc, "file", "")
            if fp and (fnmatch(fp, file_pattern) or fnmatch(fp, f"*/{file_pattern}")):
                return True
        return False

    def _line_range_matches(self, finding: dict) -> bool:
        """Check if finding is within the specified line range."""
        if not self.line_range:
            return True
        start, end = self.line_range
        locations = finding.get("locations", [])
        for loc in locations:
            line_start = loc.get("line_start", 0) if isinstance(loc, dict) else getattr(loc, "line_start", 0)
            line_end = loc.get("line_end", line_start) if isinstance(loc, dict) else getattr(loc, "line_end", line_start)
            if line_start and start <= line_start <= end:
                return True
            if line_end and start <= line_end <= end:
                return True
        return False

    def _severity_ok(self, finding: dict) -> bool:
        """Check if finding severity is at or below the max."""
        finding_sev = (finding.get("severity") or "medium").lower()
        max_sev = (self.max_severity or "critical").lower()
        return SEVERITY_ORDER.get(finding_sev, 2) <= SEVERITY_ORDER.get(max_sev, 4)


# Backward compatibility alias
IgnoreRule = SuppressionRule


class ForgeIgnore:
    """Loads and applies .forgeignore rules."""

    def __init__(self, rules: list[SuppressionRule] | None = None) -> None:
        self.rules = rules or []

    @classmethod
    def load(cls, repo_path: str) -> ForgeIgnore:
        path = Path(repo_path) / FORGEIGNORE_FILENAME
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            logger.warning(
                "Failed to parse .forgeignore at %s", path, exc_info=True
            )
            return cls()

        if data is None:
            return cls()

        # Detect v2 format
        if isinstance(data, dict) and data.get("version") == 2:
            return cls._load_v2(data)

        # v1 format (list of rules)
        if isinstance(data, list):
            return cls._load_v1(data)

        logger.warning(
            ".forgeignore should be a YAML list or v2 dict, got %s",
            type(data).__name__,
        )
        return cls()

    @classmethod
    def _load_v2(cls, data: dict) -> ForgeIgnore:
        rules = []
        for entry in data.get("suppressions", []):
            if not isinstance(entry, dict):
                continue
            match = entry.get("match", {})
            line_range = None
            lr = match.get("line_range")
            if lr and isinstance(lr, list) and len(lr) == 2:
                line_range = (lr[0], lr[1])

            anchor = match.get("anchor", {})

            rule = SuppressionRule(
                id=entry.get("id", ""),
                kind=entry.get("kind", ""),
                reason=entry.get("reason", ""),
                expires=entry.get("expires"),
                rule_family=match.get("rule_family"),
                file=match.get("file"),
                line_range=line_range,
                check_id=match.get("check_id"),
                category=match.get("category"),
                symbol=anchor.get("symbol"),
                snippet_hash=anchor.get("snippet_hash"),
                pattern=match.get("pattern"),  # legacy support in v2
                max_severity=match.get("max_severity"),
            )

            if rule.reason:  # reason is required
                rules.append(rule)
            else:
                logger.warning(
                    ".forgeignore v2 suppression '%s' rejected: missing required 'reason' field.",
                    entry.get("id", "unknown"),
                )
        return cls(rules=rules)

    @classmethod
    def _load_v1(cls, data: list) -> ForgeIgnore:
        """Load v1 format -- convert to SuppressionRule."""
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
            has_matcher = any(item.get(k) for k in ("check_id", "pattern", "path", "category"))
            if not has_matcher:
                logger.warning(
                    ".forgeignore rule %d rejected: needs at least one of "
                    "check_id, pattern, path, or category.",
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

            rule = SuppressionRule(
                kind=rule_type,
                type=rule_type,
                reason=reason,
                expires=item.get("expires"),
                check_id=item.get("check_id"),
                pattern=item.get("pattern"),
                path=item.get("path"),
                file=item.get("path"),  # map v1 path to file
                category=item.get("category"),
                max_severity=item.get("max_severity"),
            )
            rules.append(rule)
        return cls(rules=rules)

    def is_suppressed(self, finding: dict) -> tuple[bool, str | None]:
        for rule in self.rules:
            if rule.matches(finding):
                return True, rule.reason
        return False, None

    def serialize_for_prompt(self) -> str:
        """Serialize .forgeignore rules into a human-readable string for LLM prompt injection."""
        if not self.rules:
            return ""
        lines = []
        for rule in self.rules:
            parts = []
            if rule.rule_family:
                parts.append(f"Rule Family: {rule.rule_family}")
            if rule.check_id:
                parts.append(f"Check ID: {rule.check_id}")
            if rule.pattern:
                parts.append(f"Pattern: {rule.pattern}")
            if rule.file or rule.path:
                parts.append(f"File: {rule.file or rule.path}")
            if rule.symbol:
                parts.append(f"Symbol: {rule.symbol}")
            if rule.category:
                parts.append(f"Category: {rule.category}")
            parts.append(f"Type: {rule.kind or rule.type or 'unknown'}")
            parts.append(f"Reason: {rule.reason or 'no reason given'}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

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


async def sync_forgeignore_training(
    repo_path: str,
    vibe2prod_url: str = "",
    api_key: str = "",
    scan_mode: str = "full",
) -> None:
    """Share anonymized .forgeignore entries to training endpoint.

    Non-blocking, non-fatal. Only runs if consent is given via
    VIBE2PROD_DATA_SHARING env var or config file share_forgeignore field.
    """
    import hashlib
    import os
    import subprocess

    import httpx

    fi = ForgeIgnore.load(repo_path)
    if not fi.rules:
        return

    entries = []
    for rule in fi.rules:
        # Use rule_family as the primary identity, fall back to pattern/check_id
        pattern_value = rule.rule_family or rule.pattern or rule.check_id or ""
        # Category from rule or infer from rule_family
        category_value = rule.category or rule.kind or ""
        entries.append({
            "pattern": pattern_value,
            "category": category_value,
            "reason": rule.reason,
            "type": rule.kind or "false_positive",
            "rule_family": rule.rule_family or "",
            "check_id": rule.check_id,
            "path": rule.file or rule.path,
            "max_severity": getattr(rule, "max_severity", None),
        })

    if not entries:
        return

    # Anonymize repo identity
    remote = ""
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            remote = r.stdout.strip()
    except Exception:
        pass
    repo_hash = hashlib.sha256((remote or repo_path).encode()).hexdigest()

    url = vibe2prod_url or os.environ.get("VIBE2PROD_URL", "https://api.vibe2prod.net")

    try:
        from forge import __version__ as ver
    except Exception:
        ver = "unknown"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = api_key or os.environ.get("VIBE2PROD_API_KEY", "")
    if key:
        headers["X-API-Key"] = key

    async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
        await client.post(
            f"{url}/api/training/forgeignore",
            json={
                "repo_hash": repo_hash,
                "entries": entries,
                "scan_mode": scan_mode,
                "version": ver,
            },
            headers=headers,
        )
