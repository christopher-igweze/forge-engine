"""Tests for .forgeignore parser and filter."""
import pytest
from pathlib import Path
from forge.execution.forgeignore import ForgeIgnore, IgnoreRule


def _make_finding(title="Test", category="security", severity="high", file_path="backend/auth.py"):
    return {
        "title": title,
        "category": category,
        "severity": severity,
        "locations": [{"file_path": file_path, "line_start": 1}],
    }


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
