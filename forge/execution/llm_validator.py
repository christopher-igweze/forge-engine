"""LLM-based validation of deterministic findings.

Takes findings from the evaluation framework's deterministic checks
and uses an LLM to confirm or reject each one based on code context.
This reduces false positives from pattern matching.

Integration (to be wired in phases.py):
    1. After deterministic evaluation runs
    2. Pass failed checks to validate_findings()
    3. Apply validation results to filter false positives
    4. Include validated findings in the report

Example::

    from forge.execution.llm_validator import validate_findings, apply_validation

    results = await validate_findings(findings, file_reader, llm_caller)
    validated = apply_validation(findings, results)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of LLM validation for a single finding."""
    finding_id: str
    confirmed: bool  # True = real finding, False = false positive
    confidence: float  # 0.0-1.0
    reasoning: str  # Why the LLM confirmed or rejected
    suggested_severity: str | None = None  # LLM may suggest severity adjustment


VALIDATION_SYSTEM_PROMPT = """You are a senior security engineer validating automated code analysis findings.

For each finding, you receive:
- The check ID and description (what the deterministic tool found)
- The code snippet with surrounding context
- The file path and line numbers

Your job is to determine if this is a TRUE POSITIVE (real issue) or FALSE POSITIVE (noise).

<decision_criteria>
A finding is a TRUE POSITIVE when:
1. The flagged code pattern actually creates the described risk
2. The vulnerability is reachable from a user-facing entry point
3. No mitigation exists elsewhere in the code (e.g., middleware, wrapper function)
4. The code is production code (not test fixtures, not migration scripts)

A finding is a FALSE POSITIVE when:
1. The pattern match is in a comment, string literal, or documentation
2. The code is in a test file intentionally testing the pattern
3. A mitigation exists elsewhere (input validation, middleware, etc.)
4. The pattern is a framework convention (e.g., Django ORM is parameterized)
5. The variable name matches the pattern but the value is not sensitive
</decision_criteria>

<response_format>
Respond with JSON:
{
  "confirmed": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "One sentence explaining why",
  "suggested_severity": "critical|high|medium|low|null"
}
</response_format>

Be conservative: when in doubt, confirm the finding (true positive). \
But do NOT confirm findings in test files unless they test production code paths."""


VALIDATION_TASK_PROMPT_TEMPLATE = """Validate this finding:

Check: {check_id} — {check_description}
Severity: {severity}
File: {file_path} (lines {line_start}-{line_end})

Code context:
```
{code_context}
```

Is this a true positive or false positive?"""


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


async def validate_findings(
    findings: list[dict],
    file_reader,  # callable(file_path) -> str
    llm_caller,  # async callable(system_prompt, task_prompt) -> str
    max_findings: int = 30,
) -> list[ValidationResult]:
    """Validate deterministic findings using LLM reasoning.

    Args:
        findings: List of finding dicts from evaluation checks.
        file_reader: Function to read file contents given a path.
        llm_caller: Async function to call LLM with (system_prompt, task_prompt).
        max_findings: Max findings to validate (cost control).

    Returns:
        List of ValidationResult for each validated finding.
    """
    results: list[ValidationResult] = []

    # Prioritize high-severity findings for validation
    capped = findings[:max_findings]
    sorted_findings = sorted(
        capped,
        key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "low"), 4),
    )

    for finding in sorted_findings:
        try:
            result = await _validate_single(finding, file_reader, llm_caller)
            results.append(result)
        except Exception as e:
            fid = finding.get("id", finding.get("check_id", ""))
            logger.warning("Failed to validate finding %s: %s", fid, e)
            results.append(ValidationResult(
                finding_id=fid,
                confirmed=True,
                confidence=0.3,
                reasoning=f"Validation failed: {e}",
            ))

    confirmed_count = sum(1 for r in results if r.confirmed)
    rejected_count = sum(1 for r in results if not r.confirmed)
    logger.info(
        "LLM validation: %d confirmed, %d rejected out of %d findings",
        confirmed_count, rejected_count, len(results),
    )

    return results


async def _validate_single(
    finding: dict,
    file_reader,
    llm_caller,
) -> ValidationResult:
    """Validate a single finding against the LLM."""
    file_path = finding.get("file_path", "")
    line_start = finding.get("line_start", 1)
    line_end = finding.get("line_end", line_start)

    # Read surrounding code context (10 lines before and after)
    try:
        content = file_reader(file_path)
        lines = content.splitlines()
        ctx_start = max(0, line_start - 11)
        ctx_end = min(len(lines), line_end + 10)
        code_context = "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(lines[ctx_start:ctx_end], start=ctx_start)
        )
    except Exception:
        code_context = finding.get("snippet", "Code not available")

    task_prompt = VALIDATION_TASK_PROMPT_TEMPLATE.format(
        check_id=finding.get("check_id", "UNKNOWN"),
        check_description=finding.get("description", ""),
        severity=finding.get("severity", "medium"),
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        code_context=code_context,
    )

    response = await llm_caller(VALIDATION_SYSTEM_PROMPT, task_prompt)

    fid = finding.get("id", finding.get("check_id", ""))

    try:
        parsed = json.loads(response)
        return ValidationResult(
            finding_id=fid,
            confirmed=parsed.get("confirmed", True),
            confidence=parsed.get("confidence", 0.5),
            reasoning=parsed.get("reasoning", ""),
            suggested_severity=parsed.get("suggested_severity"),
        )
    except (json.JSONDecodeError, KeyError):
        return ValidationResult(
            finding_id=fid,
            confirmed=True,
            confidence=0.5,
            reasoning="LLM response could not be parsed — defaulting to confirmed",
        )


def apply_validation(
    findings: list[dict],
    results: list[ValidationResult],
) -> list[dict]:
    """Apply validation results to findings — remove false positives, adjust severity.

    Findings rejected by the LLM are excluded from the returned list.
    Findings with a suggested severity adjustment get their severity updated
    (the original is preserved in ``original_severity``).

    Findings that were not validated (no matching ValidationResult) are kept as-is.
    """
    result_map = {r.finding_id: r for r in results}
    validated: list[dict] = []

    for finding in findings:
        fid = finding.get("id", finding.get("check_id", ""))
        validation = result_map.get(fid)

        if validation and not validation.confirmed:
            # LLM says false positive — skip it
            finding["llm_rejected"] = True
            finding["llm_reasoning"] = validation.reasoning
            continue

        if validation and validation.suggested_severity:
            finding["original_severity"] = finding.get("severity")
            finding["severity"] = validation.suggested_severity
            finding["llm_reasoning"] = validation.reasoning

        validated.append(finding)

    return validated
