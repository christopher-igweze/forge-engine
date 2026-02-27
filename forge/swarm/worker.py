"""Swarm workers for Layer 1 of Hive Discovery.

Each worker analyzes a single segment through a specific lens
(security, quality, or architecture). Workers use minimax-m2.5
and write findings to the shared CodeGraph.

Two-wave execution:
  Wave 1: Workers analyze their primary segment in isolation
  Wave 2: Workers re-analyze with access to neighbor findings (MoA pattern)

Prompt structure follows research-backed patterns:
  - Sequential analysis steps (M2.5 performs better with step-by-step vs parallel)
  - Evidence requirements (Semgrep: eliminates theoretical findings)
  - Hard exclusion lists (Anthropic: filters known false-positive magnets)
  - Anti-sycophancy calibration (Stanford: 58% sycophancy rate without instruction)
  - <think> tag handling (M2.5 may emit reasoning tags)
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from forge.graph.models import CodeGraph, SegmentContext

logger = logging.getLogger(__name__)

# M2.5 degrades beyond ~90k tokens (~300k chars). Guard against this.
MAX_M2_CONTEXT_CHARS = 300_000


def _read_file_safe(path: str, max_chars: int = 12_000) -> str:
    """Read a file safely with truncation."""
    try:
        content = Path(path).read_text(errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content
    except OSError:
        return ""


class SwarmWorker(ABC):
    """Base class for swarm analysis workers.

    Each worker:
    1. Receives a SegmentContext (files, graph neighbors, edges)
    2. Calls a cheap LLM (minimax-m2.5) with a focused prompt
    3. Writes findings back to the CodeGraph
    """

    worker_type: str = "base"

    def __init__(
        self,
        segment_id: str,
        model: str = "minimax/minimax-m2.5",
        ai_provider: str = "openrouter_direct",
    ):
        self.segment_id = segment_id
        self.model = model
        self.ai_provider = ai_provider

    @abstractmethod
    def build_system_prompt(self) -> str:
        """Return the system prompt for this worker type."""

    @abstractmethod
    def build_task_prompt(
        self,
        context: SegmentContext,
        wave: int,
        repo_path: str,
    ) -> str:
        """Build the task prompt with segment context."""

    async def analyze(
        self,
        graph: CodeGraph,
        wave: int,
        repo_path: str,
    ) -> list[dict]:
        """Run analysis on the assigned segment.

        Args:
            graph: The shared CodeGraph
            wave: 1 or 2 (Wave 2 includes neighbor findings)
            repo_path: Path to the repository root

        Returns:
            List of finding dicts written to the graph
        """
        from forge.vendor.agent_ai import AgentAI, AgentAIConfig

        # Build context
        context = graph.query_segment(self.segment_id)

        # Load file contents
        root = Path(repo_path)
        for file_path in context.segment.files:
            abs_path = str(root / file_path)
            content = _read_file_safe(abs_path)
            if content:
                context.file_contents[file_path] = content

        # In Wave 2, add neighbor findings
        if wave == 2:
            context.neighbor_findings = graph.query_neighbors(self.segment_id)

        # Build prompts
        system_prompt = self.build_system_prompt()
        task_prompt = self.build_task_prompt(context, wave, repo_path)

        # M2.5 context budget guard — degrades beyond ~90k tokens
        total_chars = len(system_prompt) + len(task_prompt)
        if "minimax" in self.model and total_chars > MAX_M2_CONTEXT_CHARS:
            logger.warning(
                "M2.5 context size %d exceeds safe threshold %d — truncating file contents",
                total_chars, MAX_M2_CONTEXT_CHARS,
            )
            budget = MAX_M2_CONTEXT_CHARS - len(system_prompt) - 5000
            context.file_contents = _truncate_contents(context.file_contents, budget)
            task_prompt = self.build_task_prompt(context, wave, repo_path)

        # Call LLM
        ai = AgentAI(AgentAIConfig(
            provider=self.ai_provider,
            model=self.model,
            cwd=repo_path,
            max_turns=1,
            allowed_tools=[],
            env={"OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "")},
            agent_name=f"hive_worker/{self.worker_type}",
        ))

        response = await ai.run(task_prompt, system_prompt=system_prompt)

        # Parse findings from response
        findings = self._parse_findings(response)

        # Write findings to graph
        for finding in findings:
            finding["worker_type"] = self.worker_type
            finding["segment_id"] = self.segment_id
            finding["wave"] = wave
            graph.add_finding(finding, self.segment_id)

        logger.info(
            "Worker %s/%s wave %d: %d findings",
            self.worker_type, self.segment_id, wave, len(findings),
        )
        return findings

    def _parse_findings(self, response) -> list[dict]:
        """Parse findings from LLM response."""
        data = {}
        if response.parsed:
            data = response.parsed.model_dump() if hasattr(response.parsed, "model_dump") else {}
        elif response.text:
            data = _parse_json_response(response.text)

        findings = data.get("findings", [])
        if not isinstance(findings, list):
            findings = []

        return findings


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response — hardened for M2.5 quirks.

    Handles:
    - <think>...</think> reasoning tags (M2.5 may emit these)
    - Markdown code fences (```json ... ```)
    - Natural language preamble before the JSON object
    """
    cleaned = text.strip()

    # Strip <think>...</think> reasoning tags (M2.5 may emit these)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

    # Strip markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end]).strip()

    # M2.5 sometimes prepends natural language — find first { and last }
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        cleaned = cleaned[first_brace:last_brace + 1]

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def _truncate_contents(file_contents: dict[str, str], max_chars: int) -> dict[str, str]:
    """Truncate file contents to fit within a character budget."""
    result = {}
    chars_used = 0
    for path, content in sorted(file_contents.items()):
        if chars_used + len(content) > max_chars:
            remaining = max_chars - chars_used
            if remaining > 200:
                result[path] = content[:remaining] + "\n... (truncated for context budget)"
            break
        result[path] = content
        chars_used += len(content)
    return result


def _format_file_contents(file_contents: dict[str, str], max_total: int = 60_000) -> str:
    """Format file contents for prompt inclusion."""
    parts = []
    chars_used = 0
    for path, content in sorted(file_contents.items()):
        if chars_used + len(content) > max_total:
            break
        parts.append(f"### {path}\n```\n{content}\n```\n")
        chars_used += len(content)
    return "\n".join(parts) if parts else "(no files)"


def _format_graph_context(context: SegmentContext) -> str:
    """Format graph edges and node info for prompt context."""
    lines = []
    lines.append(f"Segment: {context.segment.id} ({context.segment.label})")
    lines.append(f"Files: {len(context.segment.files)}")
    lines.append(f"LOC: {context.segment.loc}")

    if context.segment.entry_points:
        lines.append(f"Entry Points: {', '.join(context.segment.entry_points[:10])}")

    if context.segment.external_deps:
        lines.append(f"External Dependencies: {', '.join(context.segment.external_deps[:15])}")

    if context.segment.internal_deps:
        lines.append(f"Depends on segments: {', '.join(context.segment.internal_deps)}")

    # Summarize edges
    edge_summary = {}
    for e in context.edges:
        edge_summary[e.kind.value] = edge_summary.get(e.kind.value, 0) + 1
    if edge_summary:
        lines.append(f"Edges: {', '.join(f'{k}={v}' for k, v in edge_summary.items())}")

    return "\n".join(lines)


def _format_neighbor_findings(findings: list[dict]) -> str:
    """Format neighbor findings for Wave 2 context."""
    if not findings:
        return "(no findings from neighboring segments)"

    parts = []
    for f in findings[:20]:  # Cap at 20 neighbor findings
        parts.append(
            f"- [{f.get('category', '?')}] {f.get('title', '?')}: "
            f"{f.get('description', '')[:200]}"
        )
    return "\n".join(parts)


# ── Concrete Workers ─────────────────────────────────────────────────


class SecurityWorker(SwarmWorker):
    """Security-focused analysis worker.

    Uses XML-structured prompts with sequential analysis steps optimized
    for M2.5's step-by-step reasoning strengths.
    """

    worker_type = "security"

    def __init__(
        self,
        segment_id: str,
        model: str = "minimax/minimax-m2.5",
        ai_provider: str = "openrouter_direct",
        pattern_context: str = "",
        project_context: str = "",
    ):
        super().__init__(segment_id, model, ai_provider)
        self._pattern_context = pattern_context
        self._project_context = project_context

    def build_system_prompt(self) -> str:
        prompt = """\
<role>
You are a security auditor analyzing a single code segment for exploitable
vulnerabilities. You are part of a swarm — other workers analyze neighboring
segments. Report only findings you can prove with concrete evidence from
the code provided.
</role>

<methodology>
Analyze this code segment in order:
Step 1: Scan for authentication/authorization flaws in this segment — missing
  auth checks, IDOR, privilege escalation paths
Step 2: Check input validation and sanitization for all entry points — trace
  each user input from source to sink
Step 3: Look for injection risks (SQL, XSS, command injection) — verify the
  input actually reaches an unsafe sink without sanitization
Step 4: Check for exposed secrets or credentials in source code
Step 5: Review cryptographic patterns — weak hashing, hardcoded salts,
  predictable random generation
Step 6: Self-verify — argue against each finding before including it. Could
  the framework, ORM, or template engine already prevent this?

For each step, add confirmed findings to the findings array before moving to
the next step. An empty findings array is acceptable if no genuine issues exist.
</methodology>

<evidence_requirements>
For EVERY finding you report, you MUST provide:
- The exact data flow in the "data_flow" field: source (untrusted input) ->
  transformations -> sink (dangerous operation)
- A concrete attack payload or exploit scenario in the description
- Why existing mitigations (if any) are insufficient
- A specific, minimal code fix (not an architectural rewrite)

If you cannot trace a concrete data flow from untrusted input to a dangerous
sink, do NOT report it. We want zero theoretical findings.
</evidence_requirements>

<hard_exclusions>
DO NOT report findings in these categories (known false-positive magnets):
- Denial of Service / resource exhaustion (unless trivially exploitable)
- Missing rate limiting as a standalone finding (infrastructure concern)
- Secrets stored on disk if loaded from environment variables
- Input validation on non-security-critical fields without proven impact
- Regex injection (not exploitable in most contexts)
- Generic "best practice" suggestions that are not actual vulnerabilities
- Findings in test files, documentation, or generated code
- Architecture pattern suggestions (repository layer, service layer, etc.)
  unless directly causing a security vulnerability
- Missing security headers alone (informational, not exploitable)
</hard_exclusions>

<severity_calibration>
Rate severity based on TECHNICAL IMPACT, not on how the developer might feel.
Do not soften findings with "might," "could potentially," or "worth considering."
Either it IS a vulnerability or it is NOT.

- critical: Remotely exploitable with high impact (RCE, auth bypass, data
  exfiltration via SQL injection). Confidence must be >= 0.9.
- high: Exploitable with moderate effort (IDOR, stored XSS, privilege
  escalation). Confidence must be >= 0.8.
- medium: Defense-in-depth gap with bounded impact (reflected XSS, info
  disclosure via error messages). Confidence must be >= 0.7.
- low: Best-practice gap with minimal direct impact. Confidence must be >= 0.7.
</severity_calibration>

<output_format>
Your output will be programmatically parsed by a downstream pipeline.
Return ONLY a valid JSON object. The first character must be { and the last
must be }. Do NOT wrap in ```json blocks. Do NOT write text before or after
the JSON. If the JSON is malformed, findings are lost.

{
  "findings": [
    {
      "id": "SEC-001",
      "title": "IDOR on scan status endpoint",
      "description": "GET /api/status/{scan_id} returns scan details without verifying the authenticated user owns the scan.",
      "category": "security",
      "severity": "high",
      "locations": [{"file_path": "api/routes/status.py", "line_start": 45, "line_end": 52, "snippet": "scan = await db.get_scan(scan_id)"}],
      "suggested_fix": "Add user_id filter: scan = await db.get_scan(scan_id, user_id=current_user.id)",
      "confidence": 0.92,
      "cwe_id": "CWE-639",
      "owasp_ref": "A01:2021",
      "data_flow": "Request param scan_id -> db.get_scan(scan_id) -> response body (no user_id check)",
      "actionability": "must_fix",
      "pattern_id": "",
      "pattern_slug": ""
    }
  ],
  "summary": "Brief summary of security posture"
}

The "actionability" field classifies each finding:
- "must_fix": Exploitable now, fix before shipping
- "should_fix": Real issue, prioritize this sprint
- "consider": Valid observation, may not be urgent at current project stage
- "informational": Noted for awareness, not actionable now
</output_format>"""

        if self._project_context:
            prompt += f"\n\n{self._project_context}"
        if self._pattern_context:
            prompt += f"\n\n{self._pattern_context}"
        return prompt

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Security Analysis Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments (cross-reference these)\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nCross-reference these neighbor findings with this segment's code. "
                "Look for vulnerability chains that span segments."
            )
        parts.append(
            "\nAnalyze the code above for security vulnerabilities. "
            "Follow the step-by-step methodology in your system prompt. "
            "For each finding, include the data_flow trace and actionability classification. "
            "Report only findings with confidence >= 0.7 and concrete evidence."
        )
        return "\n".join(parts)


class QualityWorker(SwarmWorker):
    """Code quality analysis worker.

    Hard exclusions filter cosmetic noise and architecture opinions
    that don't represent real quality issues.
    """

    worker_type = "quality"

    def __init__(
        self,
        segment_id: str,
        model: str = "minimax/minimax-m2.5",
        ai_provider: str = "openrouter_direct",
        project_context: str = "",
    ):
        super().__init__(segment_id, model, ai_provider)
        self._project_context = project_context

    def build_system_prompt(self) -> str:
        prompt = """\
<role>
You are a code quality auditor analyzing a single code segment for quality
issues that impact reliability, maintainability, and correctness. Report only
issues with concrete evidence and measurable impact.
</role>

<methodology>
Analyze this code segment in order:
Step 1: Check error handling — uncaught exceptions, missing error boundaries,
  swallowed errors that hide failures
Step 2: Identify code duplication — repeated logic across 3+ locations that
  risks diverging. Under 5 lines is acceptable.
Step 3: Flag complex functions — high cyclomatic complexity (>10 branches),
  deeply nested logic, functions over 100 lines
Step 4: Check for performance issues — N+1 queries, missing pagination on
  unbounded collections, synchronous blocking in async code
Step 5: Review data integrity — missing validation at system boundaries,
  type coercion risks, unchecked null/undefined access
Step 6: Self-verify — for each finding, confirm it causes a real problem
  (bug, data loss, crash), not just a style preference
</methodology>

<evidence_requirements>
For each finding, provide:
- The specific code pattern that causes the issue
- The concrete consequence (crash, data loss, performance degradation)
- A minimal, targeted fix

If you cannot explain a concrete negative consequence, do NOT report it.
</evidence_requirements>

<hard_exclusions>
DO NOT report:
- "Missing repository/service abstraction" for codebases under 5k LOC
- Enum vs Literal type choices (both are valid Python)
- Inconsistent naming conventions that don't cause bugs
- Missing __init__.py exports (cosmetic, not a quality issue)
- "Magic numbers" that are module-level constants with clear meaning
- Code duplication under 5 lines (not worth abstracting)
- Missing type annotations on internal functions
- Style preferences (single vs double quotes, trailing commas, etc.)
- "Consider using X library" suggestions without a concrete bug
</hard_exclusions>

<severity_calibration>
- critical: Will cause data loss or crashes in production
- high: Causes bugs under common conditions or severe performance degradation
- medium: Causes bugs under edge conditions or moderate performance issues
- low: Maintainability concern with no immediate impact
</severity_calibration>

<output_format>
Your output will be programmatically parsed by a downstream pipeline.
Return ONLY a valid JSON object. The first character must be { and the last
must be }. Do NOT wrap in ```json blocks. Do NOT write text before or after
the JSON. If the JSON is malformed, findings are lost.

{
  "findings": [
    {
      "id": "QUAL-001",
      "title": "Short descriptive title",
      "description": "Detailed explanation of the quality issue and its consequence",
      "category": "quality",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "path/to/file.py", "line_start": 10, "line_end": 15, "snippet": "problematic code"}],
      "suggested_fix": "How to improve this",
      "confidence": 0.8,
      "actionability": "must_fix|should_fix|consider|informational"
    }
  ],
  "summary": "Brief summary of code quality"
}
</output_format>"""

        if self._project_context:
            prompt += f"\n\n{self._project_context}"
        return prompt

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Code Quality Analysis Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nConsider these neighbor findings. "
                "Look for quality patterns that repeat across segments."
            )
        parts.append(
            "\nAnalyze the code above for quality issues. "
            "Follow the step-by-step methodology in your system prompt. "
            "Report only findings with concrete consequences and confidence >= 0.7."
        )
        return "\n".join(parts)


class ArchitectureWorker(SwarmWorker):
    """Architecture analysis worker.

    Scale-awareness instruction prevents recommending enterprise patterns
    for small codebases. project_context provides additional calibration.
    """

    worker_type = "architecture"

    def __init__(
        self,
        segment_id: str,
        model: str = "minimax/minimax-m2.5",
        ai_provider: str = "openrouter_direct",
        project_context: str = "",
    ):
        super().__init__(segment_id, model, ai_provider)
        self._project_context = project_context

    def build_system_prompt(self) -> str:
        prompt = """\
<role>
You are an architecture reviewer analyzing a single code segment for structural
issues that cause real problems — bugs, scaling failures, or maintenance burden.
You are part of a swarm analyzing the full codebase in parallel.
</role>

<methodology>
Analyze this code segment in order:
Step 1: Check coupling — does this segment have tight coupling to other modules?
  Look for concrete circular dependencies, God objects, or modules that import
  from too many unrelated segments.
Step 2: Check layering — does presentation code directly access the database?
  Are there boundary violations with concrete consequences?
Step 3: Review configuration management — hardcoded values that should be
  configurable, environment-specific settings mixed with business logic
Step 4: Check dependency management — pinned vs unpinned versions, known
  vulnerable dependency versions (CVEs, not just "outdated")
Step 5: Assess scalability — identify concrete bottlenecks (unbounded queries,
  missing pagination, synchronous blocking in async code)
Step 6: Self-verify — for each finding, confirm it causes a real problem at
  the current project scale, not just at hypothetical enterprise scale
</methodology>

<scale_awareness>
Before recommending an architectural pattern, consider the codebase size:
- Under 3k LOC: Almost all "missing abstraction" findings are noise.
  Only report if the lack of abstraction directly causes bugs or data integrity issues.
- 3k-15k LOC: Recommend patterns only if concrete duplication or coupling
  evidence exists across 3+ files.
- Over 15k LOC: Standard architectural analysis applies.

Do NOT recommend:
- Repository/service/controller layers for codebases under 5k LOC
- Dependency injection frameworks for fewer than 10 services
- Event-driven architecture for simple CRUD apps
- Microservice decomposition for monoliths under 20k LOC
</scale_awareness>

<evidence_requirements>
For each finding, provide:
- The specific structural issue with file paths and code evidence
- The concrete consequence (bugs, scaling failure, maintenance burden)
- A proportionate fix — not an architectural rewrite

If you cannot point to a concrete problem caused by the current architecture,
do NOT report it. "Could be better" is not a finding.
</evidence_requirements>

<output_format>
Your output will be programmatically parsed by a downstream pipeline.
Return ONLY a valid JSON object. The first character must be { and the last
must be }. Do NOT wrap in ```json blocks. Do NOT write text before or after
the JSON. If the JSON is malformed, findings are lost.

{
  "findings": [
    {
      "id": "ARCH-001",
      "title": "Short descriptive title",
      "description": "Detailed explanation of the architecture issue and its consequence",
      "category": "architecture",
      "severity": "critical|high|medium|low|info",
      "locations": [{"file_path": "path/to/file.py", "line_start": 10, "line_end": 15, "snippet": "problematic code"}],
      "suggested_fix": "How to improve this",
      "confidence": 0.8,
      "actionability": "must_fix|should_fix|consider|informational"
    }
  ],
  "summary": "Brief summary of architecture quality"
}
</output_format>"""

        if self._project_context:
            prompt += f"\n\n{self._project_context}"
        return prompt

    def build_task_prompt(self, context: SegmentContext, wave: int, repo_path: str) -> str:
        parts = [
            "# Architecture Review Task",
            f"\n## Segment Context\n{_format_graph_context(context)}",
            f"\n## Source Files\n{_format_file_contents(context.file_contents)}",
        ]
        if wave == 2 and context.neighbor_findings:
            parts.append(
                f"\n## Findings from Neighboring Segments (cross-cutting concerns)\n"
                f"{_format_neighbor_findings(context.neighbor_findings)}"
            )
            parts.append(
                "\nExamine how this segment's architecture relates to neighboring segment issues. "
                "Look for cross-cutting architectural concerns."
            )
        parts.append(
            "\nAnalyze the code above for architectural issues. "
            "Follow the step-by-step methodology in your system prompt. "
            "Consider the codebase scale before recommending patterns. "
            "Report only findings with concrete consequences and confidence >= 0.7."
        )
        return "\n".join(parts)
