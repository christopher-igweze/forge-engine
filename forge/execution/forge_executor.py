"""FORGE three-loop control system.

Adapts SWE-AF's three-nested-loop architecture with triggers specific
to remediation rather than construction.

Inner Loop:  Coder retry on REQUEST_CHANGES (max 3)
Middle Loop: Escalation when inner loop exhausted (RECLASSIFY/SPLIT/DEFER/ESCALATE)
Outer Loop:  Re-run Fix Strategist on remaining plan (max 1)

Philosophy: "First, do no harm." If a fix can't be applied cleanly within
3 attempts, defer it as documented technical debt rather than risk
destabilizing the codebase.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

try:
    from agentfield import Agent
except ImportError:
    from typing import Any as Agent  # Standalone: accepts StandaloneDispatcher

from forge.execution.context_broker import ForgeContextBroker
from forge.execution.json_utils import safe_parse_agent_response
from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    DeferredFindingContext,
    EscalationAction,
    EscalationDecision,
    FixOutcome,
    ForgeCodeReviewResult,
    ForgeExecutionState,
    InnerLoopState,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
    ReviewDecision,
    TestGeneratorResult,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig

logger = logging.getLogger(__name__)

# Max concurrent remediation fix executions (limits parallel opencode subprocesses)
_FIX_CONCURRENCY_LIMIT = asyncio.Semaphore(8)


# ── Test Failure Classification ──────────────────────────────────────


def _classify_test_failure(test_exec) -> str:
    """Classify a test failure as environment, test_bug, or code_bug.

    Returns:
        "environment" — env noise (urllib3, missing system deps) → ignore tests
        "test_bug"    — test itself broken (import error, 0 tests ran) → preserve APPROVE
        "code_bug"    — real assertion failures → override to REQUEST_CHANGES
    """
    err = test_exec.error_output.lower() if test_exec.error_output else ""

    # No tests ran at all → test is broken, not the code
    if test_exec.tests_run == 0:
        return "test_bug"

    # Environment patterns
    env_patterns = [
        "urllib3", "libressl", "openssl", "deprecationwarning",
        "insecurerequestwarning", "notopenssl",
        "npm warn", "npm err!", "experimentalwarning",
        "no module named 'flask_restful'",
        "no module named 'flask_sqlalchemy'",
        "no module named 'flask_cors'",
        "modulenotfounderror", "no module named",
        "pkg_resources",
    ]
    if any(p in err for p in env_patterns):
        return "environment"

    # Test bug patterns — test file itself has issues
    test_bug_patterns = [
        "importerror", "syntaxerror", "indentationerror",
        "nameerror", "typeerror: cannot read prop",
        "cannot find module", "module not found",
        "no tests found", "no test files found",
        "test suite failed to run",
        "referenceerror",
    ]
    if any(p in err for p in test_bug_patterns):
        return "test_bug"

    # Default: real test failure → the code has a bug
    return "code_bug"


def _store_deferral_context(
    state: ForgeExecutionState,
    finding_id: str,
    inner_state: InnerLoopState,
    escalation,
) -> None:
    """Store failure context when deferring a finding.

    This context flows through the convergence loop to give the Fix Strategist
    and next coder specific direction about what went wrong.
    """
    test_output = ""
    if inner_state.test_result and inner_state.test_result.coverage_summary:
        test_output = inner_state.test_result.coverage_summary[:500]
    # Also grab the review summary which often contains test failure details
    review_summary = ""
    if inner_state.review_result:
        review_summary = inner_state.review_result.summary[:500]

    state.outer_loop.deferred_context[finding_id] = DeferredFindingContext(
        finding_id=finding_id,
        attempts=inner_state.iteration,
        test_output=test_output or review_summary,
        review_feedback=inner_state.review_feedback[:500] if inner_state.review_feedback else "",
        escalation_reason=escalation.rationale[:500] if hasattr(escalation, "rationale") and escalation.rationale else "",
    )


# ── Test Retry (re-invoke Agent 9 with failure feedback) ─────────────


async def _retry_test_generation(
    app: Agent,
    node_id: str,
    finding: AuditFinding,
    code_change: CoderFixResult,
    code_diff: str,
    worktree_path: str,
    test_context: dict,
    failed_test_exec,
    original_test_contents: list,
    cfg: "ForgeConfig",
    resolved_models: dict[str, str],
):
    """Retry Agent 9 once when initial tests are broken (test_bug).

    Re-invokes test generator with original test code + failure output
    as feedback, writes new tests, runs them.

    Returns (new_test_result, new_test_exec) or (None, None).
    """
    logger.info("Test retry: re-invoking Agent 9 for %s", finding.title)

    original_test_code = "\n\n".join(
        f"# {tfc.path}\n{tfc.content}"
        for tfc in original_test_contents
    )
    error_output = (
        failed_test_exec.error_output
        if failed_test_exec and failed_test_exec.error_output
        else "Tests failed to run (0 tests executed)"
    )

    prior_test_failure = {
        "original_test_code": original_test_code[:4000],
        "error_output": error_output[:2000],
        "tests_run": failed_test_exec.tests_run if failed_test_exec else 0,
        "tests_passed": failed_test_exec.tests_passed if failed_test_exec else 0,
    }

    finding_dict = finding.model_dump()
    try:
        test_raw = await app.call(
            f"{node_id}.run_test_generator",
            finding=finding_dict,
            code_change=code_change.model_dump(),
            code_diff=code_diff,
            worktree_path=worktree_path,
            test_context=test_context,
            prior_test_failure=prior_test_failure,
            model=resolved_models.get("test_generator_model", "minimax/minimax-m2.5"),
            ai_provider=cfg.provider_for_role("test_generator"),
        )
    except Exception as e:
        logger.error("Test retry: Agent 9 call failed: %s", e)
        return None, None

    if isinstance(test_raw, Exception):
        logger.error("Test retry: Agent 9 returned error: %s", test_raw)
        return None, None

    new_test_result = TestGeneratorResult(**_unwrap(test_raw))

    if not new_test_result.test_file_contents:
        logger.warning("Test retry: Agent 9 returned no test files")
        return new_test_result, None

    # Write new tests to worktree
    for tfc in new_test_result.test_file_contents:
        test_path = os.path.join(worktree_path, tfc.path)
        try:
            os.makedirs(os.path.dirname(test_path), exist_ok=True)
            with open(test_path, "w") as f:
                f.write(tfc.content)
        except OSError as e:
            logger.warning("Test retry: failed to write %s: %s", test_path, e)

    # Run new tests
    try:
        from forge.execution.test_runner import run_tests_in_worktree
        new_test_exec = run_tests_in_worktree(
            worktree_path,
            test_files=[tfc.path for tfc in new_test_result.test_file_contents],
            timeout=120,
        )
    except Exception as e:
        logger.warning("Test retry: execution failed: %s", e)
        return new_test_result, None

    if new_test_exec and new_test_exec.success:
        logger.info("Test retry: SUCCESS — tests pass for %s", finding.title)
    elif new_test_exec:
        logger.info(
            "Test retry: still failing (%d/%d) for %s",
            new_test_exec.tests_passed, new_test_exec.tests_run, finding.title,
        )

    return new_test_result, new_test_exec


# ── Test Context Collector for Agent 9 ───────────────────────────────


def _collect_test_context(
    worktree_path: str,
    files_changed: list[str],
    codebase_map: dict | None = None,
) -> dict:
    """Pre-collect project context so Agent 9 can generate accurate tests.

    Returns a dict with:
        framework: detected test framework name
        source_files: {path: content} for modified files (max 3, 2KB each)
        existing_test_sample: content of one existing test file (for pattern matching)
        project_hints: relevant config snippets (package.json scripts, conftest, etc.)
    """
    from forge.execution.test_runner import detect_test_framework

    ctx: dict = {
        "framework": "",
        "source_files": {},
        "existing_test_sample": "",
        "project_hints": "",
    }

    root = os.path.join(worktree_path, "")

    # 1. Detect test framework
    ctx["framework"] = detect_test_framework(worktree_path)

    # 2. Read source code of modified files (max 3 files, 2KB each)
    for fp in files_changed[:3]:
        abs_path = os.path.join(worktree_path, fp) if not os.path.isabs(fp) else fp
        try:
            content = open(abs_path).read()[:2048]
            # Use relative path as key
            rel = fp if not os.path.isabs(fp) else os.path.relpath(fp, worktree_path)
            ctx["source_files"][rel] = content
        except OSError:
            pass

    # 3. Find an existing test file as a pattern example
    import glob as glob_mod
    test_patterns = [
        os.path.join(worktree_path, "tests", "test_*.py"),
        os.path.join(worktree_path, "test", "test_*.py"),
        os.path.join(worktree_path, "tests", "*.test.js"),
        os.path.join(worktree_path, "tests", "*.test.ts"),
        os.path.join(worktree_path, "__tests__", "*.test.js"),
        os.path.join(worktree_path, "__tests__", "*.test.ts"),
        os.path.join(worktree_path, "test", "*.spec.js"),
    ]
    for pattern in test_patterns:
        matches = glob_mod.glob(pattern)
        if matches:
            try:
                ctx["existing_test_sample"] = open(matches[0]).read()[:3000]
            except OSError:
                pass
            break

    # 4. Project hints — config snippets relevant to testing
    hints_parts = []

    # package.json test script
    pkg_json = os.path.join(worktree_path, "package.json")
    if os.path.exists(pkg_json):
        try:
            import json
            pkg = json.loads(open(pkg_json).read())
            scripts = pkg.get("scripts", {})
            dev_deps = list(pkg.get("devDependencies", {}).keys())
            if scripts.get("test"):
                hints_parts.append(f"Test script: {scripts['test']}")
            if dev_deps:
                hints_parts.append(f"Dev dependencies: {', '.join(dev_deps[:15])}")
        except (OSError, json.JSONDecodeError):
            pass

    # conftest.py
    conftest = os.path.join(worktree_path, "conftest.py")
    if not os.path.exists(conftest):
        conftest = os.path.join(worktree_path, "tests", "conftest.py")
    if os.path.exists(conftest):
        try:
            hints_parts.append(f"conftest.py:\n{open(conftest).read()[:1500]}")
        except OSError:
            pass

    # requirements.txt — so Agent 9 knows what's available
    for req_file in ("requirements.txt", "requirements-dev.txt"):
        req_path = os.path.join(worktree_path, req_file)
        if os.path.exists(req_path):
            try:
                hints_parts.append(f"{req_file}:\n{open(req_path).read()[:1000]}")
            except OSError:
                pass
            break

    ctx["project_hints"] = "\n\n".join(hints_parts)
    return ctx


# ── Inner Loop: Coder → Review → Retry ───────────────────────────────


async def run_inner_loop(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    worktree_path: str,
    codebase_map: dict | None,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    prior_changes: str = "",
) -> InnerLoopState:
    """Execute the inner control loop for a single finding.

    Coder produces a fix → Code Reviewer evaluates → retry if REQUEST_CHANGES.
    Max iterations controlled by cfg.max_inner_retries.
    """
    loop_state = InnerLoopState(
        finding_id=finding.id,
        max_iterations=cfg.max_inner_retries,
    )

    finding_dict = finding.model_dump()

    # Select coder based on tier
    if item.tier == RemediationTier.TIER_3:
        coder_reasoner = f"{node_id}.run_coder_tier3"
        coder_model = resolved_models.get("coder_tier3_model", "minimax/minimax-m2.5")
    else:
        coder_reasoner = f"{node_id}.run_coder_tier2"
        coder_model = resolved_models.get("coder_tier2_model", "minimax/minimax-m2.5")

    review_feedback = ""

    for iteration in range(1, cfg.max_inner_retries + 1):
        loop_state.iteration = iteration
        logger.info(
            "Inner loop: %s — iteration %d/%d",
            finding.title, iteration, cfg.max_inner_retries,
        )

        # ── Step 1: Coder ──────────────────────────────────────────────
        coder_result_dict = await app.call(
            coder_reasoner,
            finding=finding_dict,
            worktree_path=worktree_path,
            codebase_map=codebase_map,
            review_feedback=review_feedback,
            prior_changes=prior_changes,
            iteration=iteration,
            model=coder_model,
            ai_provider=cfg.provider_for_role(
                "coder_tier3" if item.tier == RemediationTier.TIER_3 else "coder_tier2"
            ),
        )

        coder_result = CoderFixResult(**_unwrap(coder_result_dict))
        loop_state.coder_result = coder_result

        if coder_result.outcome in (FixOutcome.FAILED_RETRYABLE, FixOutcome.FAILED_ESCALATED):
            review_feedback = coder_result.error_message
            continue

        # Capture actual diff for reviewer context
        actual_diff = ""
        try:
            import subprocess
            _diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=worktree_path, capture_output=True, text=True, timeout=30,
            )
            actual_diff = _diff_proc.stdout[:10000] if _diff_proc.returncode == 0 else ""
        except Exception:
            pass  # Non-fatal: reviewer works without diff, just less context

        # ── Step 2: Test Generator + Code Reviewer (parallel) ──────────
        # Collect project context for Agent 9 so it generates accurate tests
        test_context = _collect_test_context(
            worktree_path,
            coder_result.files_changed or [],
            codebase_map,
        )

        test_coro = app.call(
            f"{node_id}.run_test_generator",
            finding=finding_dict,
            code_change=coder_result.model_dump(),
            code_diff=actual_diff,
            worktree_path=worktree_path,
            test_context=test_context,
            model=resolved_models.get("test_generator_model", "minimax/minimax-m2.5"),
            ai_provider=cfg.provider_for_role("test_generator"),
        )
        review_coro = app.call(
            f"{node_id}.run_code_reviewer",
            finding=finding_dict,
            code_change=coder_result.model_dump(),
            code_diff=actual_diff,
            codebase_map=codebase_map,
            model=resolved_models.get("code_reviewer_model", "minimax/minimax-m2.5"),
            ai_provider=cfg.provider_for_role("code_reviewer"),
        )

        test_raw, review_raw = await asyncio.gather(
            test_coro, review_coro, return_exceptions=True,
        )

        # Parse test result
        if isinstance(test_raw, Exception):
            logger.error("Test generator failed: %s", test_raw)
            loop_state.test_result = TestGeneratorResult(finding_id=finding.id)
        else:
            loop_state.test_result = TestGeneratorResult(**_unwrap(test_raw))

        # Write inline test files to the worktree
        if loop_state.test_result and loop_state.test_result.test_file_contents:
            for tfc in loop_state.test_result.test_file_contents:
                test_path = os.path.join(worktree_path, tfc.path)
                try:
                    os.makedirs(os.path.dirname(test_path), exist_ok=True)
                    with open(test_path, "w") as f:
                        f.write(tfc.content)
                    logger.debug("Wrote test file: %s", test_path)
                except OSError as e:
                    logger.warning("Failed to write test file %s: %s", test_path, e)

        # ── Step 2b: Execute tests as quality gate ─────────────────────
        if loop_state.test_result and loop_state.test_result.test_file_contents:
            try:
                from forge.execution.test_runner import run_tests_in_worktree
                test_exec = run_tests_in_worktree(
                    worktree_path,
                    test_files=[tfc.path for tfc in loop_state.test_result.test_file_contents],
                    timeout=120,
                )
                if test_exec and not test_exec.success:
                    logger.info(
                        "Tests failed (%d/%d passed) for %s: %s",
                        test_exec.tests_passed, test_exec.tests_run,
                        finding.title, test_exec.error_output[:200],
                    )
            except Exception as e:
                logger.warning("Test execution failed (non-fatal): %s", e)
                test_exec = None
        else:
            test_exec = None

        # Parse review result
        if isinstance(review_raw, Exception):
            logger.error("Code reviewer failed: %s", review_raw)
            loop_state.review_result = ForgeCodeReviewResult(
                finding_id=finding.id,
                decision=ReviewDecision.REQUEST_CHANGES,
                summary=f"Review failed: {review_raw}",
            )
        else:
            loop_state.review_result = ForgeCodeReviewResult(**_unwrap(review_raw))

        # ── Step 3: Decision ───────────────────────────────────────────
        # Smart quality gate: classify test failures before overriding
        if test_exec and not test_exec.success:
            failure_class = _classify_test_failure(test_exec)
            logger.info(
                "Inner loop: test failure classified as '%s' for %s",
                failure_class, finding.title,
            )

            if failure_class == "code_bug":
                # Real test failure → override APPROVE to REQUEST_CHANGES
                if loop_state.review_result.decision == ReviewDecision.APPROVE:
                    loop_state.review_result.decision = ReviewDecision.REQUEST_CHANGES
                    loop_state.review_result.summary = (
                        f"Code review passed but tests found code issues "
                        f"({test_exec.tests_failed}/{test_exec.tests_run}): "
                        f"{test_exec.error_output[:300]}"
                    )
                    logger.info("Inner loop: overriding APPROVE → REQUEST_CHANGES (code_bug)")
            elif failure_class == "environment":
                logger.info("Inner loop: ignoring test failure (environment noise) — preserving review decision")
            elif failure_class == "test_bug":
                logger.info("Inner loop: test_bug detected — retrying Agent 9 for %s", finding.title)
                retry_test_result, retry_test_exec = await _retry_test_generation(
                    app, node_id, finding, coder_result, actual_diff,
                    worktree_path, test_context,
                    failed_test_exec=test_exec,
                    original_test_contents=(
                        loop_state.test_result.test_file_contents
                        if loop_state.test_result else []
                    ),
                    cfg=cfg,
                    resolved_models=resolved_models,
                )
                if retry_test_result:
                    loop_state.test_result = retry_test_result
                if retry_test_exec and retry_test_exec.success:
                    logger.info("Inner loop: retried tests pass — confirmed fix for %s", finding.title)
                elif retry_test_exec and not retry_test_exec.success:
                    retry_class = _classify_test_failure(retry_test_exec)
                    if retry_class == "code_bug":
                        if loop_state.review_result.decision == ReviewDecision.APPROVE:
                            loop_state.review_result.decision = ReviewDecision.REQUEST_CHANGES
                            loop_state.review_result.summary = (
                                f"Retried tests found code issues "
                                f"({retry_test_exec.tests_failed}/{retry_test_exec.tests_run}): "
                                f"{retry_test_exec.error_output[:300]}"
                            )
                            logger.info("Inner loop: retried tests → code_bug → REQUEST_CHANGES")
                    else:
                        logger.info("Inner loop: retried tests still broken (%s) — preserving review decision", retry_class)
                else:
                    logger.info("Inner loop: test retry returned no exec result — preserving review decision")

        if loop_state.review_result.decision == ReviewDecision.APPROVE:
            coder_result.outcome = FixOutcome.COMPLETED
            loop_state.coder_result = coder_result
            logger.info("Inner loop: APPROVED — %s", finding.title)
            return loop_state

        if loop_state.review_result.decision == ReviewDecision.BLOCK:
            coder_result.outcome = FixOutcome.FAILED_ESCALATED
            loop_state.coder_result = coder_result
            logger.info("Inner loop: BLOCKED — %s", finding.title)
            return loop_state

        # REQUEST_CHANGES — retry with feedback
        review_feedback = loop_state.review_result.summary
        logger.info("Inner loop: REQUEST_CHANGES — retrying %s", finding.title)

    # Exhausted retries
    if loop_state.coder_result:
        loop_state.coder_result.outcome = FixOutcome.FAILED_RETRYABLE
    logger.info(
        "Inner loop: exhausted %d retries for %s",
        cfg.max_inner_retries, finding.title,
    )
    return loop_state


# ── Middle Loop: Escalation ───────────────────────────────────────────


async def run_middle_loop(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    inner_state: InnerLoopState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str] | None = None,
) -> EscalationDecision:
    """Execute the middle loop when the inner loop is exhausted.

    Decides: RECLASSIFY, SPLIT, DEFER, or ESCALATE.
    Uses an LLM escalation agent with heuristic fallback.
    """
    logger.info("Middle loop: evaluating %s", finding.title)

    review = inner_state.review_result

    # Fast path: BLOCKED by reviewer — defer immediately (no LLM needed)
    if review and review.decision == ReviewDecision.BLOCK:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.DEFER,
            rationale=f"Blocked by reviewer: {review.summary}",
        )

    # Try LLM escalation agent
    if resolved_models:
        try:
            return await _llm_escalation(
                app, node_id, item, finding, inner_state, cfg, resolved_models,
            )
        except Exception as e:
            logger.warning("LLM escalation agent failed, falling back to heuristic: %s", e, exc_info=True)

    # Heuristic fallback
    return _heuristic_escalation(item, finding)


async def _llm_escalation(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    inner_state: InnerLoopState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> EscalationDecision:
    """Use the LLM escalation agent to decide the next action."""
    from forge.prompts.escalation_agent import (
        ESCALATION_SYSTEM_PROMPT,
        build_escalation_task,
    )

    task_prompt = build_escalation_task(
        finding=finding.model_dump(),
        coder_result=inner_state.coder_result.model_dump() if inner_state.coder_result else None,
        review_result=inner_state.review_result.model_dump() if inner_state.review_result else None,
        current_tier=item.tier.value,
        iteration_count=inner_state.iteration,
    )

    model = resolved_models.get("fix_strategist_model", "minimax/minimax-m2.5")
    provider = cfg.provider_for_role("fix_strategist")

    result = await app.call(
        f"{node_id}.run_escalation_agent",
        system_prompt=ESCALATION_SYSTEM_PROMPT,
        task_prompt=task_prompt,
        model=model,
        ai_provider=provider,
    )

    # Parse the response
    result_dict = safe_parse_agent_response(result)
    if not result_dict:
        raise ValueError("Empty response from escalation agent")

    action_str = result_dict.get("action", "DEFER").upper()
    action = (
        EscalationAction(action_str)
        if action_str in EscalationAction.__members__
        else EscalationAction.DEFER
    )

    decision = EscalationDecision(
        finding_id=finding.id,
        action=action,
        rationale=result_dict.get("rationale", "LLM escalation decision"),
    )

    if action == EscalationAction.RECLASSIFY:
        new_tier_val = result_dict.get("new_tier", 3)
        decision.new_tier = RemediationTier(new_tier_val)

    if action == EscalationAction.SPLIT:
        split_items_raw = result_dict.get("split_items", [])
        for si in split_items_raw:
            decision.split_items.append(RemediationItem(
                finding_id=f"{finding.id}-split-{len(decision.split_items) + 1}",
                title=si.get("title", finding.title),
                tier=RemediationTier.TIER_2,
                priority=item.priority,
                estimated_files=si.get("estimated_files", 1),
            ))

    logger.info("LLM escalation: %s → %s (%s)", finding.id, action.value, decision.rationale)
    return decision


def _heuristic_escalation(
    item: RemediationItem,
    finding: AuditFinding,
) -> EscalationDecision:
    """Fallback heuristic escalation logic."""
    if item.tier == RemediationTier.TIER_2:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.RECLASSIFY,
            rationale="Tier 2 fix failed after retries — promoting to Tier 3 for broader context",
            new_tier=RemediationTier.TIER_3,
        )

    if item.tier == RemediationTier.TIER_3:
        return EscalationDecision(
            finding_id=finding.id,
            action=EscalationAction.DEFER,
            rationale="Tier 3 fix failed after retries — deferring as technical debt",
        )

    return EscalationDecision(
        finding_id=finding.id,
        action=EscalationAction.DEFER,
        rationale="Could not fix within retry budget",
    )


# ── Outer Loop: Re-plan ──────────────────────────────────────────────


async def run_outer_loop(
    app: Agent,
    node_id: str,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> RemediationPlan | None:
    """Execute the outer loop — re-run Fix Strategist on remaining items.

    Only triggers when multiple fixes in the same module are failing
    or dependency conflicts are detected.
    """
    if state.outer_loop.iteration >= cfg.max_outer_replans:
        logger.info("Outer loop: max replans reached (%d)", cfg.max_outer_replans)
        return None

    escalated = [e for e in state.outer_loop.escalations if e.action == EscalationAction.ESCALATE]
    if not escalated:
        logger.info("Outer loop: no escalations requiring replan")
        return None

    logger.info("Outer loop: replanning with %d escalations", len(escalated))
    state.outer_loop.iteration += 1

    # Re-run Fix Strategist with remaining findings (excluding completed/deferred)
    completed_ids = {f.finding_id for f in state.completed_fixes}
    deferred_ids = set(state.outer_loop.deferred_findings)
    excluded = completed_ids | deferred_ids

    remaining_findings = [
        f.model_dump() for f in state.all_findings
        if f.id not in excluded
    ]

    if not remaining_findings:
        logger.info("Outer loop: no remaining findings to replan")
        return None

    codebase_map_dict = state.codebase_map.model_dump() if state.codebase_map else {}

    plan_dict = await app.call(
        f"{node_id}.run_fix_strategist",
        all_findings=remaining_findings,
        codebase_map=codebase_map_dict,
        triage_result=state.triage_result.model_dump() if state.triage_result else None,
        model=resolved_models.get("fix_strategist_model", "minimax/minimax-m2.5"),
        ai_provider=cfg.provider_for_role("fix_strategist"),
    )

    try:
        return RemediationPlan(**_unwrap(plan_dict))
    except Exception as e:
        logger.error("Outer loop: failed to parse replan: %s", e)
        return None


# ── Full Remediation Executor ─────────────────────────────────────────


async def execute_remediation(
    app: Agent,
    node_id: str,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
) -> None:
    """Execute the full remediation phase with three control loops.

    Iterates through the remediation plan level by level,
    running fixes in parallel within each level.
    """
    plan = state.remediation_plan
    if not plan or not plan.items:
        logger.info("Remediation: no items in plan")
        return

    # Install project dependencies once before creating worktrees.
    # Worktrees symlink node_modules/etc. from the main repo.
    from forge.execution.worktree import install_project_deps
    install_project_deps(state.repo_path)

    # Create shared context broker for cross-agent coordination
    broker = ForgeContextBroker()

    # Build finding lookup
    finding_map: dict[str, AuditFinding] = {f.id: f for f in state.all_findings}

    # Execute level by level
    for level_idx, level_ids in enumerate(plan.execution_levels):
        logger.info("Remediation: level %d — %d items", level_idx, len(level_ids))

        # Get items for this level
        item_map = {i.finding_id: i for i in plan.items}
        level_items = [
            item_map[fid] for fid in level_ids
            if fid in item_map and fid not in state.outer_loop.deferred_findings
        ]

        if not level_items:
            continue

        # Run fixes in parallel within each level
        tasks = []
        for item in level_items:
            finding = finding_map.get(item.finding_id)
            if not finding:
                logger.warning("Finding %s not found — skipping", item.finding_id)
                continue

            # Skip Tier 0 and Tier 1 (handled by tier_router)
            if item.tier in (RemediationTier.TIER_0, RemediationTier.TIER_1):
                continue

            tasks.append(_execute_single_fix_throttled(
                app, node_id, item, finding, state, cfg, resolved_models, broker,
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Outer loop: replan if needed ───────────────────────────────────
    new_plan = await run_outer_loop(app, node_id, state, cfg, resolved_models)
    if new_plan and new_plan.items:
        logger.info("Outer loop: replanning with %d items", len(new_plan.items))
        state.remediation_plan = new_plan
        # Re-execute with new plan (recursive, but max 1 replan)
        await execute_remediation(app, node_id, state, cfg, resolved_models)


async def _execute_single_fix_throttled(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    broker: ForgeContextBroker | None = None,
) -> None:
    """Throttled wrapper that limits concurrent fix executions."""
    async with _FIX_CONCURRENCY_LIMIT:
        return await _execute_single_fix(
            app, node_id, item, finding, state, cfg, resolved_models, broker,
        )


async def _execute_single_fix(
    app: Agent,
    node_id: str,
    item: RemediationItem,
    finding: AuditFinding,
    state: ForgeExecutionState,
    cfg: ForgeConfig,
    resolved_models: dict[str, str],
    broker: ForgeContextBroker | None = None,
) -> None:
    """Execute a single fix through inner + middle loops.

    Each fix runs in an isolated git worktree to avoid conflicts
    with parallel fixes.
    """
    from forge.execution.worktree import (
        create_worktree,
        merge_worktree,
        remove_worktree,
        get_current_branch,
    )

    codebase_map = state.codebase_map.model_dump() if state.codebase_map else None

    # Skip .ipynb files — notebook remediation not yet supported
    if finding.locations and all(
        loc.file_path.endswith(".ipynb") for loc in finding.locations
    ):
        logger.info(
            "Skipping %s — .ipynb remediation not supported", finding.title,
        )
        # Track as deferred — do NOT append to completed_fixes
        state.outer_loop.deferred_findings.append(finding.id)
        return

    # Create isolated worktree for this fix
    try:
        worktree_path = create_worktree(
            state.repo_path,
            finding.id,
            base_branch=get_current_branch(state.repo_path),
        )
    except Exception as e:
        logger.error("Failed to create worktree for %s: %s", finding.id, e)
        worktree_path = state.repo_path  # Fallback to main repo

    try:
        # Claim files in the shared context broker
        prior_changes = ""
        if broker:
            claimed_files = [loc.file_path for loc in finding.locations] if finding.locations else []
            conflicts = await broker.claim_files(finding.id, claimed_files)
            if conflicts:
                logger.info("Fix %s: file conflicts with other fixes: %s", finding.id, conflicts)
            prior_changes = await broker.get_prior_changes_context(finding.id)

        # ── Inner loop ─────────────────────────────────────────────────
        inner_state = await run_inner_loop(
            app, node_id, item, finding, worktree_path, codebase_map, cfg, resolved_models,
            prior_changes=prior_changes,
        )
        state.inner_loop_states[finding.id] = inner_state
        state.total_agent_invocations += inner_state.iteration * 3  # coder + test + review per iter

        # Check outcome
        if inner_state.coder_result and inner_state.coder_result.outcome == FixOutcome.COMPLETED:
            # Merge worktree back into main branch
            if worktree_path != state.repo_path:
                merged = merge_worktree(
                    state.repo_path,
                    worktree_path,
                    target_branch=get_current_branch(state.repo_path),
                )
                if merged:
                    # Record completion in shared context
                    if broker:
                        import subprocess as sp
                        try:
                            merge_diff = sp.run(
                                ["git", "log", "-1", "--format=", "-p"],
                                cwd=state.repo_path, capture_output=True, text=True, timeout=10,
                            ).stdout[:5000]
                        except Exception:
                            merge_diff = ""
                        await broker.record_completion(
                            finding.id, merge_diff, inner_state.coder_result.summary if inner_state.coder_result else ""
                        )
                else:
                    logger.warning("Merge failed for %s — marking as debt", finding.id)
                    inner_state.coder_result.outcome = FixOutcome.COMPLETED_WITH_DEBT
            state.completed_fixes.append(inner_state.coder_result)
            return

        # ── Middle loop ────────────────────────────────────────────────
        escalation = await run_middle_loop(
            app, node_id, item, finding, inner_state, cfg, resolved_models,
        )
        state.outer_loop.escalations.append(escalation)

        if escalation.action == EscalationAction.DEFER:
            state.outer_loop.deferred_findings.append(finding.id)
            # Store failure context so convergence loop can give the coder direction
            _store_deferral_context(state, finding.id, inner_state, escalation)
            # Do NOT append to completed_fixes — deferred items tracked separately
            if broker:
                await broker.record_failure(finding.id, "deferred")

        elif escalation.action == EscalationAction.RECLASSIFY and escalation.new_tier:
            # Promote to higher tier and retry
            item.tier = escalation.new_tier
            new_inner = await run_inner_loop(
                app, node_id, item, finding, worktree_path, codebase_map, cfg, resolved_models,
                prior_changes=prior_changes,
            )
            state.inner_loop_states[finding.id] = new_inner

            if new_inner.coder_result and new_inner.coder_result.outcome == FixOutcome.COMPLETED:
                if worktree_path != state.repo_path:
                    merged = merge_worktree(
                        state.repo_path,
                        worktree_path,
                        target_branch=get_current_branch(state.repo_path),
                    )
                    if not merged:
                        new_inner.coder_result.outcome = FixOutcome.COMPLETED_WITH_DEBT
                state.completed_fixes.append(new_inner.coder_result)
            else:
                state.outer_loop.deferred_findings.append(finding.id)
                _store_deferral_context(state, finding.id, new_inner, escalation)

        elif escalation.action == EscalationAction.ESCALATE:
            # Will be handled by outer loop
            pass

    finally:
        # Release file claims as safety net
        if broker:
            await broker.release_files(finding.id)

        # Clean up worktree (unless it was the fallback)
        if worktree_path != state.repo_path:
            try:
                remove_worktree(state.repo_path, worktree_path)
            except Exception as e:
                logger.warning("Failed to clean up worktree %s: %s", worktree_path, e)


# ── Helpers ───────────────────────────────────────────────────────────


def _unwrap(result) -> dict:
    """Handle AgentField envelope unwrapping with resilient JSON parsing."""
    return safe_parse_agent_response(result)
