"""Tests for finding fingerprint generation."""
import pytest
from forge.execution.fingerprint import fingerprint, _normalize_title


class TestNormalizeTitle:
    def test_strips_file_paths(self):
        assert "N" not in _normalize_title("Missing auth in handler")
        result = _normalize_title("God module: backend/services/supabase_client.py exceeds 1400 LOC")
        assert "backend/services" not in result
        assert "supabase_client" not in result

    def test_strips_numbers(self):
        result = _normalize_title("God module exceeds 1400 LOC with 50 functions")
        assert "1400" not in result
        assert "50" not in result
        # Numbers are replaced with "N" then lowercased to "n"
        assert "n" in result

    def test_lowercases(self):
        result = _normalize_title("Missing Auth Check on Endpoint")
        assert result == result.lower()

    def test_collapses_whitespace(self):
        result = _normalize_title("too   many    spaces")
        assert "  " not in result


class TestFingerprint:
    def test_same_finding_same_fingerprint(self):
        finding = {
            "title": "SQL injection via repo_url",
            "category": "security",
            "audit_pass": "data_handling",
            "locations": [{"file_path": "backend/api/routes/audit.py", "line_start": 42}],
            "cwe_id": "CWE-89",
        }
        assert fingerprint(finding) == fingerprint(finding)

    def test_different_finding_different_fingerprint(self):
        f1 = {
            "title": "SQL injection via repo_url",
            "category": "security",
            "locations": [{"file_path": "backend/api/routes/audit.py", "line_start": 42}],
        }
        f2 = {
            "title": "Missing auth check on probe endpoint",
            "category": "security",
            "locations": [{"file_path": "backend/api/routes/probe.py", "line_start": 100}],
        }
        assert fingerprint(f1) != fingerprint(f2)

    def test_line_shift_same_fingerprint(self):
        """Lines within same 10-line bucket should produce same fingerprint."""
        f1 = {
            "title": "Missing auth check",
            "category": "security",
            "locations": [{"file_path": "auth.py", "line_start": 42}],
        }
        f2 = {
            "title": "Missing auth check",
            "category": "security",
            "locations": [{"file_path": "auth.py", "line_start": 47}],
        }
        assert fingerprint(f1) == fingerprint(f2)

    def test_loc_count_normalized(self):
        """Different LOC counts in title shouldn't change fingerprint."""
        f1 = {
            "title": "God module: auth.py exceeds 400 LOC",
            "category": "architecture",
            "locations": [{"file_path": "auth.py", "line_start": 1}],
        }
        f2 = {
            "title": "God module: auth.py exceeds 500 LOC",
            "category": "architecture",
            "locations": [{"file_path": "auth.py", "line_start": 1}],
        }
        assert fingerprint(f1) == fingerprint(f2)

    def test_empty_finding(self):
        """Should not crash on minimal finding."""
        result = fingerprint({})
        assert isinstance(result, str)
        assert len(result) == 16

    def test_fingerprint_length(self):
        finding = {"title": "test", "category": "security"}
        assert len(fingerprint(finding)) == 16
