"""Intent Analyzer — determines if findings are intentional developer choices.

Two-phase approach:
  Phase 1: Deterministic annotation detection (zero LLM cost)
    - Suppression annotations (# noqa, // eslint-disable, @SuppressWarnings)
    - Intent-indicating comments ("by design", "deliberately", etc.)
    - Test file detection via QAConventions + common patterns
  Phase 2: LLM batch analysis for remaining ambiguous findings
    - Batches of 30 findings per LLM call
    - Uses minimax-m2.5 (~$0.005/scan) via openrouter_direct
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from forge.conventions.models import ProjectConventions, QAConventions
from forge.execution.json_utils import strip_json_fences
from forge.prompts.intent_analyzer import (
    INTENT_ANALYZER_SYSTEM_PROMPT,
    intent_analyzer_task_prompt,
)
from forge.schemas import AuditFinding

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_LLM_BATCH_SIZE = 30

# Suppression annotation patterns (compiled once)
_SUPPRESSION_PATTERNS: list[re.Pattern[str]] = [
    # Python
    re.compile(r"#\s*noqa", re.IGNORECASE),
    re.compile(r"#\s*type:\s*ignore", re.IGNORECASE),
    re.compile(r"#\s*pylint:\s*disable", re.IGNORECASE),
    re.compile(r"#\s*nosec", re.IGNORECASE),
    re.compile(r"#\s*pragma:\s*no\s*cover", re.IGNORECASE),
    # JS/TS
    re.compile(r"//\s*eslint-disable", re.IGNORECASE),
    re.compile(r"/\*\s*eslint-disable", re.IGNORECASE),
    re.compile(r"//\s*@ts-ignore", re.IGNORECASE),
    re.compile(r"//\s*@ts-expect-error", re.IGNORECASE),
    # Java
    re.compile(r"@SuppressWarnings", re.IGNORECASE),
    # Generic
    re.compile(r"//\s*NOLINT", re.IGNORECASE),
    re.compile(r"//\s*noinspection", re.IGNORECASE),
]

# Intent-indicating comment keywords
_INTENT_KEYWORDS: list[str] = [
    "intentional",
    "by design",
    "deliberately",
    "on purpose",
    "expected behavior",
    "acceptable risk",
    "known issue",
]

# Common test file path segments
_TEST_PATH_SEGMENTS: list[str] = [
    "/tests/",
    "/test/",
    "/__tests__/",
    "/e2e/",
    "/spec/",
]

# Common test filename patterns (glob-style)
_TEST_FILE_PATTERNS: list[str] = [
    "test_*.py",
    "*_test.py",
    "*.spec.ts",
    "*.test.ts",
    "*.spec.js",
    "*.test.js",
    "*.spec.tsx",
    "*.test.tsx",
    "conftest.py",
]


# ── Result Model ───────────────────────────────────────────────────────


class IntentAnalysisResult(BaseModel):
    """Summary of intent analysis across all findings."""

    decisions: dict[str, str] = Field(default_factory=dict)  # finding_id -> intent_signal
    intentional_count: int = 0
    ambiguous_count: int = 0
    unintentional_count: int = 0
    deterministic_count: int = 0  # resolved without LLM


# ── Phase 1: Deterministic Utilities ───────────────────────────────────


def _read_surrounding_lines(
    repo_path: str,
    file_path: str,
    line_start: int | None,
    window: int = 10,
) -> list[str]:
    """Read lines around a finding location from disk.

    Args:
        repo_path: Root path of the repository.
        file_path: Relative path to the file (from finding.locations).
        line_start: 1-based line number of the finding.
        window: Number of lines to read above and below.

    Returns:
        List of source lines (empty on any read failure).
    """
    try:
        full_path = os.path.join(repo_path, file_path)
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except (OSError, IOError):
        return []

    if not all_lines:
        return []

    if line_start is None:
        # No specific line — return first 2*window lines as context
        return all_lines[: window * 2]

    # Convert to 0-based index
    center = max(0, line_start - 1)
    start = max(0, center - window)
    end = min(len(all_lines), center + window + 1)
    return all_lines[start:end]


def _has_suppression_annotation(lines: list[str]) -> bool:
    """Check if any line contains a known suppression annotation."""
    for line in lines:
        for pattern in _SUPPRESSION_PATTERNS:
            if pattern.search(line):
                return True
    return False


def _has_intent_comment(lines: list[str]) -> bool:
    """Check if any line contains an intent-indicating comment keyword."""
    for line in lines:
        line_lower = line.lower()
        for keyword in _INTENT_KEYWORDS:
            if keyword in line_lower:
                return True
    return False


def _is_test_file(
    file_path: str,
    conventions: ProjectConventions | None,
) -> bool:
    """Check if a file path matches test file patterns.

    Uses QAConventions test_paths and test_file_patterns when available,
    plus common fallback patterns.
    """
    # Normalize path separators
    normalized = file_path.replace("\\", "/")
    basename = os.path.basename(normalized)

    # Check QAConventions paths and patterns
    if conventions and conventions.test:
        qa: QAConventions = conventions.test
        for test_path in qa.test_paths:
            # test_path might be "tests/" or "tests" — normalize
            tp = test_path.rstrip("/")
            if f"/{tp}/" in f"/{normalized}" or normalized.startswith(f"{tp}/"):
                return True
        for pattern in qa.test_file_patterns:
            if fnmatch.fnmatch(basename, pattern):
                return True

    # Check common path segments
    for segment in _TEST_PATH_SEGMENTS:
        if segment in f"/{normalized}":
            return True

    # Check common filename patterns
    for pattern in _TEST_FILE_PATTERNS:
        if fnmatch.fnmatch(basename, pattern):
            return True

    return False


def _deterministic_intent_check(
    finding: AuditFinding,
    repo_path: str,
    conventions: ProjectConventions | None,
) -> str | None:
    """Attempt to determine intent deterministically (zero LLM cost).

    Returns:
        "intentional" if determined, None if LLM analysis is needed.
    """
    # Get file path from first location
    if not finding.locations:
        return None

    file_path = finding.locations[0].file_path
    line_start = finding.locations[0].line_start

    # Read surrounding source lines
    lines = _read_surrounding_lines(repo_path, file_path, line_start)

    # Check for suppression annotations
    if _has_suppression_annotation(lines):
        return "intentional"

    # Check for intent-indicating comments
    if _has_intent_comment(lines):
        return "intentional"

    # Test file check — non-critical findings in test files are intentional
    if _is_test_file(file_path, conventions):
        severity = finding.severity
        sev_value = severity.value if hasattr(severity, "value") else str(severity)
        if sev_value != "critical":
            return "intentional"

    return None


# ── Phase 2: LLM Batch Analysis ───────────────────────────────────────


def _build_finding_context_block(
    finding: AuditFinding,
    surrounding_code: str,
    is_test: bool,
) -> str:
    """Build a single finding's context block for the LLM prompt."""
    location_info = ""
    if finding.locations:
        loc = finding.locations[0]
        location_info = f"  File: {loc.file_path}"
        if loc.line_start:
            location_info += f" (line {loc.line_start})"

    severity = finding.severity
    sev_value = severity.value if hasattr(severity, "value") else str(severity)

    parts = [
        f"<finding id=\"{finding.id}\">",
        f"  Title: {finding.title}",
        f"  Description: {finding.description}",
        f"  Severity: {sev_value}",
        f"  Category: {finding.category.value if hasattr(finding.category, 'value') else finding.category}",
    ]
    if location_info:
        parts.append(location_info)
    if is_test:
        parts.append("  Context: This file is in a test directory")
    parts.append(f"  <surrounding_code>\n{surrounding_code}\n  </surrounding_code>")
    parts.append("</finding>")
    return "\n".join(parts)


async def _llm_intent_analysis(
    findings_with_context: list[tuple[AuditFinding, str, bool]],
    model: str,
    ai_provider: str,
) -> dict[str, str]:
    """Run LLM-based intent analysis on a batch of findings.

    Args:
        findings_with_context: List of (finding, surrounding_code, is_test) tuples.
        model: LLM model identifier.
        ai_provider: Provider name (e.g. "openrouter_direct").

    Returns:
        Dict mapping finding_id -> intent_signal.
    """
    from forge.vendor.agent_ai import AgentAI, AgentAIConfig

    all_decisions: dict[str, str] = {}

    # Process in batches
    for batch_start in range(0, len(findings_with_context), _LLM_BATCH_SIZE):
        batch = findings_with_context[batch_start : batch_start + _LLM_BATCH_SIZE]

        # Build context blocks for this batch
        context_blocks = []
        for finding, surrounding_code, is_test in batch:
            block = _build_finding_context_block(finding, surrounding_code, is_test)
            context_blocks.append(block)

        findings_context = "\n\n".join(context_blocks)
        task_prompt = intent_analyzer_task_prompt(findings_context=findings_context)

        try:
            agent = AgentAI(
                AgentAIConfig(
                    provider=ai_provider,
                    model=model,
                    max_turns=1,
                    allowed_tools=[],
                    agent_name="intent_analyzer",
                )
            )

            response = await agent.run(
                task_prompt,
                system_prompt=INTENT_ANALYZER_SYSTEM_PROMPT,
            )

            # Parse response
            raw_text = response.text
            if not raw_text:
                logger.warning(
                    "Intent analyzer returned empty response for batch starting at %d",
                    batch_start,
                )
                continue

            # Strip markdown fences and parse JSON
            cleaned = strip_json_fences(raw_text)
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.warning(
                    "Intent analyzer returned invalid JSON (first 200 chars): %s",
                    raw_text[:200],
                )
                continue

            # Extract decisions
            decisions = data.get("decisions", data)
            if isinstance(decisions, dict):
                for fid, signal in decisions.items():
                    if signal in ("intentional", "ambiguous", "unintentional"):
                        all_decisions[fid] = signal
                    else:
                        logger.warning(
                            "Intent analyzer returned invalid signal '%s' for %s, defaulting to ambiguous",
                            signal,
                            fid,
                        )
                        all_decisions[fid] = "ambiguous"

        except Exception:
            logger.exception(
                "Intent analyzer LLM call failed for batch starting at %d",
                batch_start,
            )
            continue

    return all_decisions


# ── Public API ─────────────────────────────────────────────────────────


async def analyze_intent(
    findings: list[AuditFinding],
    repo_path: str,
    conventions: ProjectConventions | None = None,
    model: str = "minimax/minimax-m2.5",
    ai_provider: str = "openrouter_direct",
) -> IntentAnalysisResult:
    """Analyze intent for a list of audit findings.

    Phase 1: Deterministic checks (suppression annotations, intent comments,
    test file detection) resolve findings at zero LLM cost.

    Phase 2: Remaining findings are batched and sent to the LLM for
    contextual intent analysis.

    Args:
        findings: List of AuditFinding objects to analyze.
        repo_path: Absolute path to the repository root.
        conventions: Optional project conventions (for test path detection).
        model: LLM model to use for Phase 2.
        ai_provider: AI provider for Phase 2.

    Returns:
        IntentAnalysisResult with per-finding decisions and counts.
    """
    try:
        result = IntentAnalysisResult()
        needs_llm: list[tuple[AuditFinding, str, bool]] = []

        # ── Phase 1: Deterministic checks ──────────────────────────
        for finding in findings:
            deterministic = _deterministic_intent_check(finding, repo_path, conventions)
            if deterministic is not None:
                finding.intent_signal = deterministic
                result.decisions[finding.id] = deterministic
                result.deterministic_count += 1
            else:
                # Gather context for LLM analysis
                file_path = ""
                line_start = None
                if finding.locations:
                    file_path = finding.locations[0].file_path
                    line_start = finding.locations[0].line_start

                surrounding = _read_surrounding_lines(
                    repo_path, file_path, line_start, window=10
                )
                surrounding_text = "".join(surrounding) if surrounding else "(no source context available)"
                is_test = _is_test_file(file_path, conventions) if file_path else False
                needs_llm.append((finding, surrounding_text, is_test))

        # ── Phase 2: LLM batch analysis ───────────────────────────
        if needs_llm:
            llm_decisions = await _llm_intent_analysis(
                needs_llm, model=model, ai_provider=ai_provider
            )

            # Apply LLM decisions back to findings
            for finding, _, _ in needs_llm:
                signal = llm_decisions.get(finding.id, "ambiguous")
                finding.intent_signal = signal
                result.decisions[finding.id] = signal

        # ── Tally counts ──────────────────────────────────────────
        for signal in result.decisions.values():
            if signal == "intentional":
                result.intentional_count += 1
            elif signal == "unintentional":
                result.unintentional_count += 1
            else:
                result.ambiguous_count += 1

        logger.info(
            "Intent analysis complete: %d intentional, %d ambiguous, %d unintentional "
            "(%d resolved deterministically)",
            result.intentional_count,
            result.ambiguous_count,
            result.unintentional_count,
            result.deterministic_count,
        )
        return result

    except Exception:
        logger.exception("Intent analysis failed — returning empty result")
        return IntentAnalysisResult()
