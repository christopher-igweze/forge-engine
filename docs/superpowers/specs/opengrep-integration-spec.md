# FORGE — Opengrep Integration Spec

## Summary

Replace FORGE's 48 regex-based deterministic checks (`forge/evaluation/checks/`) with Opengrep, a proper SAST engine with cross-function taint analysis. Opengrep ships as a dependency of forge-engine — users get it automatically via `pip install vibe2prod`.

## Pipeline After Integration

```
Codebase
    │
    ├─── Step 1: Opengrep Scan (deterministic)
    │    ├── Runs all Opengrep rules (community + FORGE-custom)
    │    ├── Cross-function taint analysis
    │    ├── Pattern matching (AST-aware, not regex)
    │    ├── Secret detection
    │    ├── Outputs SARIF/JSON findings
    │    └── Same code = same results, every time
    │
    ├─── Step 2: LLM Auditors (rubric-based, advisory)
    │    ├── Security: evaluates against OWASP ASVS requirements
    │    ├── Quality: evaluates against ISO 25010 criteria
    │    ├── Architecture: evaluates against structural requirements
    │    ├── Intent detection (test files, conventions, ADRs)
    │    └── Finds business logic flaws Opengrep can't reason about
    │
    ├─── Step 3: LLM Validator (confirms/rejects)
    │    ├── Takes deterministic findings from Step 1
    │    ├── Validates true positive vs false positive with code context
    │    ├── Adjusts severity based on reachability/exploitability
    │    └── Filters noise from pattern matching
    │
    ├─── Step 4: Merge + Deduplicate
    │    ├── Merge Opengrep findings (confirmed by validator) + LLM findings
    │    ├── Deduplicate by file + location + category
    │    └── Tag source: "deterministic" vs "llm-advisory"
    │
    └─── Step 5: Scoring + Quality Gate
         ├── Readiness score from deterministic findings (stable)
         ├── LLM findings are advisory (don't affect score)
         ├── Baseline comparison (fingerprints, fuzzy matching)
         └── Quality gate pass/fail
```

## What Opengrep Replaces

| Current (regex checks) | After (Opengrep) |
|------------------------|-------------------|
| 48 regex patterns in `forge/evaluation/checks/` | 2000+ community rules + FORGE custom rules |
| String matching only | AST-aware pattern matching |
| No data flow tracking | Cross-function taint analysis |
| Misses encoded/split secrets | Tracks data through function chains |
| Python only | 30+ languages |
| Custom maintenance burden | Community-maintained rules |

## What Opengrep Does NOT Replace

LLM auditors still handle:
- Business logic flaws (auth design, access control logic)
- OWASP ASVS requirement evaluation (needs reasoning)
- Architecture assessment (needs understanding of intent)
- Context-aware severity (is this reachable? is it in a test?)
- Intent detection (conventions, ADR decisions)

## Implementation

### 1. Dependency

Add `opengrep` to `pyproject.toml` dependencies:
```toml
[project]
dependencies = [
    "opengrep>=1.0",
    # ... existing deps
]
```

If Opengrep doesn't have a pip package, bundle the binary or use a subprocess call to the CLI.

### 2. FORGE-Custom Rules (`forge/rules/`)

Create YAML rules in Semgrep/Opengrep format for FORGE-specific checks:

```
forge/rules/
    security/
        hardcoded-secrets.yml
        sql-injection.yml
        xss.yml
        ssrf.yml
        path-traversal.yml
        insecure-crypto.yml
        auth-bypass.yml
    quality/
        error-handling.yml
        code-duplication.yml
    performance/
        n-plus-one.yml
        missing-pagination.yml
    operations/
        health-check.yml
        logging.yml
```

Example rule:
```yaml
rules:
  - id: forge.security.hardcoded-secret
    patterns:
      - pattern: $VAR = "..."
      - metavariable-regex:
          metavariable: $VAR
          regex: (password|secret|api_key|token|private_key)
      - pattern-not-inside: |
          def test_...(...):
              ...
    message: "Hardcoded secret in variable '$VAR'"
    severity: ERROR
    metadata:
      category: security
      subcategory: hardcoded-secrets
      cwe: CWE-798
      owasp: A07:2021
      forge-check-id: SEC-001
      asvs-requirement: V6.4.1
```

### 3. Opengrep Runner (`forge/execution/opengrep_runner.py`)

```python
"""Run Opengrep as a subprocess and parse results."""

class OpengrepRunner:
    def __init__(self, rules_dir: str | None = None):
        """Initialize with FORGE rules directory.

        Falls back to community rules if no custom rules provided.
        """

    async def scan(self, repo_path: str) -> list[OpengrepFinding]:
        """Run Opengrep scan on a repository.

        1. Run opengrep with FORGE custom rules + relevant community rules
        2. Parse SARIF/JSON output
        3. Convert to FORGE finding format
        4. Return deterministic findings
        """

    def _parse_sarif(self, sarif: dict) -> list[OpengrepFinding]:
        """Parse SARIF output into FORGE findings."""

    def _to_forge_finding(self, result: dict) -> OpengrepFinding:
        """Convert single Opengrep result to FORGE finding format."""
```

### 4. Pipeline Integration (`forge/phases.py`)

In `_run_discovery()`, before the LLM auditors:

```python
# Step 1: Deterministic scan via Opengrep
from forge.execution.opengrep_runner import OpengrepRunner

runner = OpengrepRunner(rules_dir=str(Path(__file__).parent / "rules"))
opengrep_findings = await runner.scan(state.repo_path)
state.deterministic_findings = opengrep_findings

# Step 2: LLM auditors run (existing code, now rubric-based)
# ...

# Step 3: LLM validator confirms/rejects deterministic findings
from forge.execution.llm_validator import validate_findings, apply_validation
validation_results = await validate_findings(
    opengrep_findings, file_reader, llm_caller
)
confirmed_findings = apply_validation(opengrep_findings, validation_results)

# Step 4: Merge deterministic (confirmed) + LLM advisory findings
all_findings = confirmed_findings + llm_findings

# Step 5: Score based on deterministic findings only
```

### 5. Scoring Changes

The readiness score should come from Opengrep findings (deterministic):
- Each confirmed Opengrep finding deducts from the score
- LLM findings are tagged as `source: "llm-advisory"` and don't affect score
- Quality gate evaluates Opengrep findings only

### 6. What Happens to `forge/evaluation/checks/`

Keep it as a fallback for when Opengrep is not installed. But the default pipeline uses Opengrep.

```python
if opengrep_available():
    findings = await opengrep_runner.scan(repo_path)
else:
    logger.warning("Opengrep not installed — falling back to built-in checks")
    findings = run_builtin_checks(repo_path)
```

### 7. Community Rules Selection

Don't run ALL 2000+ community rules — select relevant ones based on the codebase:
- Detect language from file extensions
- Load rules for detected languages only
- Always load FORGE custom rules
- Use Opengrep's `--include` / `--exclude` for file filtering

## Testing

- Unit tests for `opengrep_runner.py` (mock subprocess)
- Unit tests for SARIF parsing
- Integration test: run Opengrep on a known-vulnerable fixture repo
- Verify determinism: two runs on same code produce identical results
- Verify scoring: Opengrep findings affect score, LLM findings don't

## Migration Path

1. Add Opengrep dependency
2. Write FORGE custom rules (start with top 20 most common findings)
3. Wire into pipeline alongside existing checks
4. Validate determinism
5. Remove old regex checks once Opengrep coverage is confirmed

## Open Questions

- Does Opengrep have a Python package on PyPI, or do we need to bundle the binary?
- Should we vendor specific community rules or pull them at install time?
- What's the scan time for a 30K LOC repo? (Need to benchmark)
- Should we run Opengrep in a subprocess or use its Python bindings (if any)?
