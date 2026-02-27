"""Post-scan extraction pipeline — learning loop for the pattern library.

After each discovery run, findings are appended to a history file and
pattern prevalence is updated. Over time this enables the extraction
of new patterns from recurring finding clusters.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from forge.patterns.loader import PatternLibrary
from forge.schemas import AuditFinding

logger = logging.getLogger(__name__)


def append_findings_history(
    findings: list[AuditFinding],
    artifacts_dir: str,
) -> Path:
    """Append findings to findings_history.jsonl for future clustering.

    Returns the path to the history file.
    """
    history_path = Path(artifacts_dir) / "findings_history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    with open(history_path, "a") as f:
        for finding in findings:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "finding_id": finding.id,
                "title": finding.title,
                "category": finding.category.value
                if hasattr(finding.category, "value")
                else str(finding.category),
                "severity": finding.severity.value
                if hasattr(finding.severity, "value")
                else str(finding.severity),
                "pattern_id": finding.pattern_id,
                "pattern_slug": finding.pattern_slug,
                "description": finding.description[:500],
                "cwe_id": finding.cwe_id,
            }
            f.write(json.dumps(entry) + "\n")

    logger.debug("Appended %d findings to %s", len(findings), history_path)
    return history_path


def update_pattern_prevalence(
    findings: list[AuditFinding],
    library: PatternLibrary,
) -> dict[str, int]:
    """Count how many findings matched each pattern.

    Updates in-memory times_detected on the pattern objects.
    Returns {pattern_id: count} for matched patterns.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.pattern_id:
            counts[finding.pattern_id] = counts.get(finding.pattern_id, 0) + 1

    for pid, count in counts.items():
        pattern = library.get(pid)
        if pattern:
            pattern.times_detected += count

    return counts
