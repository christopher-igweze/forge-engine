# Golden Test Suite

Ground truth test cases for validating FORGE detection accuracy and prompt optimization.

## Structure

```
tests/golden/
  codebases/
    <codebase_name>/
      expected.json       # Ground truth: expected findings and score range
      <source files>      # Intentionally flawed code
  test_golden_suite.py    # Deterministic golden tests (no LLM calls)
```

## expected.json Schema

```json
{
  "test_id": "unique-test-id",
  "description": "Human-readable description of the test case",
  "expected_findings": [
    {
      "title": "Finding title (fuzzy matched)",
      "category": "SECURITY | QUALITY | ARCHITECTURE",
      "severity": "CRITICAL | HIGH | MEDIUM | LOW"
    }
  ],
  "expected_fixes": [
    {
      "finding_title": "Which finding this fix addresses",
      "expected_outcome": "completed | completed_with_debt"
    }
  ],
  "expected_score_range": [min_score, max_score]
}
```

## Usage

### Deterministic tests (no LLM, fast)

```bash
cd /path/to/forge-engine
PYTHONPATH=. pytest tests/golden/ -m golden -q
```

### A/B validation (used by learning loop)

```python
from forge.learning.validation import load_golden_tests

tests = load_golden_tests(Path("tests/golden/codebases"))
```

## Adding a New Golden Test

1. Create a new directory under `codebases/` with intentionally flawed code
2. Add an `expected.json` file with the expected findings and score range
3. Add deterministic test cases to `test_golden_suite.py` if applicable
