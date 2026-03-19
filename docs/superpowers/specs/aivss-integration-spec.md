# FORGE — OWASP AIVSS Integration Spec

## Summary

Implement the OWASP AI Vulnerability Scoring System (AIVSS) in FORGE to score agentic AI projects against industry-standard security metrics. This makes FORGE one of the first tools to implement AIVSS scoring — a genuine differentiator.

Reference: https://aivss.owasp.org/

## What AIVSS Is

AIVSS extends CVSS v4.0 for AI/agent systems. It scores vulnerabilities on a 0-10 scale using three components:

```
AIVSS_Score = ((CVSS_Base + AARS) / 2) × Threat_Multiplier
```

- **CVSS_Base**: Traditional vulnerability score (attack vector, complexity, etc.)
- **AARS**: Agentic AI Risk Score — 10 factors measuring how the agent's architecture amplifies risk
- **Threat_Multiplier**: Current exploitability (mapped to CVSS v4 Exploit Maturity)

## The Three Scoring Layers

### Layer 1: Base Metrics (5 params)

| Metric | Values |
|--------|--------|
| Attack Vector (AV) | Network (0.85), Adjacent (0.62), Local (0.55), Physical (0.20) |
| Attack Complexity (AC) | Low (0.77), High (0.44) |
| Privileges Required (PR) | None (0.85), Low (0.62), High (0.27) |
| User Interaction (UI) | None (0.85), Required (0.62) |
| Scope (S) | Unchanged (1.00), Changed (1.50) |

`Base = min(10, AV × AC × PR × UI × S)`

### Layer 2: AI-Specific Metrics (5 params)

Each scored 1.0 (no vuln) to 0.2 (critical):

| Metric | Description |
|--------|-------------|
| Model Robustness (MR) | Resilience to adversarial attacks |
| Data Sensitivity (DS) | Confidentiality/integrity risks of training data |
| Ethical Impact (EI) | Bias, transparency, accountability concerns |
| Decision Criticality (DC) | Consequences of incorrect/malicious decisions |
| Adaptability (AD) | Ability to evolve while maintaining security |

`AI_Metrics = MR × DS × EI × DC × AD`

### Layer 3: AARS — Agentic AI Risk Score (10 amplification factors)

Each scored 0.0, 0.5, or 1.0:

| # | Factor | 0.0 (Low) | 0.5 (Medium) | 1.0 (High) |
|---|--------|-----------|-------------|------------|
| 1 | Execution Autonomy | Human approves every action | Human-in-the-loop for critical actions | Fully autonomous |
| 2 | External Tool Control Surface | No external tools | Limited tool set, sandboxed | Unrestricted tool access |
| 3 | Natural Language Interface | No NL input | NL input with validation | Raw NL input, no sanitization |
| 4 | Contextual Awareness | No environment context | Limited context (config only) | Full environment access |
| 5 | Behavioral Non-Determinism | Deterministic outputs | Mostly deterministic (low temp) | Highly non-deterministic |
| 6 | Opacity & Reflexivity | Full reasoning trace | Partial trace (logs) | Black box, no trace |
| 7 | Persistent State Retention | Stateless | Session-only state | Cross-session persistent memory |
| 8 | Dynamic Identity | Fixed identity | Role-based identity | Can assume arbitrary identities |
| 9 | Multi-Agent Interactions | Single agent | Coordinated agents, supervised | Unsupervised multi-agent |
| 10 | Self-Modification | No self-modification | Config-level self-tuning | Can modify own code/prompts |

`AARS = sum(all_factors) / 10 × 10` (normalized to 0-10 scale)

### Impact Metrics (4 params)

Each scored 0.0 (none) to 1.0 (critical):

| Metric | Description |
|--------|-------------|
| Confidentiality (C) | Data exposure risk |
| Integrity (I) | Data/system corruption risk |
| Availability (A) | Service disruption risk |
| Safety (SI) | Physical/human safety risk |

`Impact = (C + I + A + SI) / 4`

### Final Score

```
AIVSS = (0.25 × Base) + (0.45 × AI_Metrics_Normalized) + (0.30 × Impact)
```

Where AI_Metrics_Normalized incorporates both the AI-specific metrics AND the AARS factors.

Alternative (from Lakera article):
```
AIVSS = ((CVSS_Base + AARS) / 2) × Threat_Multiplier
```

Implement BOTH formulas and let the user choose via config.

### Severity Bands

| Score | Severity |
|-------|----------|
| 0.0 | None |
| 0.1-3.9 | Low |
| 4.0-6.9 | Medium |
| 7.0-8.9 | High |
| 9.0-10.0 | Critical |

## Implementation in FORGE

### 1. AIVSS Scorer Module (`forge/evaluation/aivss.py`)

```python
"""OWASP AIVSS implementation for scoring agentic AI vulnerabilities."""

@dataclass
class AIVSSInput:
    """All input parameters for AIVSS scoring."""
    # Base metrics
    attack_vector: float  # 0.20-0.85
    attack_complexity: float  # 0.44-0.77
    privileges_required: float  # 0.27-0.85
    user_interaction: float  # 0.62-0.85
    scope: float  # 1.00-1.50

    # AI-specific metrics (1.0 = no vuln, 0.2 = critical)
    model_robustness: float
    data_sensitivity: float
    ethical_impact: float
    decision_criticality: float
    adaptability: float

    # AARS factors (0.0, 0.5, or 1.0)
    execution_autonomy: float
    tool_control_surface: float
    natural_language_interface: float
    contextual_awareness: float
    behavioral_non_determinism: float
    opacity_reflexivity: float
    persistent_state: float
    dynamic_identity: float
    multi_agent_interactions: float
    self_modification: float

    # Impact metrics (0.0-1.0)
    confidentiality_impact: float
    integrity_impact: float
    availability_impact: float
    safety_impact: float

    # Threat multiplier (optional, default 1.0)
    threat_multiplier: float = 1.0


@dataclass
class AIVSSResult:
    """Scored AIVSS result."""
    score: float  # 0-10
    severity: str  # None/Low/Medium/High/Critical
    base_score: float
    ai_score: float
    aars_score: float
    impact_score: float
    factor_breakdown: dict[str, float]


def calculate_aivss(input: AIVSSInput) -> AIVSSResult:
    """Calculate AIVSS score from input parameters."""
    ...
```

### 2. Auto-Detection from Code (`forge/evaluation/aivss_detector.py`)

FORGE should automatically detect AARS factors by analyzing the codebase:

```python
"""Auto-detect AARS amplification factors from code analysis."""

def detect_aars_factors(codebase_map: CodebaseMap, findings: list[dict]) -> dict[str, float]:
    """Analyze codebase to automatically score AARS factors.

    Detection heuristics:
    1. Execution Autonomy: Look for human-approval patterns, confirmation prompts
    2. Tool Control Surface: Count tool/function call registrations, MCP tools
    3. Natural Language Interface: Check for prompt input handling, injection defenses
    4. Contextual Awareness: Check for env var access, file system access, network calls
    5. Non-Determinism: Check LLM temperature settings, random/sampling usage
    6. Opacity: Check for logging, tracing, audit trail implementations
    7. Persistent State: Check for session storage, database state, memory systems
    8. Dynamic Identity: Check for role switching, identity delegation
    9. Multi-Agent: Check for agent spawning, message passing, orchestration
    10. Self-Modification: Check for code generation, config mutation, prompt modification
    """
```

### 3. Pipeline Integration

After the deterministic checks and LLM auditors run, compute the AIVSS score:

```python
# In forge/phases.py, after evaluation:

# Auto-detect AARS factors from codebase analysis
aars_factors = detect_aars_factors(state.codebase_map, all_findings)

# Calculate AIVSS score
aivss_input = build_aivss_input(
    findings=all_findings,
    aars_factors=aars_factors,
    codebase_map=state.codebase_map,
)
aivss_result = calculate_aivss(aivss_input)

# Include in report
state.aivss_score = aivss_result
```

### 4. Report Output

```
AIVSS Score: 6.2/10 (Medium)

Base Score: 4.8  |  AI Score: 7.1  |  AARS: 5.5  |  Impact: 6.8

AARS Factors:
  Execution Autonomy       ██████████░░░░░  0.5  Human-in-the-loop
  Tool Control Surface     ██████████████░  1.0  Unrestricted tools
  Natural Language Input   ██████████░░░░░  0.5  Validated NL input
  Contextual Awareness     ██████████████░  1.0  Full env access
  Non-Determinism          ██████████████░  1.0  High temperature LLM
  Opacity                  ██████████░░░░░  0.5  Partial logging
  Persistent State         ░░░░░░░░░░░░░░░  0.0  Stateless
  Dynamic Identity         ░░░░░░░░░░░░░░░  0.0  Fixed identity
  Multi-Agent              ██████████████░  1.0  Multi-agent system
  Self-Modification        ░░░░░░░░░░░░░░░  0.0  No self-modification

Compliance: OWASP AIVSS v0.5 (targeting v1.0 at RSAC 2026)
```

### 5. CLI Flag

```bash
vibe2prod scan ./my-app --aivss  # Include AIVSS scoring in report
```

### 6. MCP Tool Extension

Add AIVSS fields to the `forge_scan` response so the MCP server returns AIVSS data.

## Files to Create/Modify

```
forge/evaluation/
    aivss.py           # AIVSS calculator (scoring formulas)
    aivss_detector.py  # Auto-detect AARS factors from code
tests/unit/
    test_aivss.py      # Unit tests for scoring
    test_aivss_detector.py  # Tests for auto-detection
forge/phases.py        # Wire into discovery pipeline
forge/schemas.py       # Add AIVSSResult to ForgeResult
forge/cli.py           # Add --aivss flag
forge/mcp_server.py    # Include in MCP response
```

## Testing

- Calculator produces correct scores for known inputs
- Auto-detection correctly identifies factors in sample repos
- Determinism: same code = same AIVSS score
- Edge cases: all-zero inputs, all-max inputs, missing data

## Open Questions

- Should AIVSS replace the current readiness score or complement it?
- Should auto-detection use LLM for ambiguous factors (e.g., "is this human-in-the-loop?")?
- How to handle non-agentic codebases (no agents = AARS is all zeros)?
