"""Unit tests for LLM-based finding validation."""
from __future__ import annotations

import asyncio
import json
import unittest

from forge.execution.llm_validator import (
    ValidationResult,
    apply_validation,
    validate_findings,
)


def _run(coro):
    """Helper to run an async coroutine in tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(
    *,
    check_id: str = "SEC-001",
    fid: str | None = None,
    severity: str = "high",
    file_path: str = "app/routes.py",
    line_start: int = 10,
    line_end: int = 12,
    description: str = "Hardcoded secret detected",
    snippet: str = "password = 'hunter2'",
) -> dict:
    finding: dict = {
        "check_id": check_id,
        "severity": severity,
        "file_path": file_path,
        "line_start": line_start,
        "line_end": line_end,
        "description": description,
        "snippet": snippet,
    }
    if fid is not None:
        finding["id"] = fid
    return finding


def _mock_file_reader(content: str = "line1\nline2\nline3\n"):
    """Return a file_reader callable that always returns *content*."""
    def reader(path: str) -> str:
        return content
    return reader


def _mock_llm_caller(response: dict | str):
    """Return an async llm_caller that always returns *response* as JSON string."""
    if isinstance(response, dict):
        response = json.dumps(response)

    async def caller(system_prompt: str, task_prompt: str) -> str:
        return response

    return caller


def _mock_llm_caller_per_call(responses: list[dict | str]):
    """Return an async llm_caller that returns successive responses."""
    it = iter(responses)

    async def caller(system_prompt: str, task_prompt: str) -> str:
        resp = next(it)
        return json.dumps(resp) if isinstance(resp, dict) else resp

    return caller


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidationResult(unittest.TestCase):
    """Test the ValidationResult dataclass."""

    def test_defaults(self):
        r = ValidationResult(
            finding_id="f1", confirmed=True, confidence=0.9, reasoning="real"
        )
        self.assertEqual(r.finding_id, "f1")
        self.assertTrue(r.confirmed)
        self.assertAlmostEqual(r.confidence, 0.9)
        self.assertEqual(r.reasoning, "real")
        self.assertIsNone(r.suggested_severity)

    def test_with_severity(self):
        r = ValidationResult(
            finding_id="f2",
            confirmed=False,
            confidence=0.8,
            reasoning="test fixture",
            suggested_severity="low",
        )
        self.assertFalse(r.confirmed)
        self.assertEqual(r.suggested_severity, "low")


class TestApplyValidation(unittest.TestCase):
    """Test apply_validation filtering and severity adjustment."""

    def test_removes_rejected_findings(self):
        findings = [
            _make_finding(fid="f1"),
            _make_finding(fid="f2"),
        ]
        results = [
            ValidationResult("f1", confirmed=True, confidence=0.9, reasoning="real"),
            ValidationResult("f2", confirmed=False, confidence=0.85, reasoning="test code"),
        ]
        validated = apply_validation(findings, results)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["id"], "f1")

    def test_adjusts_severity_when_suggested(self):
        findings = [_make_finding(fid="f1", severity="critical")]
        results = [
            ValidationResult(
                "f1", confirmed=True, confidence=0.7,
                reasoning="real but overstated", suggested_severity="medium",
            ),
        ]
        validated = apply_validation(findings, results)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["severity"], "medium")
        self.assertEqual(validated[0]["original_severity"], "critical")
        self.assertEqual(validated[0]["llm_reasoning"], "real but overstated")

    def test_preserves_findings_without_validation(self):
        findings = [
            _make_finding(fid="f1"),
            _make_finding(fid="f2"),
        ]
        # Only validate f1
        results = [
            ValidationResult("f1", confirmed=True, confidence=0.9, reasoning="ok"),
        ]
        validated = apply_validation(findings, results)
        # Both should be preserved
        self.assertEqual(len(validated), 2)

    def test_uses_check_id_when_no_id(self):
        """Falls back to check_id when 'id' field is absent."""
        findings = [_make_finding(check_id="SEC-005")]  # no fid
        results = [
            ValidationResult("SEC-005", confirmed=False, confidence=0.8, reasoning="nope"),
        ]
        validated = apply_validation(findings, results)
        self.assertEqual(len(validated), 0)

    def test_empty_inputs(self):
        self.assertEqual(apply_validation([], []), [])


class TestValidateFindings(unittest.TestCase):
    """Test the async validate_findings function."""

    def test_basic_confirmation(self):
        findings = [_make_finding(fid="f1")]
        llm = _mock_llm_caller({"confirmed": True, "confidence": 0.95, "reasoning": "real"})
        results = _run(validate_findings(findings, _mock_file_reader(), llm))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].confirmed)
        self.assertAlmostEqual(results[0].confidence, 0.95)

    def test_basic_rejection(self):
        findings = [_make_finding(fid="f1")]
        llm = _mock_llm_caller({
            "confirmed": False, "confidence": 0.9,
            "reasoning": "in test file", "suggested_severity": None,
        })
        results = _run(validate_findings(findings, _mock_file_reader(), llm))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].confirmed)

    def test_handles_parse_error_gracefully(self):
        findings = [_make_finding(fid="f1")]
        llm = _mock_llm_caller("NOT VALID JSON {{{")
        results = _run(validate_findings(findings, _mock_file_reader(), llm))
        self.assertEqual(len(results), 1)
        # Should default to confirmed
        self.assertTrue(results[0].confirmed)
        self.assertAlmostEqual(results[0].confidence, 0.5)
        self.assertIn("could not be parsed", results[0].reasoning)

    def test_handles_llm_exception_gracefully(self):
        async def boom(sys_prompt, task_prompt):
            raise RuntimeError("LLM unavailable")

        findings = [_make_finding(fid="f1")]
        results = _run(validate_findings(findings, _mock_file_reader(), boom))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].confirmed)
        self.assertAlmostEqual(results[0].confidence, 0.3)
        self.assertIn("LLM unavailable", results[0].reasoning)

    def test_respects_max_findings(self):
        findings = [_make_finding(fid=f"f{i}") for i in range(10)]
        call_count = 0

        async def counting_llm(sys_prompt, task_prompt):
            nonlocal call_count
            call_count += 1
            return json.dumps({"confirmed": True, "confidence": 0.8, "reasoning": "ok"})

        results = _run(validate_findings(
            findings, _mock_file_reader(), counting_llm, max_findings=3,
        ))
        self.assertEqual(len(results), 3)
        self.assertEqual(call_count, 3)

    def test_prioritizes_high_severity(self):
        findings = [
            _make_finding(fid="low1", severity="low"),
            _make_finding(fid="crit1", severity="critical"),
            _make_finding(fid="med1", severity="medium"),
            _make_finding(fid="high1", severity="high"),
        ]
        order: list[str] = []

        async def tracking_llm(sys_prompt, task_prompt):
            # Extract severity from the prompt
            for line in task_prompt.splitlines():
                if line.startswith("Severity:"):
                    order.append(line.split(":")[1].strip())
                    break
            return json.dumps({"confirmed": True, "confidence": 0.9, "reasoning": "ok"})

        _run(validate_findings(findings, _mock_file_reader(), tracking_llm))
        self.assertEqual(order, ["critical", "high", "medium", "low"])

    def test_file_reader_failure_uses_snippet(self):
        """When file_reader raises, the snippet from the finding is used."""
        findings = [_make_finding(fid="f1", snippet="password = 'secret'")]

        def bad_reader(path):
            raise FileNotFoundError(path)

        prompts_seen: list[str] = []

        async def capture_llm(sys_prompt, task_prompt):
            prompts_seen.append(task_prompt)
            return json.dumps({"confirmed": True, "confidence": 0.8, "reasoning": "ok"})

        _run(validate_findings(findings, bad_reader, capture_llm))
        self.assertIn("password = 'secret'", prompts_seen[0])

    def test_severity_suggestion_passed_through(self):
        findings = [_make_finding(fid="f1")]
        llm = _mock_llm_caller({
            "confirmed": True, "confidence": 0.7,
            "reasoning": "real", "suggested_severity": "low",
        })
        results = _run(validate_findings(findings, _mock_file_reader(), llm))
        self.assertEqual(results[0].suggested_severity, "low")

    def test_empty_findings(self):
        llm = _mock_llm_caller({"confirmed": True, "confidence": 1.0, "reasoning": "ok"})
        results = _run(validate_findings([], _mock_file_reader(), llm))
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
