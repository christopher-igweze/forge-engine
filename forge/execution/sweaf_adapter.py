"""FORGE <-> SWE-AF data model translation layer.

Converts FORGE RemediationItems + AuditFindings into SWE-AF PlannedIssue
dicts, and maps SWE-AF execution results back to FORGE CoderFixResults.
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from typing import Any

from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    RemediationItem,
)


def finding_to_planned_issue(
    item: RemediationItem,
    finding: AuditFinding,
) -> dict[str, Any]:
    """Map a FORGE RemediationItem + AuditFinding to a SWE-AF PlannedIssue dict.

    Handles both Tier 2 (scoped, 1-3 files) and Tier 3 (architectural, 5-15
    files) items. Packs all security context into the description so SWE-AF's
    coder has full context without needing FORGE's schema.
    """
    # Build rich description with all security context
    desc_parts = [finding.description]

    if finding.data_flow:
        desc_parts.append(f"\n**Data Flow:** {finding.data_flow}")

    if finding.locations:
        loc_lines = []
        for loc in finding.locations:
            line_info = f":{loc.line_start}" if loc.line_start else ""
            loc_lines.append(f"- `{loc.file_path}{line_info}`")
            if loc.snippet:
                loc_lines.append(f"  ```\n  {loc.snippet}\n  ```")
        desc_parts.append("\n**Locations:**\n" + "\n".join(loc_lines))

    if finding.cwe_id:
        desc_parts.append(f"\n**CWE:** {finding.cwe_id}")
    if finding.owasp_ref:
        desc_parts.append(f"**OWASP:** {finding.owasp_ref}")

    if finding.suggested_fix:
        desc_parts.append(f"\n**Suggested Fix:** {finding.suggested_fix}")

    if item.approach:
        desc_parts.append(f"\n**Remediation Approach:** {item.approach}")

    description = "\n".join(desc_parts)

    # Map depends_on finding IDs to SWE-AF issue names
    depends_on = [f"fix-{fid.lower()}" for fid in item.depends_on]

    # Build guidance
    is_high_severity = finding.severity.value in ("critical", "high")
    guidance = {
        "needs_deeper_qa": is_high_severity,
        "review_focus": (
            "Security review: verify input validation, auth boundaries, "
            "error exposure, and injection surfaces."
            if finding.category.value == "security"
            else "Review for correctness, test coverage, and regression safety."
        ),
    }

    return {
        "name": f"fix-{item.finding_id.lower()}",
        "title": item.title,
        "description": description,
        "acceptance_criteria": item.acceptance_criteria or [
            f"Fix addresses: {finding.title}",
            "No new security vulnerabilities introduced",
            "Existing tests pass",
        ],
        "depends_on": depends_on,
        "files_to_modify": item.files_to_modify or [
            loc.file_path for loc in finding.locations
        ],
        "guidance": guidance,
    }


def write_issue_files(
    issues: list[dict[str, Any]],
    artifacts_dir: str,
) -> str:
    """Write SWE-AF issue .md files that the coder reads from issues_dir.

    Returns the path to the issues directory.
    """
    issues_dir = os.path.join(artifacts_dir, "sweaf-issues")
    os.makedirs(issues_dir, exist_ok=True)

    for issue in issues:
        filename = f"{issue['name']}.md"
        filepath = os.path.join(issues_dir, filename)

        ac_lines = "\n".join(
            f"- [ ] {ac}" for ac in issue.get("acceptance_criteria", [])
        )
        files_lines = "\n".join(
            f"- `{f}`" for f in issue.get("files_to_modify", [])
        )

        content = (
            f"# {issue['title']}\n\n"
            f"{issue['description']}\n\n"
            f"## Acceptance Criteria\n{ac_lines}\n\n"
            f"## Files to Modify\n{files_lines}\n"
        )

        with open(filepath, "w") as f:
            f.write(content)

    return issues_dir


def compute_execution_levels(issues: list[dict[str, Any]]) -> list[list[str]]:
    """Topological sort of issues by dependency graph (Kahn's algorithm).

    Returns a list of levels, where each level is a list of issue names
    that can execute in parallel.

    Falls back to a single level if circular dependencies are detected.
    """
    # Build adjacency and in-degree
    all_names = {issue["name"] for issue in issues}
    in_degree: dict[str, int] = {issue["name"]: 0 for issue in issues}
    dependents: dict[str, list[str]] = defaultdict(list)

    for issue in issues:
        for dep in issue.get("depends_on", []):
            if dep in all_names:
                in_degree[issue["name"]] += 1
                dependents[dep].append(issue["name"])

    # Kahn's algorithm with level tracking
    queue = deque(name for name, deg in in_degree.items() if deg == 0)
    levels: list[list[str]] = []

    while queue:
        level = list(queue)
        levels.append(level)
        next_queue: deque[str] = deque()
        for name in level:
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_queue.append(dependent)
        queue = next_queue

    # Check for circular dependencies
    processed = sum(len(level) for level in levels)
    if processed < len(all_names):
        # Circular dependency detected — fall back to single level
        return [list(all_names)]

    return levels


def build_plan_result(
    issues: list[dict[str, Any]],
    artifacts_dir: str,
) -> dict[str, Any]:
    """Build a SWE-AF plan_result dict with M2.5 model override.

    Sets model_override to minimax/minimax-m2.5 so SWE-AF workers use
    the cheap, fast model for all edit tasks.
    """
    levels = compute_execution_levels(issues)
    return {
        "issues": issues,
        "levels": levels,
        "artifacts_dir": artifacts_dir,
        "prd": {},
        "architecture": {},
        "model_override": "minimax/minimax-m2.5",
    }


def sweaf_result_to_coder_fix_results(
    sweaf_result: dict[str, Any],
    finding_map: dict[str, AuditFinding],
) -> list[CoderFixResult]:
    """Map SWE-AF DAGState issue outcomes back to FORGE CoderFixResults.

    Status mapping:
        completed -> COMPLETED
        partial / completed_with_debt -> COMPLETED_WITH_DEBT
        failed / error -> FAILED_RETRYABLE
    """
    results: list[CoderFixResult] = []

    issues = sweaf_result.get("issues", {})
    if isinstance(issues, list):
        issues = {i.get("name", ""): i for i in issues}

    for issue_name, outcome in issues.items():
        # Extract finding_id from issue name (fix-<finding_id>)
        finding_id = issue_name.removeprefix("fix-").upper()

        if isinstance(outcome, str):
            status = outcome
            files_changed: list[str] = []
            summary = ""
        elif isinstance(outcome, dict):
            status = outcome.get("status", "failed")
            files_changed = outcome.get("files_changed", [])
            summary = outcome.get("summary", "")
        else:
            status = "failed"
            files_changed = []
            summary = ""

        # Map status
        if status in ("completed", "success"):
            fix_outcome = FixOutcome.COMPLETED
        elif status in ("partial", "completed_with_debt"):
            fix_outcome = FixOutcome.COMPLETED_WITH_DEBT
        else:
            fix_outcome = FixOutcome.FAILED_RETRYABLE

        results.append(CoderFixResult(
            finding_id=finding_id,
            outcome=fix_outcome,
            files_changed=files_changed,
            summary=summary or f"SWE-AF: {status}",
        ))

    return results
