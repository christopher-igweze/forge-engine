"""Pattern learning pipeline: extract recurring patterns from scan history.

Reads findings_history.jsonl, clusters similar findings across scans,
and proposes new VulnerabilityPattern YAML files when a cluster recurs
in enough distinct scans.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

from forge.patterns.schema import (
    LLMGuidance,
    PatternSource,
    PatternTier,
    VulnerabilityPattern,
)

logger = logging.getLogger(__name__)


@dataclass
class FindingCluster:
    """A group of similar findings across scans."""

    category: str
    cluster_key: str  # normalized title used for grouping
    findings: list[dict]
    scan_count: int  # distinct timestamps (proxy for distinct scans)


def load_findings_history(artifacts_dir: Path) -> list[dict]:
    """Load all findings from findings_history.jsonl."""
    path = artifacts_dir / "findings_history.jsonl"
    if not path.exists():
        return []
    findings = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in findings_history.jsonl")
    return findings


def _normalize_title(title: str) -> str:
    """Normalize a finding title for clustering.

    Strips file paths, line numbers, and variable names to group
    structurally identical findings together.
    """
    # Remove file paths (e.g., "in src/foo/bar.ts")
    normalized = re.sub(r"\bin\s+\S+\.\w+\b", "", title)
    # Remove line references
    normalized = re.sub(r"\b(?:line|L)\s*\d+\b", "", normalized, flags=re.IGNORECASE)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return slug.strip("-")[:60]


def cluster_findings(
    findings: list[dict],
    min_occurrences: int = 3,
) -> list[FindingCluster]:
    """Cluster findings by category + normalized title.

    Only returns clusters that appear across ``min_occurrences`` or more
    distinct scan timestamps (proxy for distinct scan runs).
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in findings:
        category = (f.get("category") or "unknown").upper()
        title = f.get("title", "")
        cluster_key = _normalize_title(title)
        if not cluster_key:
            continue
        groups[(category, cluster_key)].append(f)

    clusters = []
    for (category, cluster_key), group in groups.items():
        # Use timestamps truncated to the minute as a scan-run proxy
        scan_markers = {f.get("timestamp", "unknown")[:16] for f in group}
        if len(scan_markers) >= min_occurrences:
            clusters.append(
                FindingCluster(
                    category=category,
                    cluster_key=cluster_key,
                    findings=group,
                    scan_count=len(scan_markers),
                )
            )

    return sorted(clusters, key=lambda c: c.scan_count, reverse=True)


def _get_next_pattern_id(library_dir: Path) -> int:
    """Determine the next available VP-NNN ID number."""
    existing_ids: list[int] = []
    for subdir in ("curated", "proposed"):
        pattern_dir = library_dir / subdir
        if not pattern_dir.is_dir():
            continue
        for yaml_path in pattern_dir.glob("VP-*.yaml"):
            match = re.match(r"VP-(\d+)", yaml_path.stem)
            if match:
                existing_ids.append(int(match.group(1)))
    return max(existing_ids, default=0) + 1


def generate_proposed_pattern(
    cluster: FindingCluster,
    pattern_id: int,
) -> VulnerabilityPattern:
    """Generate a proposed VulnerabilityPattern from a finding cluster."""
    titles = [f.get("title", "") for f in cluster.findings]
    descriptions = [f.get("description", "") for f in cluster.findings]
    severities = [f.get("severity", "medium").lower() for f in cluster.findings]
    cwe_ids = [f.get("cwe_id", "") for f in cluster.findings if f.get("cwe_id")]

    most_common_title = Counter(titles).most_common(1)[0][0] if titles else cluster.cluster_key
    most_common_severity = Counter(severities).most_common(1)[0][0] if severities else "medium"

    # Synthesize LLM guidance from descriptions
    unique_descs = list({d[:300] for d in descriptions if d})[:3]
    reasoning_prompt = " | ".join(unique_descs) if unique_descs else ""

    # Deduplicate CWE IDs
    unique_cwe = list(dict.fromkeys(cwe_ids))[:5]

    pid = f"VP-{pattern_id:03d}"
    slug = _slugify(most_common_title)

    return VulnerabilityPattern(
        id=pid,
        name=most_common_title,
        slug=slug,
        description=f"Auto-proposed pattern from {len(cluster.findings)} findings "
        f"across {cluster.scan_count} scans.",
        category=cluster.category.lower(),
        severity_default=most_common_severity,
        cwe_ids=unique_cwe,
        tier=PatternTier.LLM_ONLY,
        llm_guidance=LLMGuidance(reasoning_prompt=reasoning_prompt),
        source=PatternSource.SCAN_DERIVED,
        times_detected=len(cluster.findings),
        times_confirmed=0,
        false_positive_rate=0.0,
    )


def extract_proposed_patterns(
    artifacts_dir: Path,
    library_dir: Path | None = None,
    min_occurrences: int = 3,
) -> list[VulnerabilityPattern]:
    """Main entry point: scan findings_history -> proposed patterns.

    Returns list of new proposed patterns; also saves them as YAML files.
    """
    if library_dir is None:
        library_dir = Path(__file__).parent / "library"

    proposed_dir = library_dir / "proposed"
    proposed_dir.mkdir(parents=True, exist_ok=True)

    findings = load_findings_history(artifacts_dir)
    if not findings:
        logger.info("No findings history found -- nothing to learn from")
        return []

    clusters = cluster_findings(findings, min_occurrences=min_occurrences)
    if not clusters:
        logger.info(
            "No recurring patterns found (min_occurrences=%d)", min_occurrences
        )
        return []

    next_id = _get_next_pattern_id(library_dir)

    proposed: list[VulnerabilityPattern] = []
    for cluster in clusters:
        pattern = generate_proposed_pattern(cluster, next_id)
        next_id += 1

        # Serialize via model_dump -> YAML
        yaml_path = proposed_dir / f"{pattern.id}.yaml"
        yaml_path.write_text(
            yaml.dump(
                pattern.model_dump(mode="json"),
                default_flow_style=False,
                sort_keys=False,
            )
        )
        logger.info(
            "Proposed pattern %s: %s (%d occurrences across %d scans)",
            pattern.id,
            pattern.name,
            len(cluster.findings),
            cluster.scan_count,
        )
        proposed.append(pattern)

    return proposed
