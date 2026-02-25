"""Discovery-mode reasoners: Agents 1-4.

Agent 1: Codebase Analyst — hybrid deterministic scan + LLM summary
Agent 2: Security Auditor — 3 parallel passes (auth, data, infra)
Agent 3: Quality Auditor — 3 parallel passes (error handling, patterns, perf)
Agent 4: Architecture Reviewer — structural coherence analysis
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from forge.vendor.agent_ai import AgentAI, AgentAIConfig

from forge.execution.context_builder import (
    build_codebase_inventory,
    build_file_tree,
    read_package_manifests,
    select_files_for_pass,
    select_files_for_quality_pass,
)
from forge.prompts.codebase_analyst import (
    SYSTEM_PROMPT as ANALYST_SYSTEM_PROMPT,
    codebase_analyst_task_prompt,
)
from forge.prompts.security_auditor import (
    PASS_SYSTEM_PROMPTS as SECURITY_PASS_PROMPTS,
    security_audit_task_prompt,
)
from forge.prompts.quality_auditor import (
    PASS_SYSTEM_PROMPTS as QUALITY_PASS_PROMPTS,
    quality_audit_task_prompt,
)
from forge.prompts.architecture_reviewer import (
    SYSTEM_PROMPT as ARCH_REVIEWER_SYSTEM_PROMPT,
    architecture_review_task_prompt,
)
from forge.schemas import (
    AuditFinding,
    AuditPassType,
    CodebaseMap,
    FileEntry,
    FindingCategory,
    QualityAuditAggregate,
    QualityAuditResult,
    ArchitectureReviewResult,
    SecurityAuditAggregate,
    SecurityAuditResult,
)

from . import router

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response, stripping markdown fences."""
    cleaned = text.strip()
    # Strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON from LLM response, returning empty dict")
        return {}


# ── Agent 1: Codebase Analyst ─────────────────────────────────────────


@router.reasoner()
async def run_codebase_analyst(
    repo_path: str,
    repo_url: str = "",
    artifacts_dir: str = "",
    model: str = "minimax/minimax-m2.5",
    ai_provider: str = "openrouter_direct",
) -> dict:
    """Agent 1: Analyze codebase and produce CodebaseMap.

    Hybrid approach:
      1. Deterministic file scanning (os.walk, LOC counting, language detection)
      2. Single LLM call for architectural analysis and pattern recognition
    """
    logger.info("Agent 1: Codebase Analyst starting for %s", repo_url or repo_path)

    # ── Step 1: Deterministic inventory ────────────────────────────────
    file_inventory = build_codebase_inventory(repo_path)
    file_tree = build_file_tree(repo_path)
    package_manifests = read_package_manifests(repo_path)

    # Pre-populate CodebaseMap with deterministic data
    files = [FileEntry(**f) for f in file_inventory]
    loc_total = sum(f.loc for f in files)
    languages = sorted(set(f.language for f in files if f.language))
    primary_lang = max(
        set(f.language for f in files if f.language),
        key=lambda l: sum(f.loc for f in files if f.language == l),
        default="",
    ) if files else ""

    # ── Step 2: LLM architectural analysis ─────────────────────────────
    # Build concise file listing for the prompt (skip LOC details)
    file_listing = "\n".join(f"  {f.path}" for f in files[:200])
    sample_content = f"File listing ({len(files)} files):\n{file_listing}"

    task = codebase_analyst_task_prompt(
        file_tree=file_tree,
        package_manifests=package_manifests,
        sample_files=sample_content,
        repo_url=repo_url,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=repo_path,
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="codebase_analyst",
    ))

    response = await ai.run(
        task,
        system_prompt=ANALYST_SYSTEM_PROMPT,
    )

    # ── Step 3: Merge deterministic + LLM results ─────────────────────
    llm_data = {}
    if response.parsed:
        llm_data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        llm_data = _parse_json_response(response.text)

    # Build final CodebaseMap — deterministic data is authoritative,
    # LLM provides architectural analysis
    codebase_map = CodebaseMap(
        files=files,
        loc_total=loc_total,
        file_count=len(files),
        primary_language=primary_lang,
        languages=languages,
        # LLM-provided fields
        modules=llm_data.get("modules", []),
        dependencies=llm_data.get("dependencies", []),
        data_flows=llm_data.get("data_flows", llm_data.get("dataFlows", [])),
        auth_boundaries=llm_data.get("auth_boundaries", llm_data.get("authBoundaries", [])),
        entry_points=llm_data.get("entry_points", llm_data.get("entryPoints", [])),
        tech_stack=llm_data.get("tech_stack", llm_data.get("techStack", {})),
        architecture_summary=llm_data.get("architecture_summary", ""),
        key_patterns=llm_data.get("key_patterns", llm_data.get("keyPatterns", [])),
    )

    # Save artifact
    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/codebase_map.json", codebase_map.model_dump())

    logger.info(
        "Agent 1: Complete — %d files, %d LOC, %d modules",
        codebase_map.file_count, codebase_map.loc_total, len(codebase_map.modules),
    )
    return codebase_map.model_dump()


# ── Agent 2: Security Auditor ─────────────────────────────────────────


async def _run_single_security_pass(
    audit_pass: AuditPassType,
    repo_path: str,
    codebase_map: CodebaseMap,
    model: str,
    ai_provider: str,
) -> SecurityAuditResult:
    """Execute a single security audit pass."""
    logger.info("Agent 2: Security pass %s starting", audit_pass.value)

    # Select relevant files for this pass
    file_contents = select_files_for_pass(
        repo_path, audit_pass, codebase_map,
    )

    codebase_map_json = json.dumps(
        {
            "modules": [m.model_dump() for m in codebase_map.modules],
            "entry_points": [e.model_dump() for e in codebase_map.entry_points],
            "auth_boundaries": [a.model_dump() for a in codebase_map.auth_boundaries],
            "tech_stack": codebase_map.tech_stack.model_dump()
            if hasattr(codebase_map.tech_stack, "model_dump")
            else codebase_map.tech_stack,
        },
        indent=2,
    )

    task = security_audit_task_prompt(
        audit_pass=audit_pass,
        codebase_map_json=codebase_map_json,
        relevant_file_contents=file_contents,
    )

    system_prompt = SECURITY_PASS_PROMPTS[audit_pass]

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=repo_path,
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name=f"security_auditor/{audit_pass.value}",
    ))

    response = await ai.run(task, system_prompt=system_prompt)

    # Parse response
    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    findings = []
    for f_data in data.get("findings", []):
        f_data["agent"] = "security_auditor"
        f_data["audit_pass"] = audit_pass.value
        if "category" not in f_data:
            f_data["category"] = "security"
        try:
            findings.append(AuditFinding(**f_data))
        except Exception as e:
            logger.warning("Failed to parse finding: %s — %s", f_data.get("title", "?"), e)

    result = SecurityAuditResult(
        audit_pass=audit_pass,
        findings=findings,
        pass_summary=data.get("pass_summary", ""),
        files_analyzed=data.get("files_analyzed", 0),
    )

    logger.info(
        "Agent 2: Security pass %s complete — %d findings",
        audit_pass.value, len(findings),
    )
    return result


@router.reasoner()
async def run_security_auditor(
    repo_path: str,
    codebase_map: dict,
    artifacts_dir: str = "",
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
    parallel: bool = True,
) -> dict:
    """Agent 2: Run 3 security audit passes (optionally in parallel).

    Passes: auth_flow, data_handling, infrastructure.
    """
    logger.info("Agent 2: Security Auditor starting")
    cm = CodebaseMap(**codebase_map)

    security_passes = [
        AuditPassType.AUTH_FLOW,
        AuditPassType.DATA_HANDLING,
        AuditPassType.INFRASTRUCTURE,
    ]

    if parallel:
        results = await asyncio.gather(
            *[
                _run_single_security_pass(p, repo_path, cm, model, ai_provider)
                for p in security_passes
            ],
            return_exceptions=True,
        )
        pass_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("Security pass failed: %s", r)
                pass_results.append(SecurityAuditResult(
                    audit_pass=AuditPassType.AUTH_FLOW,
                    findings=[],
                    pass_summary=f"Pass failed: {r}",
                ))
            else:
                pass_results.append(r)
    else:
        pass_results = []
        for p in security_passes:
            result = await _run_single_security_pass(
                p, repo_path, cm, model, ai_provider,
            )
            pass_results.append(result)

    # Aggregate
    all_findings = []
    for pr in pass_results:
        all_findings.extend(pr.findings)

    aggregate = SecurityAuditAggregate(
        findings=all_findings,
        pass_results=pass_results,
        total_findings=len(all_findings),
        critical_count=sum(1 for f in all_findings if f.severity.value == "critical"),
        high_count=sum(1 for f in all_findings if f.severity.value == "high"),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/security_findings.json", aggregate.model_dump())

    logger.info(
        "Agent 2: Complete — %d total findings (%d critical, %d high)",
        aggregate.total_findings, aggregate.critical_count, aggregate.high_count,
    )
    return aggregate.model_dump()


# ── Agent 3: Quality Auditor ──────────────────────────────────────────


async def _run_single_quality_pass(
    audit_pass: AuditPassType,
    repo_path: str,
    codebase_map: CodebaseMap,
    model: str,
    ai_provider: str,
) -> QualityAuditResult:
    """Execute a single quality audit pass."""
    logger.info("Agent 3: Quality pass %s starting", audit_pass.value)

    file_contents = select_files_for_quality_pass(
        repo_path, audit_pass, codebase_map,
    )

    codebase_map_json = json.dumps(
        {
            "modules": [m.model_dump() for m in codebase_map.modules],
            "tech_stack": codebase_map.tech_stack.model_dump()
            if hasattr(codebase_map.tech_stack, "model_dump")
            else codebase_map.tech_stack,
            "primary_language": codebase_map.primary_language,
        },
        indent=2,
    )

    task = quality_audit_task_prompt(
        audit_pass=audit_pass,
        codebase_map_json=codebase_map_json,
        relevant_file_contents=file_contents,
    )

    system_prompt = QUALITY_PASS_PROMPTS[audit_pass]

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=repo_path,
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name=f"quality_auditor/{audit_pass.value}",
    ))

    response = await ai.run(task, system_prompt=system_prompt)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    findings = []
    for f_data in data.get("findings", []):
        f_data["agent"] = "quality_auditor"
        f_data["audit_pass"] = audit_pass.value
        if "category" not in f_data:
            f_data["category"] = "quality"
        try:
            findings.append(AuditFinding(**f_data))
        except Exception as e:
            logger.warning("Failed to parse quality finding: %s — %s", f_data.get("title", "?"), e)

    result = QualityAuditResult(
        audit_pass=audit_pass,
        findings=findings,
        pass_summary=data.get("pass_summary", ""),
    )

    logger.info("Agent 3: Quality pass %s complete — %d findings", audit_pass.value, len(findings))
    return result


@router.reasoner()
async def run_quality_auditor(
    repo_path: str,
    codebase_map: dict,
    artifacts_dir: str = "",
    model: str = "minimax/minimax-m2.5",
    ai_provider: str = "openrouter_direct",
    parallel: bool = True,
) -> dict:
    """Agent 3: Run 3 quality audit passes (optionally in parallel).

    Passes: error_handling, code_patterns, performance.
    """
    logger.info("Agent 3: Quality Auditor starting")
    cm = CodebaseMap(**codebase_map)

    quality_passes = [
        AuditPassType.ERROR_HANDLING,
        AuditPassType.CODE_PATTERNS,
        AuditPassType.PERFORMANCE,
    ]

    if parallel:
        results = await asyncio.gather(
            *[_run_single_quality_pass(p, repo_path, cm, model, ai_provider) for p in quality_passes],
            return_exceptions=True,
        )
        pass_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("Quality pass failed: %s", r)
                pass_results.append(QualityAuditResult(
                    audit_pass=AuditPassType.ERROR_HANDLING,
                    findings=[],
                    pass_summary=f"Pass failed: {r}",
                ))
            else:
                pass_results.append(r)
    else:
        pass_results = []
        for p in quality_passes:
            result = await _run_single_quality_pass(p, repo_path, cm, model, ai_provider)
            pass_results.append(result)

    all_findings = []
    for pr in pass_results:
        all_findings.extend(pr.findings)

    aggregate = QualityAuditAggregate(
        findings=all_findings,
        pass_results=pass_results,
        total_findings=len(all_findings),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/quality_findings.json", aggregate.model_dump())

    logger.info("Agent 3: Complete — %d total quality findings", aggregate.total_findings)
    return aggregate.model_dump()


# ── Agent 4: Architecture Reviewer ────────────────────────────────────


@router.reasoner()
async def run_architecture_reviewer(
    repo_path: str,
    codebase_map: dict,
    artifacts_dir: str = "",
    model: str = "anthropic/claude-haiku-4.5",
    ai_provider: str = "openrouter_direct",
) -> dict:
    """Agent 4: Architecture review — structural coherence analysis."""
    logger.info("Agent 4: Architecture Reviewer starting")
    cm = CodebaseMap(**codebase_map)

    codebase_map_json = json.dumps(cm.model_dump(), indent=2, default=str)

    # Build a simple dependency graph from module data
    dep_lines = []
    for mod in cm.modules:
        dep_lines.append(f"  {mod.name} ({mod.path}): {mod.loc} LOC, {len(mod.files)} files")
    module_graph = "\n".join(dep_lines) if dep_lines else "(no modules detected)"

    task = architecture_review_task_prompt(
        codebase_map_json=codebase_map_json,
        module_dependency_graph=module_graph,
    )

    ai = AgentAI(AgentAIConfig(
        provider=ai_provider,
        model=model,
        cwd=repo_path,
        max_turns=1,
        allowed_tools=[],
        env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
        agent_name="architecture_reviewer",
    ))

    response = await ai.run(task, system_prompt=ARCH_REVIEWER_SYSTEM_PROMPT)

    data = {}
    if response.parsed:
        data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
    elif response.text:
        data = _parse_json_response(response.text)

    findings = []
    for f_data in data.get("findings", []):
        f_data["agent"] = "architecture_reviewer"
        if "category" not in f_data:
            f_data["category"] = "architecture"
        try:
            findings.append(AuditFinding(**f_data))
        except Exception as e:
            logger.warning("Failed to parse arch finding: %s — %s", f_data.get("title", "?"), e)

    result = ArchitectureReviewResult(
        findings=findings,
        structural_coherence_score=data.get("structural_coherence_score", 0),
        coupling_assessment=data.get("coupling_assessment", ""),
        layering_assessment=data.get("layering_assessment", ""),
        summary=data.get("summary", ""),
    )

    if artifacts_dir:
        _save_artifact(artifacts_dir, "scan/architecture_findings.json", result.model_dump())

    logger.info(
        "Agent 4: Complete — %d findings, coherence score: %d",
        len(findings), result.structural_coherence_score,
    )
    return result.model_dump()


# ── Artifact persistence ──────────────────────────────────────────────


def _save_artifact(artifacts_dir: str, rel_path: str, data: dict) -> None:
    """Save a JSON artifact to the artifacts directory."""
    from pathlib import Path

    full_path = Path(artifacts_dir) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Saved artifact: %s", full_path)
