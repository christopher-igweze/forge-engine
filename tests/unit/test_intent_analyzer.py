"""Tests for the Intent Analyzer deterministic checks and integration points.

Covers:
- Phase 1 deterministic intent detection (suppression annotations, intent
  comments, test file classification)
- _is_test_file path and pattern matching
- _has_suppression_annotation and _has_intent_comment utility functions
- Actionability integration: intent_signal="intentional" -> informational
- Triage integration: intent_signal="intentional" -> Tier 0
"""

from __future__ import annotations

import pytest

from forge.conventions.models import ProjectConventions, QAConventions
from forge.execution.actionability import classify_actionability
from forge.execution.intent_analyzer import (
    _deterministic_intent_check,
    _has_intent_comment,
    _has_suppression_annotation,
    _is_test_file,
)
from forge.reasoners.triage import _rule_based_triage
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    FindingCategory,
    FindingLocation,
    FindingSeverity,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_finding(
    file_path: str,
    line_start: int | None = 5,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    category: FindingCategory = FindingCategory.SECURITY,
    intent_signal: str = "",
) -> AuditFinding:
    """Create a minimal AuditFinding for testing."""
    locations = []
    if file_path:
        locations = [FindingLocation(file_path=file_path, line_start=line_start)]
    return AuditFinding(
        title="Test finding",
        description="Test description",
        category=category,
        severity=severity,
        locations=locations,
        intent_signal=intent_signal,
    )


# ── TestDeterministicIntentCheck ──────────────────────────────────────


class TestDeterministicIntentCheck:
    """Phase 1 utility tests using tmp_path to create real files."""

    def test_suppression_noqa(self, tmp_path):
        """# noqa annotation on the finding line -> intentional."""
        src = tmp_path / "app.py"
        src.write_text(
            "import os\n"
            "import sys\n"
            "import subprocess\n"
            "import shlex\n"
            "result = subprocess.run(cmd, shell=True)  # noqa: S603\n"
            "print(result)\n"
        )
        finding = _make_finding("app.py", line_start=5)
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result == "intentional"

    def test_suppression_eslint_disable(self, tmp_path):
        """// eslint-disable-next-line annotation -> intentional."""
        src = tmp_path / "app.js"
        src.write_text(
            "const x = 1;\n"
            "const y = 2;\n"
            "const z = 3;\n"
            "// eslint-disable-next-line no-eval\n"
            "eval(code);\n"
            "console.log('done');\n"
        )
        finding = _make_finding("app.js", line_start=5)
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result == "intentional"

    def test_suppression_ts_ignore(self, tmp_path):
        """// @ts-ignore annotation -> intentional."""
        src = tmp_path / "app.ts"
        src.write_text(
            "const a = 1;\n"
            "const b = 2;\n"
            "// @ts-ignore\n"
            "const c: any = someUntypedThing();\n"
            "export default c;\n"
        )
        finding = _make_finding("app.ts", line_start=4)
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result == "intentional"

    def test_intent_comment(self, tmp_path):
        """Comment saying 'intentionally permissive' -> intentional."""
        src = tmp_path / "config.py"
        src.write_text(
            "import os\n"
            "# intentionally permissive — this is a dev-only endpoint\n"
            "CORS_ORIGINS = ['*']\n"
            "DEBUG = True\n"
        )
        finding = _make_finding("config.py", line_start=3)
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result == "intentional"

    def test_test_file_medium_severity(self, tmp_path):
        """Medium severity finding in tests/ dir -> intentional."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_foo.py"
        test_file.write_text(
            "def test_something():\n"
            "    assert True\n"
        )
        finding = _make_finding(
            "tests/test_foo.py",
            line_start=1,
            severity=FindingSeverity.MEDIUM,
        )
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result == "intentional"

    def test_test_file_critical_severity(self, tmp_path):
        """CRITICAL severity in test file -> None (needs LLM, don't auto-skip)."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_foo.py"
        test_file.write_text(
            "def test_something():\n"
            "    assert True\n"
        )
        finding = _make_finding(
            "tests/test_foo.py",
            line_start=1,
            severity=FindingSeverity.CRITICAL,
        )
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result is None

    def test_no_annotations_no_test(self, tmp_path):
        """Regular production file with no annotations -> None."""
        src = tmp_path / "main.py"
        src.write_text(
            "import os\n"
            "def run():\n"
            "    os.system('rm -rf /')\n"
        )
        finding = _make_finding("main.py", line_start=3)
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result is None

    def test_no_locations(self, tmp_path):
        """Finding with no locations -> None."""
        finding = _make_finding("", line_start=None)
        finding.locations = []
        result = _deterministic_intent_check(finding, str(tmp_path), None)
        assert result is None


# ── TestIsTestFile ────────────────────────────────────────────────────


class TestIsTestFile:
    """Test file detection via path segments and filename patterns."""

    def test_is_test_file_in_tests_dir(self):
        assert _is_test_file("tests/test_something.py", None) is True

    def test_is_test_file_spec_pattern(self):
        assert _is_test_file("src/app.spec.ts", None) is True

    def test_is_test_file_production(self):
        assert _is_test_file("src/main.py", None) is False

    def test_is_test_file_with_conventions(self):
        """Custom test_paths from QAConventions should match."""
        conventions = ProjectConventions(
            test=QAConventions(
                test_paths=["custom_tests/"],
                test_file_patterns=["check_*.py"],
            )
        )
        # Custom path match
        assert _is_test_file("custom_tests/check_auth.py", conventions) is True
        # Custom pattern match in non-standard dir
        assert _is_test_file("lib/check_utils.py", conventions) is True
        # Neither custom path nor pattern
        assert _is_test_file("src/service.py", conventions) is False

    def test_is_test_file_e2e_dir(self):
        assert _is_test_file("e2e/login.spec.ts", None) is True

    def test_is_test_file_test_suffix(self):
        assert _is_test_file("src/utils.test.js", None) is True

    def test_is_test_file_conftest(self):
        assert _is_test_file("tests/conftest.py", None) is True


# ── TestHasSuppressionAnnotation ──────────────────────────────────────


class TestHasSuppressionAnnotation:
    """Quick positive/negative cases for _has_suppression_annotation."""

    def test_noqa_positive(self):
        assert _has_suppression_annotation(["x = 1  # noqa\n"]) is True

    def test_pylint_disable_positive(self):
        assert _has_suppression_annotation(["# pylint: disable=C0114\n"]) is True

    def test_nosec_positive(self):
        assert _has_suppression_annotation(["run(cmd)  # nosec\n"]) is True

    def test_eslint_disable_positive(self):
        assert _has_suppression_annotation(["// eslint-disable-next-line\n"]) is True

    def test_ts_expect_error_positive(self):
        assert _has_suppression_annotation(["// @ts-expect-error\n"]) is True

    def test_suppress_warnings_positive(self):
        assert _has_suppression_annotation(["@SuppressWarnings(\"unchecked\")\n"]) is True

    def test_nolint_positive(self):
        assert _has_suppression_annotation(["// NOLINT\n"]) is True

    def test_pragma_no_cover_positive(self):
        assert _has_suppression_annotation(["if TYPE_CHECKING:  # pragma: no cover\n"]) is True

    def test_no_annotation_negative(self):
        assert _has_suppression_annotation(["x = 1\n", "y = 2\n"]) is False

    def test_empty_lines_negative(self):
        assert _has_suppression_annotation([]) is False

    def test_comment_without_suppression(self):
        assert _has_suppression_annotation(["# this is a normal comment\n"]) is False


# ── TestHasIntentComment ──────────────────────────────────────────────


class TestHasIntentComment:
    """Quick positive/negative cases for _has_intent_comment."""

    def test_intentional_keyword(self):
        assert _has_intent_comment(["# intentional — dev-only endpoint\n"]) is True

    def test_by_design_keyword(self):
        assert _has_intent_comment(["// by design: we skip validation here\n"]) is True

    def test_deliberately_keyword(self):
        assert _has_intent_comment(["# deliberately left empty\n"]) is True

    def test_on_purpose_keyword(self):
        assert _has_intent_comment(["// on purpose for testing\n"]) is True

    def test_acceptable_risk_keyword(self):
        assert _has_intent_comment(["# acceptable risk per security review\n"]) is True

    def test_known_issue_keyword(self):
        assert _has_intent_comment(["# known issue: tracked in JIRA-123\n"]) is True

    def test_no_intent_comment_negative(self):
        assert _has_intent_comment(["x = 1\n", "y = 2\n"]) is False

    def test_case_insensitive(self):
        assert _has_intent_comment(["# INTENTIONAL choice\n"]) is True


# ── TestActionabilityIntentIntegration ─────────────────────────────────


class TestActionabilityIntentIntegration:
    """Verify that actionability classification respects intent_signal."""

    def test_intentional_becomes_informational(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "category": "security",
            "intent_signal": "intentional",
        }
        assert classify_actionability(finding) == "informational"

    def test_ambiguous_normal_classification(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "category": "security",
            "intent_signal": "ambiguous",
        }
        result = classify_actionability(finding)
        assert result != "informational"  # should be should_fix based on rules

    def test_empty_intent_normal_classification(self):
        finding = {
            "severity": "high",
            "confidence": 0.9,
            "category": "security",
            "intent_signal": "",
        }
        result = classify_actionability(finding)
        assert result != "informational"

    def test_unintentional_normal_classification(self):
        finding = {
            "severity": "critical",
            "confidence": 0.95,
            "category": "security",
            "intent_signal": "unintentional",
        }
        assert classify_actionability(finding) == "must_fix"

    def test_intentional_overrides_critical(self):
        """Even critical+high-confidence is informational if intentional."""
        finding = {
            "severity": "critical",
            "confidence": 0.99,
            "category": "security",
            "intent_signal": "intentional",
        }
        assert classify_actionability(finding) == "informational"


# ── TestTriageIntentIntegration ────────────────────────────────────────


class TestTriageIntentIntegration:
    """Verify that triage rule-based classifier respects intent_signal."""

    def test_intentional_is_tier_0(self):
        finding = AuditFinding(
            title="Something benign",
            description="desc",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
            intent_signal="intentional",
        )
        cm = CodebaseMap(
            files=[],
            loc_total=0,
            file_count=0,
            primary_language="python",
            languages=["python"],
        )
        decision = _rule_based_triage(finding, cm)
        assert decision is not None
        assert decision.tier.value == 0

    def test_intentional_rationale_mentions_intent_analyzer(self):
        finding = AuditFinding(
            title="Something benign",
            description="desc",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
            intent_signal="intentional",
        )
        cm = CodebaseMap(
            files=[],
            loc_total=0,
            file_count=0,
            primary_language="python",
            languages=["python"],
        )
        decision = _rule_based_triage(finding, cm)
        assert decision is not None
        assert "Intent Analyzer" in decision.rationale

    def test_empty_intent_no_tier_0(self):
        """No intent_signal + generic title -> None (needs LLM)."""
        finding = AuditFinding(
            title="Something unique xyz",
            description="desc that does not match any rule",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
        )
        cm = CodebaseMap(
            files=[],
            loc_total=0,
            file_count=0,
            primary_language="python",
            languages=["python"],
        )
        decision = _rule_based_triage(finding, cm)
        assert decision is None

    def test_ambiguous_intent_no_tier_0(self):
        """ambiguous intent_signal should not trigger the intent rule."""
        finding = AuditFinding(
            title="Something unique xyz",
            description="desc that does not match any rule",
            category=FindingCategory.QUALITY,
            severity=FindingSeverity.MEDIUM,
            intent_signal="ambiguous",
        )
        cm = CodebaseMap(
            files=[],
            loc_total=0,
            file_count=0,
            primary_language="python",
            languages=["python"],
        )
        decision = _rule_based_triage(finding, cm)
        assert decision is None
