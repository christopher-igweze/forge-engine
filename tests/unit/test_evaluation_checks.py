"""Tests for FORGE v3 deterministic evaluation checks."""

import tempfile
from pathlib import Path

import pytest

from forge.evaluation.checks import (
    CheckResult,
    run_security_checks,
    run_reliability_checks,
    run_maintainability_checks,
    run_test_quality_checks,
    run_performance_checks,
    run_documentation_checks,
    run_operations_checks,
)


def _make_repo(files: dict[str, str]) -> str:
    """Create a temp repo with given files. Returns path."""
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


# ── Security Checks ──────────────────────────────────────────────────


class TestSEC001:
    def test_detects_hardcoded_secret(self):
        repo = _make_repo({"app.py": 'API_KEY = "sk-1234abcdefghijklmnopqrst"'})
        results = run_security_checks(repo)
        sec001 = [r for r in results if r.check_id == "SEC-001"][0]
        assert not sec001.passed
        assert sec001.deduction < 0

    def test_passes_clean_file(self):
        repo = _make_repo({"app.py": 'import os\nAPI_KEY = os.getenv("KEY")'})
        results = run_security_checks(repo)
        sec001 = [r for r in results if r.check_id == "SEC-001"][0]
        assert sec001.passed


class TestSEC002:
    def test_detects_sql_concat(self):
        repo = _make_repo({
            "db.py": 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")'
        })
        results = run_security_checks(repo)
        sec002 = [r for r in results if r.check_id == "SEC-002"][0]
        assert not sec002.passed

    def test_passes_parameterized(self):
        repo = _make_repo({
            "db.py": 'cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))'
        })
        results = run_security_checks(repo)
        sec002 = [r for r in results if r.check_id == "SEC-002"][0]
        assert sec002.passed


class TestSEC004:
    def test_detects_missing_auth(self):
        repo = _make_repo({
            "api.py": (
                '@app.get("/users")\n'
                'def get_users():\n'
                '    return []\n'
            ),
        })
        results = run_security_checks(repo)
        sec004 = [r for r in results if r.check_id == "SEC-004"][0]
        assert not sec004.passed

    def test_passes_with_auth(self):
        repo = _make_repo({
            "api.py": (
                'from fastapi import Depends\n'
                '@app.get("/users")\n'
                'def get_users(user=Depends(get_current_user)):\n'
                '    return []\n'
            ),
        })
        results = run_security_checks(repo)
        sec004 = [r for r in results if r.check_id == "SEC-004"][0]
        assert sec004.passed


class TestSEC006:
    def test_detects_debug_mode(self):
        repo = _make_repo({"config.py": "DEBUG = True\nSECRET_KEY = 'abc'"})
        results = run_security_checks(repo)
        sec006 = [r for r in results if r.check_id == "SEC-006"][0]
        assert not sec006.passed

    def test_passes_no_debug(self):
        repo = _make_repo({"config.py": "DEBUG = False"})
        results = run_security_checks(repo)
        sec006 = [r for r in results if r.check_id == "SEC-006"][0]
        assert sec006.passed


class TestSEC007:
    def test_detects_cors_wildcard(self):
        repo = _make_repo({
            "main.py": 'app.add_middleware(CORSMiddleware, allow_origins=["*"])'
        })
        results = run_security_checks(repo)
        sec007 = [r for r in results if r.check_id == "SEC-007"][0]
        assert not sec007.passed

    def test_passes_specific_origin(self):
        repo = _make_repo({
            "main.py": 'app.add_middleware(CORSMiddleware, allow_origins=["https://example.com"])'
        })
        results = run_security_checks(repo)
        sec007 = [r for r in results if r.check_id == "SEC-007"][0]
        assert sec007.passed


# ── Reliability Checks ───────────────────────────────────────────────


class TestREL002:
    def test_detects_no_health_check(self):
        repo = _make_repo({"main.py": "app = FastAPI()"})
        results = run_reliability_checks(repo)
        rel002 = [r for r in results if r.check_id == "REL-002"][0]
        assert not rel002.passed

    def test_passes_with_health(self):
        repo = _make_repo({
            "main.py": '@app.get("/health")\ndef health():\n    return {"ok": True}'
        })
        results = run_reliability_checks(repo)
        rel002 = [r for r in results if r.check_id == "REL-002"][0]
        assert rel002.passed


class TestREL004:
    def test_detects_bare_except(self):
        repo = _make_repo({
            "handler.py": (
                "try:\n"
                "    do_thing()\n"
                "except:\n"
                "    pass\n"
            ),
        })
        results = run_reliability_checks(repo)
        rel004 = [r for r in results if r.check_id == "REL-004"][0]
        assert not rel004.passed

    def test_passes_with_logging(self):
        repo = _make_repo({
            "handler.py": (
                "try:\n"
                "    do_thing()\n"
                "except Exception as e:\n"
                "    logger.error(e)\n"
            ),
        })
        results = run_reliability_checks(repo)
        rel004 = [r for r in results if r.check_id == "REL-004"][0]
        assert rel004.passed


# ── Maintainability Checks ───────────────────────────────────────────


class TestMNT001:
    def test_detects_god_class(self):
        # Create a class with 600 lines
        lines = ["class GodClass:"] + [f"    x{i} = {i}" for i in range(600)]
        repo = _make_repo({"big.py": "\n".join(lines)})
        results = run_maintainability_checks(repo)
        mnt001 = [r for r in results if r.check_id == "MNT-001"][0]
        assert not mnt001.passed

    def test_passes_small_class(self):
        lines = ["class SmallClass:"] + [f"    x{i} = {i}" for i in range(10)]
        repo = _make_repo({"small.py": "\n".join(lines)})
        results = run_maintainability_checks(repo)
        mnt001 = [r for r in results if r.check_id == "MNT-001"][0]
        assert mnt001.passed


class TestMNT002:
    def test_detects_high_complexity(self):
        # Build a function with 25+ branches
        branches = "\n".join(f"    if x == {i}:\n        pass" for i in range(25))
        code = f"def complex_func(x):\n{branches}"
        repo = _make_repo({"complex.py": code})
        results = run_maintainability_checks(repo)
        mnt002 = [r for r in results if r.check_id == "MNT-002"][0]
        assert not mnt002.passed

    def test_passes_simple_function(self):
        repo = _make_repo({"simple.py": "def add(a, b):\n    return a + b"})
        results = run_maintainability_checks(repo)
        mnt002 = [r for r in results if r.check_id == "MNT-002"][0]
        assert mnt002.passed


# ── Test Quality Checks ──────────────────────────────────────────────


class TestTST001:
    def test_detects_no_tests(self):
        repo = _make_repo({"app.py": "print('hello')"})
        results = run_test_quality_checks(repo)
        tst001 = [r for r in results if r.check_id == "TST-001"][0]
        assert not tst001.passed

    def test_passes_with_tests(self):
        repo = _make_repo({
            "app.py": "def add(a, b): return a + b",
            "tests/test_app.py": "def test_add():\n    assert add(1, 2) == 3",
        })
        results = run_test_quality_checks(repo)
        tst001 = [r for r in results if r.check_id == "TST-001"][0]
        assert tst001.passed


class TestTST003:
    def test_detects_empty_test(self):
        repo = _make_repo({
            "tests/test_empty.py": "def test_foo():\n    pass\n",
        })
        results = run_test_quality_checks(repo)
        tst003 = [r for r in results if r.check_id == "TST-003"][0]
        assert not tst003.passed

    def test_passes_with_assertion(self):
        repo = _make_repo({
            "tests/test_good.py": "def test_foo():\n    assert 1 == 1\n",
        })
        results = run_test_quality_checks(repo)
        tst003 = [r for r in results if r.check_id == "TST-003"][0]
        assert tst003.passed


# ── Documentation Checks ─────────────────────────────────────────────


class TestDOC001:
    def test_detects_no_readme(self):
        repo = _make_repo({"app.py": "pass"})
        results = run_documentation_checks(repo)
        doc001 = [r for r in results if r.check_id == "DOC-001"][0]
        assert not doc001.passed

    def test_passes_with_readme(self):
        repo = _make_repo({"README.md": "# My Project\nSome content here."})
        results = run_documentation_checks(repo)
        doc001 = [r for r in results if r.check_id == "DOC-001"][0]
        assert doc001.passed


# ── Operations Checks ────────────────────────────────────────────────


class TestOPS001:
    def test_detects_no_ci(self):
        repo = _make_repo({"app.py": "pass"})
        results = run_operations_checks(repo)
        ops001 = [r for r in results if r.check_id == "OPS-001"][0]
        assert not ops001.passed

    def test_passes_with_github_actions(self):
        repo = _make_repo({
            ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest",
        })
        results = run_operations_checks(repo)
        ops001 = [r for r in results if r.check_id == "OPS-001"][0]
        assert ops001.passed


# ── Performance Checks ───────────────────────────────────────────────


class TestPRF005:
    def test_detects_no_caching(self):
        repo = _make_repo({"app.py": "def compute(): return 42"})
        results = run_performance_checks(repo)
        prf005 = [r for r in results if r.check_id == "PRF-005"][0]
        assert not prf005.passed

    def test_passes_with_cache(self):
        repo = _make_repo({
            "app.py": "from functools import lru_cache\n@lru_cache\ndef compute(): return 42"
        })
        results = run_performance_checks(repo)
        prf005 = [r for r in results if r.check_id == "PRF-005"][0]
        assert prf005.passed
