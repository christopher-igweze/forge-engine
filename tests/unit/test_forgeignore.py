"""Tests for .forgeignore parser and filter."""
import pytest
from pathlib import Path
from forge.execution.forgeignore import ForgeIgnore, IgnoreRule, SuppressionRule


def _make_finding(
    title="Test",
    category="security",
    severity="high",
    file_path="backend/auth.py",
    line_start=1,
    line_end=None,
    check_id=None,
    rule_family=None,
    enclosing_symbol=None,
):
    f = {
        "title": title,
        "category": category,
        "severity": severity,
        "locations": [{"file_path": file_path, "line_start": line_start, "line_end": line_end or line_start}],
    }
    if check_id:
        f["check_id"] = check_id
    if rule_family:
        f["rule_family"] = rule_family
    if enclosing_symbol:
        f["enclosing_symbol"] = enclosing_symbol
    return f


class TestIgnoreRule:
    def test_pattern_match(self):
        rule = IgnoreRule(pattern="probe.*authentication", reason="by design")
        assert rule.matches(_make_finding(title="Probe route missing authentication"))

    def test_pattern_no_match(self):
        rule = IgnoreRule(pattern="probe.*authentication", reason="by design")
        assert not rule.matches(_make_finding(title="SQL injection in auth"))

    def test_category_filter(self):
        rule = IgnoreRule(category="architecture", reason="opinions")
        assert rule.matches(_make_finding(category="architecture"))
        assert not rule.matches(_make_finding(category="security"))

    def test_max_severity(self):
        rule = IgnoreRule(category="architecture", max_severity="medium", reason="cap")
        assert rule.matches(_make_finding(category="architecture", severity="medium"))
        assert rule.matches(_make_finding(category="architecture", severity="low"))
        assert not rule.matches(_make_finding(category="architecture", severity="high"))

    def test_path_glob(self):
        rule = IgnoreRule(path="supabase/migrations/**", reason="historical")
        assert rule.matches(_make_finding(file_path="supabase/migrations/001.sql"))
        assert not rule.matches(_make_finding(file_path="backend/auth.py"))

    def test_expired_rule(self):
        rule = IgnoreRule(pattern=".*", reason="temp", expires="2020-01-01")
        assert not rule.matches(_make_finding())

    def test_future_expiry(self):
        rule = IgnoreRule(pattern=".*", reason="temp", expires="2099-01-01")
        assert rule.matches(_make_finding())


class TestForgeIgnore:
    def test_load_missing_file(self, tmp_path):
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 0

    def test_load_yaml(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(
            '- pattern: "probe.*auth"\n  reason: "by design"\n'
        )
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 1

    def test_apply_splits(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(
            '- pattern: "probe"\n  reason: "by design"\n'
        )
        fi = ForgeIgnore.load(str(tmp_path))
        findings = [
            _make_finding(title="Probe auth missing"),
            _make_finding(title="SQL injection"),
        ]
        kept, suppressed = fi.apply(findings)
        assert len(kept) == 1
        assert len(suppressed) == 1
        assert suppressed[0]["suppressed"] is True

    def test_invalid_yaml(self, tmp_path):
        (tmp_path / ".forgeignore").write_text("not: valid: yaml: [")
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 0


class TestBackwardCompatibility:
    """IgnoreRule is an alias for SuppressionRule."""

    def test_ignore_rule_is_suppression_rule(self):
        assert IgnoreRule is SuppressionRule

    def test_ignore_rule_works_as_before(self):
        rule = IgnoreRule(pattern="probe.*auth", reason="by design")
        assert rule.matches(_make_finding(title="Probe auth bypass"))


class TestSuppressionRuleMatching:
    """Multi-strategy matching precedence tests."""

    # --- Strategy 1: check_id ---

    def test_check_id_exact_match(self):
        rule = SuppressionRule(check_id="SEC-001", reason="template text")
        assert rule.matches(_make_finding(check_id="SEC-001"))

    def test_check_id_no_match(self):
        rule = SuppressionRule(check_id="SEC-001", reason="template text")
        assert not rule.matches(_make_finding(check_id="SEC-002"))

    def test_check_id_scoped_to_file(self):
        rule = SuppressionRule(check_id="SEC-001", file="forge/cli.py", reason="CLI only")
        assert rule.matches(_make_finding(check_id="SEC-001", file_path="forge/cli.py"))
        assert not rule.matches(_make_finding(check_id="SEC-001", file_path="forge/mcp_server.py"))

    # --- Strategy 2: rule_family + file + line_range ---

    def test_rule_family_file_line_range_match(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/mcp_server.py",
            line_range=(80, 100),
            reason="sample value",
        )
        finding = _make_finding(
            rule_family="hardcoded-secret",
            file_path="forge/mcp_server.py",
            line_start=90,
        )
        assert rule.matches(finding)

    def test_rule_family_file_line_range_outside(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/mcp_server.py",
            line_range=(80, 100),
            reason="sample value",
        )
        finding = _make_finding(
            rule_family="hardcoded-secret",
            file_path="forge/mcp_server.py",
            line_start=150,
        )
        assert not rule.matches(finding)

    # --- Strategy 3: rule_family + file + symbol ---

    def test_rule_family_file_symbol_match(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/mcp_server.py",
            symbol="_send_telemetry",
            reason="sample value",
        )
        finding = _make_finding(
            rule_family="hardcoded-secret",
            file_path="forge/mcp_server.py",
            enclosing_symbol="_send_telemetry",
        )
        assert rule.matches(finding)

    def test_rule_family_file_symbol_no_match(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/mcp_server.py",
            symbol="_send_telemetry",
            reason="sample value",
        )
        finding = _make_finding(
            rule_family="hardcoded-secret",
            file_path="forge/mcp_server.py",
            enclosing_symbol="other_function",
        )
        assert not rule.matches(finding)

    # --- Strategy 4: rule_family + file ---

    def test_rule_family_file_broad_match(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            file="forge/cli.py",
            reason="CLI tool",
        )
        finding = _make_finding(
            rule_family="missing-rate-limit",
            file_path="forge/cli.py",
        )
        assert rule.matches(finding)

    def test_rule_family_file_wrong_file(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            file="forge/cli.py",
            reason="CLI tool",
        )
        finding = _make_finding(
            rule_family="missing-rate-limit",
            file_path="forge/mcp_server.py",
        )
        assert not rule.matches(finding)

    # --- rule_family global (no file) ---

    def test_rule_family_global(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            reason="CLI tool, no web server",
        )
        finding = _make_finding(rule_family="missing-rate-limit")
        assert rule.matches(finding)

    def test_rule_family_global_wrong_family(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            reason="CLI tool",
        )
        finding = _make_finding(rule_family="hardcoded-secret")
        assert not rule.matches(finding)

    # --- Strategy 5: legacy pattern ---

    def test_legacy_pattern_fallback(self):
        rule = SuppressionRule(pattern="probe.*auth", reason="by design")
        assert rule.matches(_make_finding(title="Probe auth bypass"))

    def test_legacy_pattern_no_match(self):
        rule = SuppressionRule(pattern="probe.*auth", reason="by design")
        assert not rule.matches(_make_finding(title="SQL injection"))

    # --- Expiry ---

    def test_expired_rule_blocks_match(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            reason="temp",
            expires="2020-01-01",
        )
        finding = _make_finding(rule_family="hardcoded-secret")
        assert not rule.matches(finding)

    def test_future_expiry_allows_match(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            reason="temp",
            expires="2099-01-01",
        )
        finding = _make_finding(rule_family="hardcoded-secret")
        assert rule.matches(finding)

    # --- Category + severity filters ---

    def test_rule_family_with_category_filter(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            category="security",
            reason="not applicable",
        )
        assert rule.matches(_make_finding(rule_family="missing-rate-limit", category="security"))
        assert not rule.matches(_make_finding(rule_family="missing-rate-limit", category="quality"))

    def test_rule_family_with_severity_cap(self):
        rule = SuppressionRule(
            rule_family="missing-rate-limit",
            max_severity="medium",
            reason="low risk",
        )
        assert rule.matches(_make_finding(rule_family="missing-rate-limit", severity="low"))
        assert rule.matches(_make_finding(rule_family="missing-rate-limit", severity="medium"))
        assert not rule.matches(_make_finding(rule_family="missing-rate-limit", severity="high"))

    # --- Precedence: check_id wins over rule_family ---

    def test_check_id_takes_precedence_over_rule_family(self):
        """If check_id is set, rule_family is not checked."""
        rule = SuppressionRule(
            check_id="SEC-001",
            rule_family="hardcoded-secret",
            reason="test",
        )
        # Matches on check_id even though rule_family differs
        finding = _make_finding(check_id="SEC-001", rule_family="other-family")
        assert rule.matches(finding)

    def test_file_glob_matching(self):
        """File glob patterns should work with wildcards."""
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/*.py",
            reason="all forge files",
        )
        assert rule.matches(_make_finding(rule_family="hardcoded-secret", file_path="forge/cli.py"))
        assert not rule.matches(_make_finding(rule_family="hardcoded-secret", file_path="tests/test_cli.py"))


class TestForgeIgnoreV2Loading:
    """Tests for v2 .forgeignore format loading."""

    V2_CONTENT = """\
version: 2
suppressions:
  - id: sup_001
    kind: false_positive
    match:
      rule_family: hardcoded-secret
      file: forge/mcp_server.py
      line_range: [80, 100]
      anchor:
        symbol: _send_telemetry
        snippet_hash: 8f31c2d
    reason: Telemetry sample value, not a real secret
    expires: "2099-06-01"

  - id: sup_002
    kind: not_applicable
    match:
      rule_family: missing-rate-limit
      file: forge/cli.py
    reason: CLI tool, not a web server

  - id: sup_003
    kind: false_positive
    match:
      check_id: SEC-001
    reason: Template text, not actual secrets
"""

    def test_load_v2_format(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 3

    def test_v2_rule_fields(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))

        rule = fi.rules[0]
        assert rule.id == "sup_001"
        assert rule.kind == "false_positive"
        assert rule.rule_family == "hardcoded-secret"
        assert rule.file == "forge/mcp_server.py"
        assert rule.line_range == (80, 100)
        assert rule.symbol == "_send_telemetry"
        assert rule.snippet_hash == "8f31c2d"
        assert rule.reason == "Telemetry sample value, not a real secret"
        assert rule.expires == "2099-06-01"

    def test_v2_check_id_rule(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))

        rule = fi.rules[2]
        assert rule.check_id == "SEC-001"
        assert rule.kind == "false_positive"

    def test_v2_matching_works(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))

        # Should match sup_002 (rule_family + file)
        finding = _make_finding(
            rule_family="missing-rate-limit",
            file_path="forge/cli.py",
        )
        suppressed, reason = fi.is_suppressed(finding)
        assert suppressed
        assert reason == "CLI tool, not a web server"

    def test_v2_check_id_matching(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))

        finding = _make_finding(check_id="SEC-001")
        suppressed, reason = fi.is_suppressed(finding)
        assert suppressed
        assert reason == "Template text, not actual secrets"

    def test_v2_rejects_missing_reason(self, tmp_path):
        content = """\
version: 2
suppressions:
  - id: sup_bad
    kind: false_positive
    match:
      rule_family: hardcoded-secret
"""
        (tmp_path / ".forgeignore").write_text(content)
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 0

    def test_v2_apply_splits_findings(self, tmp_path):
        (tmp_path / ".forgeignore").write_text(self.V2_CONTENT)
        fi = ForgeIgnore.load(str(tmp_path))

        findings = [
            _make_finding(check_id="SEC-001", title="Secret found"),
            _make_finding(rule_family="sql-injection", title="SQL injection"),
        ]
        kept, suppressed = fi.apply(findings)
        assert len(kept) == 1
        assert len(suppressed) == 1
        assert suppressed[0]["check_id"] == "SEC-001"


class TestForgeIgnoreV1BackwardCompat:
    """Ensure v1 format still loads and works correctly."""

    def test_v1_with_check_id(self, tmp_path):
        content = """\
- check_id: "SEC-001"
  type: "false_positive"
  reason: "Template text"
"""
        (tmp_path / ".forgeignore").write_text(content)
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 1
        assert fi.rules[0].check_id == "SEC-001"
        assert fi.rules[0].kind == "false_positive"

        finding = _make_finding(check_id="SEC-001")
        assert fi.is_suppressed(finding) == (True, "Template text")

    def test_v1_with_path(self, tmp_path):
        content = """\
- path: "migrations/**"
  reason: "historical code"
"""
        (tmp_path / ".forgeignore").write_text(content)
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 1
        # v1 path maps to file
        assert fi.rules[0].file == "migrations/**"

    def test_v1_mixed_rules(self, tmp_path):
        content = """\
- pattern: "probe.*auth"
  reason: "by design"
- check_id: "ARCH-001"
  type: "not_applicable"
  reason: "monolith is fine"
- path: "test_fixtures/**"
  reason: "test code"
"""
        (tmp_path / ".forgeignore").write_text(content)
        fi = ForgeIgnore.load(str(tmp_path))
        assert len(fi.rules) == 3


class TestSerializeForPrompt:
    def test_serialize_v2_rule(self):
        rule = SuppressionRule(
            rule_family="hardcoded-secret",
            file="forge/mcp_server.py",
            symbol="_send_telemetry",
            kind="false_positive",
            reason="Sample value",
        )
        fi = ForgeIgnore(rules=[rule])
        text = fi.serialize_for_prompt()
        assert "Rule Family: hardcoded-secret" in text
        assert "File: forge/mcp_server.py" in text
        assert "Symbol: _send_telemetry" in text
        assert "Reason: Sample value" in text

    def test_serialize_empty(self):
        fi = ForgeIgnore(rules=[])
        assert fi.serialize_for_prompt() == ""
