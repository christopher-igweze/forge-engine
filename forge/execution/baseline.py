"""Baseline storage and comparison for cross-scan finding tracking."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASELINE_FILENAME = "baseline.json"


@dataclass
class BaselineComparison:
    """Result of comparing current scan findings against a baseline."""

    new_findings: list[dict] = field(default_factory=list)
    recurring_findings: list[dict] = field(default_factory=list)
    fixed_findings: list[dict] = field(default_factory=list)
    suppressed_findings: list[dict] = field(default_factory=list)
    regressed_findings: list[dict] = field(default_factory=list)


@dataclass
class BaselineEntry:
    finding_id: str
    title: str
    category: str
    severity: str
    status: str  # "open" | "fixed" | "suppressed"
    first_seen: str
    last_seen: str
    scan_count: int
    file_path: str = ""
    cwe_id: str = ""
    audit_pass: str = ""


class Baseline:
    """Manages finding fingerprint persistence across scans."""

    def __init__(self) -> None:
        self.scan_id: str = ""
        self.generated_at: str = ""
        self.repo_path: str = ""
        self.fingerprints: dict[str, BaselineEntry] = {}
        self.suppressions: dict[str, dict] = {}

    @classmethod
    def load(cls, artifacts_dir: str) -> Baseline:
        """Load baseline from artifacts directory. Returns empty baseline if not found."""
        baseline = cls()
        path = Path(artifacts_dir) / BASELINE_FILENAME
        if not path.exists():
            return baseline
        try:
            data = json.loads(path.read_text())
            baseline.scan_id = data.get("scan_id", "")
            baseline.generated_at = data.get("generated_at", "")
            baseline.repo_path = data.get("repo_path", "")
            for fp, entry_data in data.get("fingerprints", {}).items():
                baseline.fingerprints[fp] = BaselineEntry(**entry_data)
            baseline.suppressions = data.get("suppressions", {})
        except Exception:
            logger.warning("Failed to load baseline from %s, starting fresh", path)
        return baseline

    def save(self, artifacts_dir: str) -> None:
        """Save baseline to artifacts directory."""
        path = Path(artifacts_dir) / BASELINE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "scan_id": self.scan_id,
            "generated_at": self.generated_at,
            "repo_path": self.repo_path,
            "fingerprints": {
                fp: {
                    "finding_id": entry.finding_id,
                    "title": entry.title,
                    "category": entry.category,
                    "severity": entry.severity,
                    "status": entry.status,
                    "first_seen": entry.first_seen,
                    "last_seen": entry.last_seen,
                    "scan_count": entry.scan_count,
                    "file_path": entry.file_path,
                    "cwe_id": entry.cwe_id,
                    "audit_pass": entry.audit_pass,
                }
                for fp, entry in self.fingerprints.items()
            },
            "suppressions": self.suppressions,
        }
        path.write_text(json.dumps(data, indent=2))

    def update_from_scan(
        self,
        scan_id: str,
        current_findings: list[dict],
    ) -> BaselineComparison:
        """Compare current findings against baseline and update state."""
        now = datetime.now(timezone.utc).isoformat()
        self.scan_id = scan_id
        self.generated_at = now

        comparison = BaselineComparison()
        current_fps: set[str] = set()

        # Snapshot baseline entries before processing for fuzzy matching
        pre_scan_entries = self._entries_as_dicts()

        for finding in current_findings:
            fp = finding.get("fingerprint", "")
            if not fp:
                continue
            current_fps.add(fp)

            # Check suppression
            if fp in self.suppressions:
                comparison.suppressed_findings.append(finding)
                continue

            if fp in self.fingerprints:
                entry = self.fingerprints[fp]
                if entry.status == "fixed":
                    # Was fixed, now it's back -- regression
                    comparison.regressed_findings.append(finding)
                    entry.status = "open"
                else:
                    comparison.recurring_findings.append(finding)
                entry.last_seen = now
                entry.scan_count += 1
                # Update severity if it changed
                entry.severity = finding.get("severity", entry.severity)
            else:
                # Try fuzzy match against baseline
                from forge.execution.fingerprint import find_match

                fuzzy_fp = find_match(finding, pre_scan_entries)
                if fuzzy_fp:
                    # Fuzzy match found -- treat as recurring, update fingerprint
                    entry = self.fingerprints[fuzzy_fp]
                    if entry.status == "fixed":
                        comparison.regressed_findings.append(finding)
                        entry.status = "open"
                    else:
                        comparison.recurring_findings.append(finding)
                    entry.last_seen = now
                    entry.scan_count += 1
                    entry.severity = finding.get("severity", entry.severity)
                    # Track old fingerprint so it's not marked fixed
                    current_fps.add(fuzzy_fp)
                else:
                    # Truly new finding
                    loc = (finding.get("locations") or [{}])[0]
                    comparison.new_findings.append(finding)
                    self.fingerprints[fp] = BaselineEntry(
                        finding_id=finding.get("id", ""),
                        title=finding.get("title", ""),
                        category=finding.get("category", ""),
                        severity=finding.get("severity", ""),
                        status="open",
                        first_seen=now,
                        last_seen=now,
                        scan_count=1,
                        file_path=loc.get("file_path", ""),
                        cwe_id=finding.get("cwe_id", ""),
                        audit_pass=finding.get("audit_pass", ""),
                    )

        # Findings in baseline but not in current scan -> fixed
        for fp, entry in self.fingerprints.items():
            if fp not in current_fps and entry.status == "open":
                entry.status = "fixed"
                comparison.fixed_findings.append(
                    {
                        "fingerprint": fp,
                        "finding_id": entry.finding_id,
                        "title": entry.title,
                        "category": entry.category,
                        "severity": entry.severity,
                    }
                )

        return comparison

    def _entries_as_dicts(self) -> dict[str, dict]:
        """Convert baseline entries to dicts suitable for find_match()."""
        return {
            fp: {
                "title": entry.title,
                "category": entry.category,
                "severity": entry.severity,
                "file_path": entry.file_path,
                "cwe_id": entry.cwe_id,
                "audit_pass": entry.audit_pass,
            }
            for fp, entry in self.fingerprints.items()
            if entry.status == "open"
        }

    def suppress(self, fingerprint: str, reason: str) -> None:
        """Mark a fingerprint as suppressed."""
        now = datetime.now(timezone.utc).isoformat()
        self.suppressions[fingerprint] = {
            "reason": reason,
            "suppressed_at": now,
            "suppressed_by": "user",
        }
