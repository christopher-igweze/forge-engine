"""NIST SP 800-218A (SSDF) compliance mapping for FORGE.

Loads the practice mapping from ``ssdf_mapping.yaml`` and generates a
``ComplianceReport`` by cross-referencing practices with evidence
produced during a FORGE run (findings, fixes, validation results).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_MAPPING_PATH = Path(__file__).parent / "ssdf_mapping.yaml"


class CoverageLevel(str, Enum):
    """How well FORGE addresses a given SSDF practice."""

    COVERED = "COVERED"
    PARTIAL = "PARTIAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# ── Evidence mapping: evidence_type -> where to find it in run data ────


_EVIDENCE_TYPE_SOURCES: dict[str, str] = {
    "codebase_map": "findings",
    "security_findings": "findings",
    "quality_findings": "findings",
    "architecture_findings": "findings",
    "triage_results": "findings",
    "remediation_plan": "fixes",
    "fix_results": "fixes",
    "review_results": "fixes",
    "test_results": "validation",
    "validation_results": "validation",
    "debt_items": "findings",
}


@dataclass
class PracticeEvidence:
    """Evidence that a specific SSDF practice was addressed."""

    practice_id: str
    practice_name: str
    coverage: CoverageLevel
    agents_involved: list[str]
    evidence_items: list[dict[str, Any]]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "practice_id": self.practice_id,
            "practice_name": self.practice_name,
            "coverage": self.coverage.value,
            "agents_involved": self.agents_involved,
            "evidence_items": self.evidence_items,
            "notes": self.notes,
        }


@dataclass
class ComplianceReport:
    """Full NIST SSDF compliance report for a FORGE run."""

    forge_run_id: str
    practices: list[PracticeEvidence] = field(default_factory=list)
    covered_count: int = 0
    partial_count: int = 0
    not_applicable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "forge_run_id": self.forge_run_id,
            "practices": [p.to_dict() for p in self.practices],
            "covered_count": self.covered_count,
            "partial_count": self.partial_count,
            "not_applicable_count": self.not_applicable_count,
            "total_practices": len(self.practices),
        }

    def to_markdown(self) -> str:
        """Render the compliance report as a Markdown section."""
        lines: list[str] = [
            "## NIST SP 800-218A (SSDF) Compliance Summary",
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Practices Covered | {self.covered_count} |",
            f"| Practices Partially Covered | {self.partial_count} |",
            f"| Practices Not Applicable | {self.not_applicable_count} |",
            f"| Total Practices | {len(self.practices)} |",
            "",
        ]

        for p in self.practices:
            icon = {"COVERED": "[x]", "PARTIAL": "[-]", "NOT_APPLICABLE": "[ ]"}
            marker = icon.get(p.coverage.value, "[ ]")
            lines.append(f"- {marker} **{p.practice_id}** — {p.practice_name}")
            if p.agents_involved:
                agents_str = ", ".join(p.agents_involved)
                lines.append(f"  - Agents: {agents_str}")
            if p.evidence_items:
                lines.append(f"  - Evidence items: {len(p.evidence_items)}")
            if p.notes:
                lines.append(f"  - Note: {p.notes}")

        return "\n".join(lines)


def load_ssdf_mapping(mapping_path: Path | None = None) -> dict[str, Any]:
    """Load the SSDF practice mapping YAML.

    Falls back to the bundled ``ssdf_mapping.yaml`` next to this module.
    """
    path = mapping_path or _MAPPING_PATH
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data or {}
    except FileNotFoundError:
        logger.error("SSDF mapping file not found: %s", path)
        return {}
    except yaml.YAMLError as exc:
        logger.error("Failed to parse SSDF mapping YAML: %s", exc)
        return {}


def _collect_evidence_for_agent(
    agent_entry: dict[str, Any],
    findings: list[dict[str, Any]],
    fixes: list[dict[str, Any]] | None,
    validation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Collect evidence items from run data for a single agent mapping."""
    evidence_type = agent_entry.get("evidence_type", "")
    source = _EVIDENCE_TYPE_SOURCES.get(evidence_type, "")
    items: list[dict[str, Any]] = []

    agent_name_lower = agent_entry.get("agent_name", "").lower().replace(" ", "_")

    if source == "findings":
        for f in findings:
            # Match by agent name or category alignment
            f_agent = str(f.get("agent", "")).lower()
            f_category = str(f.get("category", "")).lower()
            if agent_name_lower and (
                agent_name_lower in f_agent
                or _evidence_matches_category(evidence_type, f_category)
            ):
                items.append({
                    "type": evidence_type,
                    "finding_id": f.get("id", ""),
                    "title": f.get("title", ""),
                    "severity": f.get("severity", ""),
                })
    elif source == "fixes" and fixes:
        for fix in fixes:
            items.append({
                "type": evidence_type,
                "finding_id": fix.get("finding_id", ""),
                "outcome": fix.get("outcome", ""),
            })
    elif source == "validation" and validation:
        items.append({
            "type": evidence_type,
            "tests_run": validation.get("tests_run", 0),
            "tests_passed": validation.get("tests_passed", 0),
            "passed": validation.get("passed", False),
        })

    return items


def _evidence_matches_category(evidence_type: str, category: str) -> bool:
    """Check if an evidence type aligns with a finding category."""
    mapping = {
        "security_findings": "security",
        "quality_findings": "quality",
        "architecture_findings": "architecture",
    }
    return mapping.get(evidence_type, "") == category


def generate_compliance_report(
    forge_run_id: str,
    findings: list[dict[str, Any]],
    fixes: list[dict[str, Any]] | None = None,
    validation: dict[str, Any] | None = None,
    mapping_path: Path | None = None,
) -> ComplianceReport:
    """Generate a compliance report from FORGE run data.

    Maps each SSDF practice to evidence collected during the run.
    Practices with no agents are marked NOT_APPLICABLE.
    Practices with agents but no evidence from the run are downgraded
    from COVERED to PARTIAL if the run data is incomplete.
    """
    mapping = load_ssdf_mapping(mapping_path)
    practices_raw = mapping.get("practices", {})

    report = ComplianceReport(forge_run_id=forge_run_id)

    for practice_id, practice_data in practices_raw.items():
        coverage = CoverageLevel(practice_data.get("forge_coverage", "NOT_APPLICABLE"))
        agents = practice_data.get("agents", [])
        notes = practice_data.get("notes", "")

        agents_involved: list[str] = []
        all_evidence: list[dict[str, Any]] = []

        for agent_entry in agents:
            agent_name = agent_entry.get("agent_name", "")
            agents_involved.append(agent_name)

            evidence = _collect_evidence_for_agent(
                agent_entry, findings, fixes, validation,
            )
            all_evidence.extend(evidence)

        # Downgrade COVERED to PARTIAL if agents are mapped but no evidence
        if coverage == CoverageLevel.COVERED and agents and not all_evidence:
            coverage = CoverageLevel.PARTIAL

        practice_evidence = PracticeEvidence(
            practice_id=practice_id,
            practice_name=practice_data.get("name", ""),
            coverage=coverage,
            agents_involved=agents_involved,
            evidence_items=all_evidence,
            notes=notes,
        )
        report.practices.append(practice_evidence)

    # Tally coverage levels
    report.covered_count = sum(
        1 for p in report.practices if p.coverage == CoverageLevel.COVERED
    )
    report.partial_count = sum(
        1 for p in report.practices if p.coverage == CoverageLevel.PARTIAL
    )
    report.not_applicable_count = sum(
        1 for p in report.practices if p.coverage == CoverageLevel.NOT_APPLICABLE
    )

    logger.info(
        "SSDF compliance report: %d covered, %d partial, %d N/A (of %d practices)",
        report.covered_count,
        report.partial_count,
        report.not_applicable_count,
        len(report.practices),
    )
    return report
