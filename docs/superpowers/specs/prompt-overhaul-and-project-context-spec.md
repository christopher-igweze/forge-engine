# Prompt Overhaul + Project Context Intake + Actionable Reports

**Status:** Spec Complete — Ready for Implementation
**Priority:** P0 — Directly impacts report quality (core product value)
**Origin:** Self-scan analysis showed 50% signal-to-noise ratio; Semgrep research shows hybrid approaches achieve 96% accuracy
**Repos:** `forge-engine` (primary), `vibe2prod` (Tier 1 reporter + API)

---

## Problem Statement

FORGE discovery ran against the vibe2prod backend itself and produced 25 findings.
External review classified only ~12 as actionable — a **~50% signal-to-noise ratio**.

Root causes identified:

1. **Generic prompts** — Security auditor and swarm workers use broad "find
   everything" instructions. Semgrep's research shows this produces 86% false
   positives. Targeted, CWE-specific prompts with evidence requirements cut
   false positives dramatically.

2. **No project context** — The scanner doesn't know if it's auditing a weekend
   MVP or a pre-Series A product. F-012 ("add repository abstraction") is
   correct advice for a 50k LOC enterprise app and pure noise for a 2k LOC
   solo-dev MVP. Without project context, the LLM defaults to "enterprise
   patterns good."

3. **Flat severity presentation** — Findings are listed by severity
   (critical/high/medium/low) without actionability context. Users can't
   distinguish "must fix before shipping" from "nice to have at your scale."

4. **Model-specific prompt gaps** — Claude 4.6 takes instructions literally and
   benefits from XML tags + anti-sycophancy instructions. Minimax M2.5 was
   trained at temperature 1.0, degrades after 90k tokens, and needs sequential
   step-by-step analysis. Neither model gets optimized prompts.

---

## Solution Overview

Three interconnected workstreams, each independently shippable:

```
                 ┌─────────────────────┐
                 │  Project Context     │
                 │  Intake (API + DB)   │
                 └────────┬────────────┘
                          │ project_context string
                          ▼
┌──────────────┐   ┌─────────────────────┐   ┌──────────────────────┐
│ Pattern Lib  │──▶│  Prompt Overhaul     │──▶│  Actionable Report   │
│ (DONE)       │   │  (Auditor + Workers) │   │  (Tiers + Framing)   │
└──────────────┘   └─────────────────────┘   └──────────────────────┘
                          │
                          ▼
                   Better findings with:
                   - Evidence-based reasoning
                   - Confidence calibration
                   - Actionability classification
```

**Cost impact:** Zero additional LLM calls. All improvements are prompt
engineering within existing API calls.

---

## Workstream A: Prompt Overhaul (forge-engine)

### A1. Security Auditor Prompts (`forge/prompts/security_auditor.py`)

**Current state:** ~120 lines, generic focus areas, no evidence requirements, no
false-positive filtering, no model-specific optimizations.

**Target state:** Structured XML-tagged prompts with Think & Verify reasoning,
evidence requirements, hard exclusion list, few-shot examples, and
anti-sycophancy instructions.

#### A1a. System Prompt Structure (all 3 passes)

Replace the current `_BASE_SYSTEM` with XML-structured prompt:

```xml
<role>
You are a senior application security engineer performing a production readiness
audit. You specialize in finding exploitable vulnerabilities in vibe-coded
applications (Lovable, Bolt, Cursor, Replit Agent). You report only findings
you can prove with concrete evidence.
</role>

<methodology>
For each potential vulnerability, follow this analysis chain:
1. IDENTIFY entry points where untrusted data enters (request params, headers,
   body, query strings, file uploads, webhook payloads)
2. TRACE the data flow from source through transformations to sink
3. CHECK for sanitization, validation, or framework protections at each step
4. VERIFY the finding is exploitable — construct a concrete attack scenario
5. ASSESS severity based on actual impact, not theoretical risk
6. SELF-CHECK — argue against your own finding. Could you be wrong?
</methodology>

<evidence_requirements>
For EVERY finding you report, you MUST provide:
- The exact data flow: source (untrusted input) -> transformations -> sink
- A concrete attack payload or exploit scenario
- Why existing mitigations (if any) are insufficient
- A specific, minimal code fix (not an architectural rewrite)

If you cannot trace a concrete data flow from untrusted input to a dangerous
sink, do NOT report it. We want zero theoretical findings.
</evidence_requirements>

<hard_exclusions>
DO NOT report findings in these categories (known false-positive magnets):
- Denial of Service / resource exhaustion (unless trivially exploitable)
- Missing rate limiting (infrastructure concern, not a code vulnerability)
- Secrets stored on disk if loaded from environment variables
- Input validation on non-security-critical fields without proven impact
- Regex injection (not exploitable in most contexts)
- Generic "best practice" suggestions that are not actual vulnerabilities
- Findings in test files, documentation, or generated code
- Architecture pattern suggestions (repository layer, service layer, etc.)
  unless directly causing a security vulnerability
</hard_exclusions>

<severity_calibration>
Rate severity based on TECHNICAL IMPACT, not on how the developer might feel.
Do not soften findings with "might," "could potentially," or "worth considering."
Either it IS a vulnerability or it is NOT.

- critical: Remotely exploitable with high impact (RCE, auth bypass, data
  exfiltration). Confidence must be >= 0.9.
- high: Exploitable with moderate effort (IDOR, stored XSS, privilege
  escalation). Confidence must be >= 0.8.
- medium: Defense-in-depth gap with bounded impact (reflected XSS, info
  disclosure). Confidence must be >= 0.7.
- low: Best-practice gap with minimal direct impact. Confidence must be >= 0.7.
</severity_calibration>

<confidence_scoring>
Your confidence score (0.0-1.0) must reflect:
- 0.9-1.0: Deterministic proof (e.g., hardcoded secret, missing auth check)
- 0.7-0.89: Strong evidence with minor uncertainty (e.g., injection with
  unclear sanitization)
- 0.5-0.69: Possible vulnerability, needs manual verification (suppress these)
- Below 0.5: Do not report
</confidence_scoring>

{project_context}

{pattern_context}
```

#### A1b. Per-Pass Focus Updates

Each pass keeps its specific focus but gains structured reasoning steps:

**Pass 1 (Auth Flow):**
```
ANALYSIS STEPS:
Step 1: Map all route handlers and their middleware chains
Step 2: For each route, trace the auth check — is it present? Can it be bypassed?
Step 3: Check session/token handling — creation, validation, expiration, storage
Step 4: Verify RBAC — can User A access User B's resources?
Step 5: Check OAuth flows — state parameter, redirect URI validation, PKCE
Step 6: Self-verify each finding against the framework's built-in protections
```

**Pass 2 (Data Handling):**
```
ANALYSIS STEPS:
Step 1: Identify all user-controlled inputs (params, headers, body, files)
Step 2: For each input, trace it to every sink (DB queries, HTML output, file ops)
Step 3: At each source-to-sink path, verify sanitization exists AND is adequate
Step 4: Check for secrets in source code (API keys, passwords, tokens)
Step 5: Verify encryption at rest and in transit for sensitive data
Step 6: Self-verify — could framework ORMs or template engines prevent this?
```

**Pass 3 (Infrastructure):**
```
ANALYSIS STEPS:
Step 1: Check CORS configuration — are origins restricted? Credentials safe?
Step 2: Review error handling — are stack traces or internal details exposed?
Step 3: Check dependency manifests for known CVEs (npm audit, pip audit)
Step 4: Verify security headers (HSTS, CSP, X-Frame-Options)
Step 5: Check environment config — debug mode, default credentials, verbose logging
Step 6: Self-verify — which of these are handled by the deployment platform?
```

#### A1c. Few-Shot Examples

Add to each pass's system prompt — 1 true positive, 1 false positive dismissal:

```xml
<examples>
<true_positive>
{
  "title": "IDOR on scan status endpoint",
  "description": "GET /api/status/{scan_id} returns scan details without
    verifying the authenticated user owns the scan. Any authenticated user
    can enumerate scan_ids and view other users' scan results.",
  "category": "security",
  "severity": "high",
  "locations": [{"file_path": "api/routes/status.py", "line_start": 45,
    "line_end": 52, "snippet": "scan = await db.get_scan(scan_id)"}],
  "data_flow": "Request param scan_id -> db.get_scan() -> response (no user_id check)",
  "exploit_scenario": "Attacker increments scan_id in GET /api/status/123 to
    view scans belonging to other users",
  "suggested_fix": "Add user_id filter: scan = await db.get_scan(scan_id, user_id=current_user.id)",
  "confidence": 0.92,
  "cwe_id": "CWE-639",
  "pattern_id": "",
  "pattern_slug": ""
}
</true_positive>

<false_positive_dismissal>
Finding considered: "Direct database access without repository abstraction"
Reason dismissed: This is an architectural opinion, not a security vulnerability.
The codebase is a small backend (~2k LOC) where a repository layer would add
indirection without security benefit. The DB client is already centralized in
a single module. No untrusted data reaches the database through this pattern.
</false_positive_dismissal>
</examples>
```

#### A1d. Output Schema Update

Add `data_flow` and `actionability` fields to the JSON schema:

```python
class AuditFinding:
    # ... existing fields ...
    data_flow: str = ""        # source -> transformation -> sink trace
    actionability: str = ""    # "must_fix" | "should_fix" | "consider" | "informational"
```

Add to `forge/schemas.py`:
```python
# After pattern_slug field (line ~211):
data_flow: str = ""
actionability: str = ""  # must_fix | should_fix | consider | informational
```

#### A1e. Task Prompt Threading

In `security_audit_task_prompt()`, add project_context parameter and inject
before pattern_context:

```python
def security_audit_task_prompt(
    *,
    audit_pass: AuditPassType,
    codebase_map_json: str,
    relevant_file_contents: str,
    repo_url: str = "",
    pattern_context: str = "",
    project_context: str = "",  # NEW
) -> str:
```

---

### A2. Swarm Worker Prompts (`forge/swarm/worker.py`)

#### A2a. SecurityWorker System Prompt Overhaul

Replace the current ~30-line generic prompt with structured XML format matching
A1a, adapted for segment-level analysis. Key differences from the auditor:

- Segment-scoped (not whole-repo): "You are analyzing a single code segment"
- Cross-reference instruction: "In Wave 2, validate findings against neighbor
  segment context"
- Same evidence requirements, hard exclusions, severity calibration
- Same few-shot examples

Add sequential analysis steps (M2.5 performs better with sequential vs parallel):

```
Analyze this code segment in order:
Step 1: Scan for authentication/authorization flaws in this segment
Step 2: Check input validation and sanitization for all entry points
Step 3: Look for injection risks (SQL, XSS, command injection)
Step 4: Check for exposed secrets or credentials
Step 5: Review cryptographic patterns
Step 6: Self-verify — argue against each finding before including it

For each step, add confirmed findings to the findings array before moving to
the next step. An empty findings array is acceptable if no genuine issues exist.
```

#### A2b. QualityWorker Prompt Improvements

Add evidence requirements and hard exclusions:

```xml
<hard_exclusions>
DO NOT report:
- "Missing repository/service abstraction" for codebases under 5k LOC
- Enum vs Literal type choices (both are valid Python)
- Inconsistent naming conventions that don't cause bugs
- Missing __init__.py exports (cosmetic, not a quality issue)
- "Magic numbers" that are module-level constants with clear meaning
- Code duplication under 5 lines (not worth abstracting)
</hard_exclusions>
```

#### A2c. ArchitectureWorker Prompt Improvements

Add scale-awareness instruction:

```xml
<scale_awareness>
Before recommending an architectural pattern, consider the codebase size:
- Under 3k LOC: Almost all "missing abstraction" findings are noise.
  Only report if the lack of abstraction directly causes bugs or security issues.
- 3k-15k LOC: Recommend patterns only if concrete duplication or coupling
  evidence exists across 3+ files.
- Over 15k LOC: Standard architectural analysis applies.

{project_context}
</scale_awareness>
```

---

### A3. M2.5-Specific Optimizations

#### A3a. Temperature and Parameters

In `forge/swarm/worker.py` `analyze()` method, when constructing the AgentAI
call, set M2.5-optimized parameters:

```python
# M2.5 was trained at temperature 1.0 — lower temps degrade quality
ai_config = AgentAIConfig(
    provider=self.ai_provider,
    model=self.model,
    temperature=1.0 if "minimax" in self.model else 0.7,
    top_p=0.95 if "minimax" in self.model else 1.0,
    # ...
)
```

#### A3b. JSON Output Instructions

Add M2.5-specific output instructions to all worker system prompts:

```
Your output will be programmatically parsed by a downstream pipeline.
Return ONLY a valid JSON object. The first character must be { and the last
must be }. Do NOT wrap in ```json blocks. Do NOT write text before or after
the JSON. If the JSON is malformed, findings are lost.
```

#### A3c. JSON Parser Hardening

In `forge/swarm/worker.py` `_parse_json_response()`:

```python
def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response — hardened for M2.5 quirks."""
    import re as _re

    cleaned = text.strip()

    # Strip <think>...</think> reasoning tags (M2.5 may emit these)
    cleaned = _re.sub(r"<think>.*?</think>", "", cleaned, flags=_re.DOTALL).strip()

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
```

#### A3d. Context Budget

Add a guard in worker.analyze() to warn/truncate when total prompt size
approaches M2.5's degradation threshold:

```python
MAX_M2_CONTEXT_CHARS = 300_000  # ~90k tokens, M2.5 degrades beyond this
total_chars = len(system_prompt) + len(task_prompt)
if "minimax" in self.model and total_chars > MAX_M2_CONTEXT_CHARS:
    logger.warning(
        "M2.5 context size %d exceeds safe threshold %d — truncating file contents",
        total_chars, MAX_M2_CONTEXT_CHARS,
    )
    # Truncate file contents to fit
    context.file_contents = _truncate_contents(context.file_contents, MAX_M2_CONTEXT_CHARS - len(system_prompt) - 5000)
```

---

### A4. Claude 4.6-Specific Optimizations (Security Auditor)

#### A4a. Adaptive Thinking

If using Claude 4.6 directly (not via OpenRouter), enable adaptive thinking:

```python
thinking={"type": "adaptive"}
```

For OpenRouter passthrough, add `x-thinking` header if supported.

#### A4b. Structured Outputs (Future — when OpenRouter supports it)

Claude 4.6 supports structured outputs via beta flag
`structured-outputs-2025-11-13`. When available through the provider, use
grammar-constrained decoding for zero JSON parsing failures:

```python
output_format={
    "type": "json_schema",
    "json_schema": {
        "name": "security_findings",
        "strict": True,
        "schema": FINDING_JSON_SCHEMA,
    }
}
```

Track provider support. For now, rely on strict prompt instructions.

#### A4c. Anti-Sycophancy

Already included in the system prompt (A1a severity_calibration section).
Stanford research shows 58% sycophancy rate in Claude — the explicit
"rate based on technical impact, not feelings" instruction mitigates this.

---

## Workstream B: Project Context Intake

### B1. Data Model (`vibe2prod/backend/models/`)

Create `ProjectContext` Pydantic model:

```python
class ProjectContext(BaseModel):
    """User-provided project context for scan personalization."""

    # Core identity
    project_stage: str = ""          # "mvp" | "early_product" | "growth" | "enterprise"
    vision_summary: str = ""         # 1-3 sentences: what does this do, where is it heading
    team_size: int = 1               # solo dev = 1

    # Technical context
    known_compromises: list[str] = Field(default_factory=list)
    # e.g. ["Auth is basic — OAuth planned for next sprint",
    #        "No tests yet — adding after core features stabilize"]

    beloved_features: list[str] = Field(default_factory=list)
    # e.g. ["Real-time workout tracking", "Supabase edge functions for payments"]

    sensitive_data_types: list[str] = Field(default_factory=list)
    # e.g. ["payments", "pii", "health", "auth_secrets"]
    # (extends existing sensitive_data param in scanner)

    original_prompt: str = ""        # The vibe-coding prompt used to generate the code
    target_launch: str = ""          # "2 weeks" | "3 months" | "already in production"

    class Config:
        extra = "ignore"
```

### B2. Storage (Supabase)

Add `project_context` JSONB column to `projects` table:

```sql
-- Migration: add_project_context.sql
ALTER TABLE projects
ADD COLUMN IF NOT EXISTS project_context JSONB DEFAULT '{}';
```

### B3. API Endpoint (`vibe2prod/backend/api/routes/`)

**Option A (preferred): Extend existing scan submission.**

Add optional `project_context` field to the audit request body. Users who
don't provide it get the current behavior. Users who do get personalized
findings.

In `audit_routes.py`, the existing `POST /api/audit` handler receives the
project_context and passes it through the pipeline:

```python
class AuditRequest(BaseModel):
    repo_url: str
    tier1_only: bool = True
    sensitive_data: list[str] = Field(default_factory=list)
    project_context: dict = Field(default_factory=dict)  # NEW
```

**Option B: Separate endpoint for progressive disclosure.**

`POST /api/projects/{project_id}/context` — allows updating context before
or after a scan. Frontend can present a wizard/form that saves context
first, then triggers the scan.

**Recommendation:** Start with Option A (simpler, no new endpoint). Add
Option B later if the frontend needs a wizard flow.

### B4. Context String Builder (`forge-engine` or `vibe2prod`)

Create `build_project_context_string(ctx: ProjectContext) -> str`:

```python
def build_project_context_string(ctx: dict) -> str:
    """Build a prompt-injectable project context section."""
    if not ctx:
        return ""

    parts = ["## Project Context\n"]
    parts.append("The developer provided the following context about this project.\n")
    parts.append("Use this to calibrate your findings — what matters depends on")
    parts.append("the project's stage, scale, and goals.\n")

    stage = ctx.get("project_stage", "")
    if stage:
        stage_labels = {
            "mvp": "MVP / Prototype (early stage, solo dev likely, speed over perfection)",
            "early_product": "Early Product (small team, some users, hardening in progress)",
            "growth": "Growth Stage (active users, scaling concerns, needs production rigor)",
            "enterprise": "Enterprise (large team, compliance requirements, full production standards)",
        }
        parts.append(f"**Project Stage:** {stage_labels.get(stage, stage)}")

    team_size = ctx.get("team_size", 0)
    if team_size:
        parts.append(f"**Team Size:** {team_size} {'developer' if team_size == 1 else 'developers'}")

    vision = ctx.get("vision_summary", "")
    if vision:
        parts.append(f"**Vision:** {vision}")

    target = ctx.get("target_launch", "")
    if target:
        parts.append(f"**Target Launch:** {target}")

    compromises = ctx.get("known_compromises", [])
    if compromises:
        parts.append("\n**Known Compromises (developer is aware of these):**")
        for c in compromises:
            parts.append(f"- {c}")
        parts.append("\nIf a finding overlaps with a known compromise, note it but")
        parts.append("classify as 'informational' — the developer already knows.")

    beloved = ctx.get("beloved_features", [])
    if beloved:
        parts.append(f"\n**Beloved Features:** {', '.join(beloved)}")
        parts.append("The developer values these. Suggest minimal-impact fixes that")
        parts.append("preserve their design intent rather than architectural rewrites.")

    original_prompt = ctx.get("original_prompt", "")
    if original_prompt:
        parts.append(f"\n**Original Prompt (used to generate this codebase):**")
        parts.append(f"> {original_prompt[:500]}")

    sensitive = ctx.get("sensitive_data_types", [])
    if sensitive:
        parts.append(f"\n**Sensitive Data:** {', '.join(sensitive)}")
        parts.append("Escalate severity for any finding that could expose this data.")

    # Scale-awareness heuristic
    if stage in ("mvp", "early_product") or team_size <= 2:
        parts.append("\n**Scale Guidance:** This is a small/early-stage project.")
        parts.append("Do NOT recommend architectural patterns (repository layers,")
        parts.append("service abstractions, etc.) unless they directly fix a")
        parts.append("security vulnerability. Focus on exploitable bugs, not structure.")

    return "\n".join(parts)
```

### B5. Threading Through Pipeline

**vibe2prod (Tier 1):**
- `Tier1Orchestrator.run()` receives `project_context` dict
- Passes to `Tier1Reporter` for report framing
- Passes `sensitive_data_types` to `DeterministicScanner` (extends existing
  `sensitive_data` param)

**forge-engine (FORGE):**
- `ForgeConfig` already has `pattern_library_path`. Add `project_context: dict = {}`.
- `run_security_auditor()` builds project_context string, passes alongside
  pattern_context to task prompt
- `HiveOrchestrator` builds project_context string once, passes to all workers
- `run_standalone()` accepts optional `--project-context` JSON file arg

---

## Workstream C: Actionable Report Presentation

### C1. Actionability Tier Classification

Findings get classified into 4 tiers based on severity + project context:

| Tier | Label | Criteria |
|------|-------|----------|
| 1 | **Must Fix** | Critical/High severity + confidence >= 0.85 + concrete exploit |
| 2 | **Should Fix** | High/Medium severity + confidence >= 0.7 + evidence-based |
| 3 | **Consider** | Medium/Low severity OR overlaps with known compromise |
| 4 | **Informational** | Low severity OR architectural opinion OR deferred by context |

Classification logic (in reporter or post-processing):

```python
def classify_actionability(finding: dict, project_context: dict) -> str:
    severity = finding.get("severity", "low")
    confidence = finding.get("confidence", 0.0)
    known_compromises = project_context.get("known_compromises", [])
    stage = project_context.get("project_stage", "")

    # Check if finding overlaps with a known compromise
    is_known = any(
        comp.lower() in finding.get("description", "").lower()
        for comp in known_compromises
    )

    if is_known:
        return "informational"

    if severity == "critical" and confidence >= 0.85:
        return "must_fix"
    if severity == "high" and confidence >= 0.8:
        return "must_fix" if stage in ("growth", "enterprise") else "should_fix"
    if severity in ("high", "medium") and confidence >= 0.7:
        return "should_fix"
    if severity == "medium" and stage in ("mvp", "early_product"):
        return "consider"
    if severity == "low":
        return "informational"

    return "consider"
```

### C2. Report Framing Per Finding

Each finding in the report gets contextual framing:

**Must Fix:**
> "This is exploitable now. Fix before shipping."

**Should Fix:**
> "This is a real issue. Prioritize in your current sprint."

**Consider:**
> "We flagged this because [reason]. Given your project stage [stage] and
> vision [vision], this may not be urgent. Here's what to watch for as you
> scale: [scaling trigger]."

**Informational (known compromise overlap):**
> "You mentioned [known compromise]. We detected [finding]. Since you're
> already aware, we're noting it here for completeness. When you're ready
> to address it: [fix suggestion]."

### C3. Tier 1 Report Template Update (`vibe2prod`)

In `Tier1Reporter`, update the LLM prompt that generates the markdown report
to include:

1. Project context section (if provided)
2. Findings grouped by actionability tier, not just severity
3. Contextual framing language per tier
4. "Why We Flagged This" explanation per finding

### C4. FORGE HTML Report Template Update (`forge-engine`)

In `forge/execution/report.py`, update `_render_discovery_html()`:

1. Add project context summary box at top of report
2. Group findings by actionability tier with visual indicators:
   - Must Fix: red badge
   - Should Fix: orange badge
   - Consider: yellow badge
   - Informational: gray badge
3. Each finding card includes "Why This Matters For Your Project" section

### C5. FORGE JSON Report Update

Add to the JSON report structure:

```python
{
    "project_context": { ... },
    "findings_by_actionability": {
        "must_fix": [...],
        "should_fix": [...],
        "consider": [...],
        "informational": [...],
    },
    "summary": {
        "must_fix_count": 5,
        "should_fix_count": 3,
        "consider_count": 4,
        "informational_count": 6,
        "signal_to_noise_ratio": 0.44,  # (must_fix + should_fix) / total
    }
}
```

---

## Implementation Tasks

### Workstream A: Prompt Overhaul (forge-engine)

| File | Action | Description |
|------|--------|-------------|
| `forge/prompts/security_auditor.py` | Rewrite | XML-structured base system prompt, per-pass analysis steps, few-shot examples, hard exclusions |
| `forge/swarm/worker.py` SecurityWorker | Rewrite | XML-structured prompt, sequential analysis steps, evidence requirements, M2.5 output instructions |
| `forge/swarm/worker.py` QualityWorker | Update | Add hard exclusions, evidence requirements |
| `forge/swarm/worker.py` ArchitectureWorker | Update | Add scale-awareness, project_context injection |
| `forge/swarm/worker.py` `_parse_json_response` | Harden | Strip `<think>` tags, handle preamble text, extract JSON from noise |
| `forge/swarm/worker.py` `analyze()` | Update | M2.5 temperature/context budget guards |
| `forge/schemas.py` | Update | Add `data_flow`, `actionability` fields to `AuditFinding` |
| `tests/unit/test_security_auditor_prompt.py` | Update | Verify XML structure, project_context injection, pattern_context injection |
| `tests/unit/test_worker_prompts.py` | Create | Verify worker prompt structure, M2.5 optimizations, JSON parser hardening |

### Workstream B: Project Context Intake (both repos)

| File | Repo | Action | Description |
|------|------|--------|-------------|
| `backend/models/project_context.py` | vibe2prod | Create | `ProjectContext` Pydantic model |
| `backend/api/routes/audit_routes.py` | vibe2prod | Update | Accept `project_context` in audit request |
| `backend/tier1/orchestrator.py` | vibe2prod | Update | Thread project_context to reporter |
| `backend/tier1/reporter.py` | vibe2prod | Update | Include project_context in LLM report prompt |
| `backend/services/supabase_client.py` | vibe2prod | Update | Store/retrieve project_context |
| `supabase/migrations/` | vibe2prod | Create | Add project_context JSONB column |
| `forge/config.py` | forge-engine | Update | Add `project_context: dict = {}` |
| `forge/prompts/project_context.py` | forge-engine | Create | `build_project_context_string()` |
| `forge/reasoners/discovery.py` | forge-engine | Update | Build + thread project_context string |
| `forge/reasoners/hive_discovery.py` | forge-engine | Update | Forward project_context to orchestrator |
| `forge/swarm/orchestrator.py` | forge-engine | Update | Build project_context string, pass to workers |
| `forge/standalone.py` | forge-engine | Update | Accept `--project-context` JSON file |
| `tests/unit/test_project_context.py` | forge-engine | Create | Context string builder tests |
| `backend/tests/test_project_context.py` | vibe2prod | Create | Model + API integration tests |

### Workstream C: Actionable Reports (both repos)

| File | Repo | Action | Description |
|------|------|--------|-------------|
| `forge/execution/report.py` | forge-engine | Update | Actionability classification, tier grouping, contextual framing in HTML/JSON |
| `forge/execution/actionability.py` | forge-engine | Create | `classify_actionability()` function |
| `backend/tier1/reporter.py` | vibe2prod | Update | Tier grouping in Tier 1 report prompt |
| `backend/tier1/contracts.py` | vibe2prod | Update | Add `actionability` field to `Tier1Finding` |
| `tests/unit/test_actionability.py` | forge-engine | Create | Classification logic tests |
| `tests/unit/test_report_actionability.py` | forge-engine | Create | Report rendering with tiers |

---

## Verification Criteria

1. **All existing tests pass** — 583+ forge-engine, 69+ vibe2prod
2. **New tests:** ~25-35 across prompt structure, context builder, actionability
3. **Benchmark: self-scan comparison** — Run FORGE discovery on vibe2prod with
   new prompts vs old prompts. Target: signal-to-noise ratio improves from
   ~50% to >= 70% (must_fix + should_fix as fraction of total findings)
4. **Benchmark: frostflow_app** — VP-001 still triggers. Finding count should
   decrease (fewer false positives) while real findings are preserved.
5. **No cost increase** — Same number of LLM calls, same models, same token
   budgets. Improvements are prompt-only.
6. **Backward compatible** — project_context is optional everywhere.
   Existing scans without context produce the same results as before.

---

## Deferred Work (Future Sprint)

- **Semgrep-style "Memories":** Store triage decisions per-project. When the
  same false positive recurs, auto-suppress it. Requires user feedback loop.
- **Structured outputs via Claude API:** When OpenRouter supports the
  `structured-outputs-2025-11-13` beta, switch from prompt-based JSON to
  grammar-constrained decoding for zero parsing failures.
- **Multi-sample confidence:** Run the same analysis 3x, measure agreement.
  Findings flagged by 3/3 runs get boosted confidence.
- **Verification pass:** After discovery, run a separate "try to disprove
  each finding" pass. Anthropic's own security review tool uses this pattern.
- **Frontend wizard:** Progressive disclosure form for project context intake
  (vibe2prod frontend, out of scope for backend spec).

---

## Commit Plan (Suggested Order)

1. `forge-engine`: Schema updates (data_flow, actionability fields)
2. `forge-engine`: Security auditor prompt rewrite
3. `forge-engine`: Swarm worker prompt rewrite + M2.5 optimizations
4. `forge-engine`: JSON parser hardening
5. `forge-engine`: Project context builder
6. `forge-engine`: Actionability classifier
7. `forge-engine`: Report template updates (HTML + JSON)
8. `forge-engine`: Thread project_context through discovery + standalone
9. `forge-engine`: Tests for all above
10. `vibe2prod`: ProjectContext model + API endpoint
11. `vibe2prod`: Thread project_context through Tier 1 pipeline
12. `vibe2prod`: Reporter template updates
13. `vibe2prod`: Tests for all above
14. Both repos: Benchmark comparison (old vs new prompts)
