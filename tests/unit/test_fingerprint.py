"""Tests for finding fingerprint generation."""
import os
import tempfile

import pytest
from forge.execution.fingerprint import (
    fingerprint,
    _normalize_title,
    find_match,
    _title_similarity,
    compute_evidence_hash,
    detect_enclosing_symbol,
)


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


class TestTitleSimilarity:
    def test_identical(self):
        assert _title_similarity("Missing auth", "Missing auth") == 1.0

    def test_reordered_words(self):
        sim = _title_similarity("Missing auth check on endpoint", "Endpoint missing auth check")
        assert sim > 0.6

    def test_completely_different(self):
        assert _title_similarity("SQL injection", "Missing error boundary") < 0.3

    def test_empty_strings(self):
        assert _title_similarity("", "") == 0.0
        assert _title_similarity("hello", "") == 0.0

    def test_loc_counts_normalized(self):
        """Different LOC counts should normalize to same tokens."""
        sim = _title_similarity(
            "God module: auth.py exceeds 400 LOC",
            "God module: auth.py exceeds 500 LOC",
        )
        assert sim == 1.0


class TestFindMatch:
    def test_exact_fingerprint_match(self):
        finding = {
            "fingerprint": "abc123",
            "title": "SQL injection",
            "category": "security",
        }
        baseline = {"abc123": {"title": "SQL injection", "category": "security"}}
        assert find_match(finding, baseline) == "abc123"

    def test_similar_title_same_category_and_file_matches(self):
        finding = {
            "title": "Missing auth check on scan endpoint",
            "category": "security",
            "locations": [{"file_path": "routes/scan.py"}],
        }
        baseline = {
            "abc123": {
                "title": "Scan endpoint missing authorization check",
                "category": "security",
                "file_path": "routes/scan.py",
                "cwe_id": "",
                "audit_pass": "",
            }
        }
        result = find_match(finding, baseline)
        assert result == "abc123"

    def test_different_finding_no_match(self):
        finding = {
            "title": "SQL injection",
            "category": "security",
            "locations": [{"file_path": "db.py"}],
        }
        baseline = {
            "abc123": {
                "title": "Missing error boundary",
                "category": "reliability",
                "file_path": "app.tsx",
                "cwe_id": "",
                "audit_pass": "",
            }
        }
        assert find_match(finding, baseline) is None

    def test_same_file_boosts_match(self):
        """Two findings about the same file should match more easily."""
        finding = {
            "title": "Hardcoded secret in config",
            "category": "security",
            "locations": [{"file_path": "config.py"}],
        }
        # Same file + same category + some title overlap => should match
        baseline = {
            "abc123": {
                "title": "Hardcoded secret found in configuration",
                "category": "security",
                "file_path": "config.py",
                "cwe_id": "",
                "audit_pass": "",
            }
        }
        assert find_match(finding, baseline) == "abc123"

    def test_threshold_respected(self):
        """Below-threshold matches return None."""
        finding = {
            "title": "Missing auth check",
            "category": "security",
            "locations": [{"file_path": "auth.py"}],
        }
        baseline = {
            "abc123": {
                "title": "Missing auth check",
                "category": "security",
                "file_path": "auth.py",
                "cwe_id": "",
                "audit_pass": "",
            }
        }
        # Very high threshold should reject
        assert find_match(finding, baseline, threshold=1.1) is None
        # Default threshold should accept
        assert find_match(finding, baseline) == "abc123"

    def test_loc_count_doesnt_affect_match(self):
        """'exceeds 400 LOC' vs 'exceeds 500 LOC' should match."""
        finding = {
            "title": "God module: auth.py exceeds 400 LOC",
            "category": "architecture",
            "locations": [{"file_path": "auth.py"}],
        }
        baseline = {
            "xyz789": {
                "title": "God module: auth.py exceeds 500 LOC",
                "category": "architecture",
                "file_path": "auth.py",
                "cwe_id": "",
                "audit_pass": "",
            }
        }
        assert find_match(finding, baseline) == "xyz789"

    def test_cwe_match_boosts_score(self):
        finding = {
            "title": "Injection vulnerability",
            "category": "security",
            "cwe_id": "CWE-89",
            "locations": [{"file_path": "api.py"}],
        }
        baseline = {
            "abc123": {
                "title": "Injection flaw detected",
                "category": "security",
                "file_path": "api.py",
                "cwe_id": "CWE-89",
                "audit_pass": "",
            }
        }
        assert find_match(finding, baseline) == "abc123"

    def test_best_match_selected(self):
        """When multiple candidates exist, the best match wins."""
        finding = {
            "title": "Missing auth check on scan endpoint",
            "category": "security",
            "locations": [{"file_path": "routes/scan.py"}],
        }
        baseline = {
            "weak": {
                "title": "Unrelated finding about performance",
                "category": "performance",
                "file_path": "other.py",
                "cwe_id": "",
                "audit_pass": "",
            },
            "strong": {
                "title": "Scan endpoint missing authorization",
                "category": "security",
                "file_path": "routes/scan.py",
                "cwe_id": "",
                "audit_pass": "",
            },
        }
        assert find_match(finding, baseline) == "strong"

    def test_empty_baseline_returns_none(self):
        finding = {"title": "Some finding", "category": "security"}
        assert find_match(finding, {}) is None


class TestRuleFamilyFingerprint:
    def test_rule_family_used_when_present(self):
        """When rule_family is set, it should be used instead of title."""
        f1 = {
            "title": "SQL injection via repo_url parameter",
            "rule_family": "sql-injection",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        f2 = {
            "title": "SQL injection through user input concatenation",
            "rule_family": "sql-injection",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        # Different titles but same rule_family => same fingerprint
        assert fingerprint(f1) == fingerprint(f2)

    def test_falls_back_to_title_when_rule_family_empty(self):
        """When rule_family is empty, title is used as before."""
        f1 = {
            "title": "SQL injection via repo_url",
            "rule_family": "",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        f2 = {
            "title": "SQL injection via repo_url",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        # No rule_family in either => both use normalized title
        assert fingerprint(f1) == fingerprint(f2)

    def test_different_rule_family_different_fingerprint(self):
        f1 = {
            "title": "Injection issue",
            "rule_family": "sql-injection",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        f2 = {
            "title": "Injection issue",
            "rule_family": "command-injection",
            "category": "security",
            "locations": [{"file_path": "api.py", "line_start": 42}],
        }
        assert fingerprint(f1) != fingerprint(f2)


class TestComputeEvidenceHash:
    def test_empty_snippet(self):
        assert compute_evidence_hash("") == ""

    def test_whitespace_only(self):
        assert compute_evidence_hash("   ") == ""

    def test_comment_only(self):
        assert compute_evidence_hash("# just a comment") == ""

    def test_normalizes_whitespace(self):
        h1 = compute_evidence_hash("x = 1")
        h2 = compute_evidence_hash("x  =  1")
        assert h1 == h2

    def test_strips_python_comments(self):
        h1 = compute_evidence_hash("x = 1")
        h2 = compute_evidence_hash("x = 1  # set x")
        assert h1 == h2

    def test_strips_js_comments(self):
        h1 = compute_evidence_hash("const x = 1")
        h2 = compute_evidence_hash("const x = 1  // set x")
        assert h1 == h2

    def test_case_insensitive(self):
        h1 = compute_evidence_hash("X = 1")
        h2 = compute_evidence_hash("x = 1")
        assert h1 == h2

    def test_returns_12_chars(self):
        result = compute_evidence_hash("x = 1")
        assert len(result) == 12

    def test_different_code_different_hash(self):
        h1 = compute_evidence_hash("x = 1")
        h2 = compute_evidence_hash("y = 2")
        assert h1 != h2


class TestDetectEnclosingSymbol:
    def test_finds_python_function(self):
        code = "def foo():\n    x = 1\n    return x\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            result = detect_enclosing_symbol(f.name, 2)
            assert result == "foo"
        os.unlink(f.name)

    def test_finds_python_class(self):
        code = "class MyClass:\n    def method(self):\n        pass\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            # Line 3 is inside method, should find "method"
            result = detect_enclosing_symbol(f.name, 3)
            assert result == "method"
        os.unlink(f.name)

    def test_finds_async_def(self):
        code = "async def handler():\n    await do_thing()\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            result = detect_enclosing_symbol(f.name, 2)
            assert result == "handler"
        os.unlink(f.name)

    def test_returns_empty_for_nonexistent_file(self):
        assert detect_enclosing_symbol("/nonexistent/file.py", 1) == ""

    def test_returns_empty_for_empty_path(self):
        assert detect_enclosing_symbol("", 1) == ""

    def test_returns_empty_for_zero_line(self):
        assert detect_enclosing_symbol("/some/file.py", 0) == ""

    def test_returns_empty_when_no_symbol_found(self):
        code = "x = 1\ny = 2\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            result = detect_enclosing_symbol(f.name, 1)
            assert result == ""
        os.unlink(f.name)
