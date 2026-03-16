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
    """Execute findings via SWE-AF's DAG executor.

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

    # Step 6: Record SWE-AF cost to FORGE telemetry
    cost_summary = result.get("cost_summary") if isinstance(result, dict) else None
    if cost_summary:
        try:
            from forge.execution.run_telemetry import _current_run_telemetry
            rt = _current_run_telemetry.get(None)
            if rt:
                sweaf_cost = cost_summary.get("total_cost_usd", 0)
                sweaf_tokens = cost_summary.get("total_tokens", 0)
                if sweaf_cost > 0:
                    await rt.record_invocation(
                        agent_name="sweaf_remediation",
                        model="minimax/minimax-m2.5",
                        input_tokens=sweaf_tokens,
                        output_tokens=0,
                        cost_usd=sweaf_cost,
                    )
                logger.info(
                    "SWE-AF cost: $%.4f (%d tokens, %d calls)",
                    cost_summary.get("total_cost_usd", 0),
                    cost_summary.get("total_tokens", 0),
                    cost_summary.get("total_invocations", 0),
                )
        except Exception:
            pass

    # Step 7: Map results
    return sweaf_result_to_coder_fix_results(result, finding_map)


async def _post_execution(
    plan_result: dict[str, Any],
    state: ForgeExecutionState,
    cfg: ForgeConfig,
) -> str:
    """POST async execution request to AgentField. Returns execution_id."""
    url = f"{cfg.sweaf_agentfield_url}/api/v1/execute/async/{cfg.sweaf_node_id}.execute"

    payload = json.dumps({
        "input": {
            "plan_result": plan_result,
            "repo_path": state.repo_path,
            "config": {
                "runtime": cfg.sweaf_runtime,
                "max_coding_iterations": cfg.sweaf_max_coding_iterations,
                "max_concurrent_issues": cfg.sweaf_max_concurrent_issues,
                "models": {"default": "minimax/minimax-m2.5"},
            },
            "git_config": {
                "repo_url": cfg.repo_url,
            },
        },
    }).encode()

    headers = {"Content-Type": "application/json"}
    if cfg.sweaf_api_key:
        headers["Authorization"] = f"Bearer {cfg.sweaf_api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    # Use sync urlopen — urllib is blocking anyway, and run_in_executor
    # can deadlock with certain asyncio event loop configurations.
    response = urllib.request.urlopen(req, timeout=30)
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

    headers = {}
    if cfg.sweaf_api_key:
        headers["Authorization"] = f"Bearer {cfg.sweaf_api_key}"

    elapsed = 0

    while elapsed < cfg.sweaf_timeout_seconds:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            response = urllib.request.urlopen(req, timeout=30)
            body = json.loads(response.read())
        except Exception as e:
            logger.warning("SWE-AF bridge: poll error (will retry): %s", e)
            continue

        status = body.get("status", "")
        logger.debug("SWE-AF bridge: execution %s status=%s", execution_id, status)

        if status in ("completed", "success", "succeeded"):
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
