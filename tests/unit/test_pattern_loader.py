"""Tests for vulnerability pattern loader."""

import pytest

from forge.patterns.loader import PatternLibrary
from forge.patterns.schema import VulnerabilityPattern


class TestPatternLibraryLoadDefault:
    def test_loads_curated_patterns(self):
        lib = PatternLibrary.load_default()
        assert len(lib) == 3

    def test_pattern_ids(self):
        lib = PatternLibrary.load_default()
        ids = {p.id for p in lib}
        assert ids == {"VP-001", "VP-002", "VP-003"}

    def test_all_are_security_category(self):
        lib = PatternLibrary.load_default()
        for p in lib:
            assert p.category == "security"


class TestPatternLibraryGet:
    def test_get_by_id(self):
        lib = PatternLibrary.load_default()
        p = lib.get("VP-001")
        assert p is not None
        assert p.name == "Client-writable server-authority columns"

    def test_get_missing_returns_none(self):
        lib = PatternLibrary.load_default()
        assert lib.get("VP-999") is None

    def test_get_by_slug(self):
        lib = PatternLibrary.load_default()
        p = lib.get_by_slug("client-writable-server-authority")
        assert p is not None
        assert p.id == "VP-001"

    def test_get_by_slug_missing(self):
        lib = PatternLibrary.load_default()
        assert lib.get_by_slug("nonexistent") is None


class TestPatternLibraryByCategory:
    def test_filter_security(self):
        lib = PatternLibrary.load_default()
        security = lib.by_category("security")
        assert len(security) == 3

    def test_filter_nonexistent_category(self):
        lib = PatternLibrary.load_default()
        assert lib.by_category("performance") == []


class TestPatternLibraryFromDirectory:
    def test_empty_directory(self, tmp_path):
        lib = PatternLibrary.load_from_directory(tmp_path)
        assert len(lib) == 0

    def test_skips_invalid_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("not: valid: yaml: {{")
        lib = PatternLibrary.load_from_directory(tmp_path)
        assert len(lib) == 0

    def test_skips_yaml_without_id(self, tmp_path):
        (tmp_path / "no_id.yaml").write_text("name: test\nslug: test\n")
        lib = PatternLibrary.load_from_directory(tmp_path)
        assert len(lib) == 0

    def test_loads_valid_yaml(self, tmp_path):
        (tmp_path / "test.yaml").write_text(
            "id: VP-TEST\nname: Test\nslug: test\n"
        )
        lib = PatternLibrary.load_from_directory(tmp_path)
        assert len(lib) == 1
        assert lib.get("VP-TEST") is not None

    def test_loads_nested_directories(self, tmp_path):
        nested = tmp_path / "sub" / "dir"
        nested.mkdir(parents=True)
        (nested / "p.yaml").write_text("id: VP-NESTED\nname: Nested\nslug: nested\n")
        lib = PatternLibrary.load_from_directory(tmp_path)
        assert len(lib) == 1


class TestPatternLibraryIteration:
    def test_iter(self):
        lib = PatternLibrary.load_default()
        patterns = list(lib)
        assert len(patterns) == 3
        assert all(isinstance(p, VulnerabilityPattern) for p in patterns)

    def test_bool_true(self):
        lib = PatternLibrary.load_default()
        assert bool(lib) is True

    def test_bool_false(self):
        lib = PatternLibrary()
        assert bool(lib) is False


class TestPatternLibraryConstructor:
    def test_from_list(self):
        patterns = [
            VulnerabilityPattern(id="VP-A", name="A", slug="a"),
            VulnerabilityPattern(id="VP-B", name="B", slug="b"),
        ]
        lib = PatternLibrary(patterns)
        assert len(lib) == 2
        assert lib.get("VP-A") is not None

    def test_empty(self):
        lib = PatternLibrary()
        assert len(lib) == 0
        assert lib.all() == []
