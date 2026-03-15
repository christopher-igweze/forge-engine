"""SWE-AF HTTP bridge for all AI remediation (Tier 2 + Tier 3).

Routes findings to SWE-AF's DAG executor via AgentField's async API.
Passes remaining budget from RunTelemetry so SWE-AF respects cost caps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from forge.execution.sweaf_adapter import (
    build_plan_result,
    finding_to_planned_issue,
    sweaf_result_to_coder_fix_results,
    write_issue_files,
)
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    RemediationItem,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig
    from forge.schemas import ForgeExecutionState

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10  # seconds between status checks


async def execute_tier3_via_sweaf(
    tier3_items: list[RemediationItem],
    findings: list[AuditFinding],
    state: ForgeExecutionState,
    cfg: ForgeConfig,
) -> list[CoderFixResult]:
    """Execute Tier 3 findings via SWE-AF's DAG executor.

    1. Convert FORGE items to SWE-AF planned issues
    2. Write issue .md files to artifacts dir
    3. POST async execution to AgentField
    4. Poll until complete/failed/timeout
    5. Map results back to CoderFixResults
    """
    finding_map = {f.id: f for f in findings}

    # Step 1: Convert to SWE-AF issues
    issues = []
    for item in tier3_items:
        finding = finding_map.get(item.finding_id)
        if not finding:
            logger.warning("SWE-AF bridge: finding %s not found, skipping", item.finding_id)
            continue
        issues.append(finding_to_planned_issue(item, finding))

    if not issues:
        return []

    # Step 2: Write issue files
    artifacts_dir = os.path.join(state.repo_path, ".forge-artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    write_issue_files(issues, artifacts_dir)

    # Step 3: Build synthetic plan_result (includes M2.5 model override)
    plan_result = build_plan_result(issues, artifacts_dir)

    # Step 4: POST to AgentField
    try:
        execution_id = await _post_execution(plan_result, state, cfg)
    except Exception as e:
        logger.error("SWE-AF bridge: failed to start execution: %s", e)
        return _failed_results(tier3_items, str(e))

    # Step 5: Poll for completion
    try:
        result = await _poll_execution(execution_id, cfg)
    except Exception as e:
        logger.error("SWE-AF bridge: polling failed: %s", e)
        return _failed_results(tier3_items, str(e))

    # Step 6: Map results
    return sweaf_result_to_coder_fix_results(result, finding_map)


async def _post_execution(
    plan_result: dict[str, Any],
    state: ForgeExecutionState,
    cfg: ForgeConfig,
) -> str:
    """POST async execution request to AgentField. Returns execution_id."""
    url = f"{cfg.sweaf_agentfield_url}/api/v1/execute/async/{cfg.sweaf_node_id}.execute"

    # Read remaining budget from RunTelemetry (if active)
    max_cost = cfg.sweaf_max_cost_usd
    try:
        from forge.execution.run_telemetry import _current_run_telemetry
        rt = _current_run_telemetry.get(None)
        if rt is not None:
            remaining = rt.max_cost_usd - rt.total_cost_usd
            if remaining < max_cost:
                max_cost = max(0.0, remaining)
                logger.info(
                    "SWE-AF bridge: capping cost to $%.2f (remaining budget)", max_cost,
                )
    except Exception:
        pass  # RunTelemetry not available — use config default

    payload = json.dumps({
        "plan_result": plan_result,
        "repo_path": state.repo_path,
        "repo_url": cfg.repo_url,
        "max_coding_iterations": cfg.sweaf_max_coding_iterations,
        "max_concurrent_issues": cfg.sweaf_max_concurrent_issues,
        "runtime": cfg.sweaf_runtime,
        "max_cost_usd": max_cost,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.sweaf_api_key}",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=30))
    body = json.loads(response.read())

    execution_id = body.get("execution_id", body.get("id", ""))
    if not execution_id:
        raise ValueError(f"No execution_id in response: {body}")

    logger.info("SWE-AF bridge: started execution %s", execution_id)
    return execution_id


async def _poll_execution(
    execution_id: str,
    cfg: ForgeConfig,
) -> dict[str, Any]:
    """Poll AgentField until execution completes or times out."""
    url = f"{cfg.sweaf_agentfield_url}/api/v1/executions/{execution_id}"
    headers = {"Authorization": f"Bearer {cfg.sweaf_api_key}"}

    elapsed = 0
    loop = asyncio.get_running_loop()

    while elapsed < cfg.sweaf_timeout_seconds:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=30),
            )
            body = json.loads(response.read())
        except Exception as e:
            logger.warning("SWE-AF bridge: poll error (will retry): %s", e)
            continue

        status = body.get("status", "")
        logger.debug("SWE-AF bridge: execution %s status=%s", execution_id, status)

        if status in ("completed", "success"):
            return body.get("result", body)
        if status in ("failed", "error", "cancelled"):
            raise RuntimeError(f"SWE-AF execution {execution_id} failed: {body.get('error', status)}")

    raise TimeoutError(f"SWE-AF execution {execution_id} timed out after {cfg.sweaf_timeout_seconds}s")


def _failed_results(
    items: list[RemediationItem],
    error_msg: str,
) -> list[CoderFixResult]:
    """Generate FAILED_RETRYABLE results for all items on bridge failure."""
    return [
        CoderFixResult(
            finding_id=item.finding_id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"SWE-AF bridge error: {error_msg[:200]}",
        )
        for item in items
    ]
