"""Security dimension checks (SEC-001 through SEC-012)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from forge.evaluation.checks import (
    CheckResult,
    iter_source_files,
    is_test_file,
    read_file_safe,
    parse_ast_safe,
    severity_deduction,
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_SECRET_PREFIXES = re.compile(
    r"""(?:"""
    r"""sk-[a-zA-Z0-9]{20,}"""       # OpenAI-style
    r"""|AKIA[0-9A-Z]{16}"""          # AWS access key
    r"""|ghp_[a-zA-Z0-9]{36,}"""      # GitHub PAT
    r"""|sk_live_[a-zA-Z0-9]{20,}"""  # Stripe live key
    r"""|AIza[0-9A-Za-z\-_]{35}"""    # Google API key
    r""")""",
    re.VERBOSE,
)

_HARDCODED_PASSWORD = re.compile(
    r"""(?:password|passwd|api_key|apikey|secret|token)\s*=\s*["'][^"']{4,}["']""",
    re.IGNORECASE,
)

_SQL_CONCAT = re.compile(
    r"""(?:"""
    r"""\.(?:execute|raw|query)\s*\(\s*f["']"""
    r"""|\.(?:execute|raw|query)\s*\(\s*["'].*?\s*\+"""
    r"""|["']SELECT\s.*?["']\s*\+"""
    r"""|["']INSERT\s.*?["']\s*\+"""
    r"""|["']UPDATE\s.*?["']\s*\+"""
    r"""|["']DELETE\s.*?["']\s*\+"""
    r""")""",
    re.IGNORECASE | re.VERBOSE,
)

_CMD_INJECTION_FUNCS = {"system", "popen"}
_SUBPROCESS_FUNCS = {"call", "run", "Popen"}

_ROUTE_DECORATOR = re.compile(
    r"""@(?:app|router|api)\.\s*(?:get|post|put|delete|patch|options|head|route)\s*\(""",
    re.IGNORECASE,
)

_AUTH_INDICATOR = re.compile(
    r"""(?:Depends\s*\(|authenticate|login_required|@auth|@require_auth|@permission|IsAuthenticated|jwt_required)""",
    re.IGNORECASE,
)

_INSECURE_CRYPTO = re.compile(
    r"""(?:hashlib\.md5\s*\(|hashlib\.sha1\s*\()""",
)
_INSECURE_CRYPTO_ALGOS = re.compile(
    r"""(?:\bDES\b|\bECB\b|\bRC4\b)""",
)

_DEBUG_MODE = re.compile(
    r"""(?:DEBUG\s*=\s*True|debug\s*:\s*true|app\.debug\s*=\s*True)""",
    re.IGNORECASE,
)

_CORS_WILDCARD = re.compile(
    r"""(?:allow_origins\s*=\s*\[\s*["']\*["']\s*\]"""
    r"""|["']Access-Control-Allow-Origin["']\s*:\s*["']\*["']"""
    r"""|cors\s*\(\s*origin\s*:\s*["']\*["']\))""",
    re.IGNORECASE,
)

_HTTP_HARDCODED = re.compile(r"""http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)""")

_VERBOSE_ERROR = re.compile(
    r"""(?:traceback\.format_exc\s*\(\)|str\s*\(\s*e\s*\)|repr\s*\(\s*e\s*\))""",
)
_RESPONSE_RETURN = re.compile(
    r"""(?:return|Response\s*\(|JSONResponse\s*\(|jsonify\s*\()""",
)

_PII_VARS = re.compile(
    r"""\b(?:password|token|secret|ssn|credit_card|api_key)\b""", re.IGNORECASE
)
_LOG_CALL = re.compile(
    r"""(?:log(?:ging)?\.(?:info|debug|warning|error|critical)\s*\(|print\s*\()"""
)

_INSECURE_DEFAULT = re.compile(
    r"""(?:password|passwd)\s*=\s*["'](?:password|admin|changeme|123456)["']"""
    r"""|secret\s*=\s*["'](?:secret|changeme)["']""",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_sec001(repo_path: str) -> CheckResult:
    """SEC-001: Hardcoded secrets/API keys."""
    locations = []
    skip_names = {".env.example", ".env.sample", "package-lock.json", "yarn.lock", "poetry.lock"}
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts", ".jsx", ".tsx")):
        if path.name in skip_names or is_test_file(path):
            continue
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if line.strip().startswith("#") or line.strip().startswith("//"):
                continue
            match = _SECRET_PREFIXES.search(line) or _HARDCODED_PASSWORD.search(line)
            if match:
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-001",
        name="Hardcoded secrets",
        passed=passed,
        severity="critical",
        deduction=0 if passed else severity_deduction("critical"),
        locations=locations[:5],
        details=f"Found {len(locations)} potential hardcoded secret(s)." if locations else "",
        stride="information_disclosure",
        asvs_ref="V13.1.3",
        fix_guidance="Move secrets to environment variables or a secrets manager and rotate any exposed credentials." if not passed else "",
    )


def _check_sec002(repo_path: str) -> CheckResult:
    """SEC-002: SQL string concatenation."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _SQL_CONCAT.search(line):
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-002",
        name="SQL string concatenation",
        passed=passed,
        severity="critical",
        deduction=0 if passed else severity_deduction("critical"),
        locations=locations[:5],
        details=f"Found {len(locations)} SQL injection risk(s)." if locations else "",
        stride="tampering",
        asvs_ref="V1.5.3",
        fix_guidance="Replace string concatenation with parameterized queries or ORM methods." if not passed else "",
    )


def _check_sec003(repo_path: str) -> CheckResult:
    """SEC-003: Command injection."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        tree = parse_ast_safe(content, str(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # os.system / os.popen
            if (isinstance(func, ast.Attribute)
                and func.attr in _CMD_INJECTION_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"):
                if node.args and not isinstance(node.args[0], ast.Constant):
                    locations.append({
                        "file": str(path),
                        "line": node.lineno,
                        "snippet": f"os.{func.attr}() with dynamic argument",
                    })
            # subprocess.call/run/Popen(shell=True)
            if (isinstance(func, ast.Attribute)
                and func.attr in _SUBPROCESS_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
                for kw in node.keywords:
                    if (kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True):
                        if node.args and not isinstance(node.args[0], ast.Constant):
                            locations.append({
                                "file": str(path),
                                "line": node.lineno,
                                "snippet": f"subprocess.{func.attr}(shell=True) with dynamic argument",
                            })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-003",
        name="Command injection",
        passed=passed,
        severity="critical",
        deduction=0 if passed else severity_deduction("critical"),
        locations=locations[:5],
        details=f"Found {len(locations)} command injection risk(s)." if locations else "",
        stride="elevation_of_privilege",
        asvs_ref="",
        fix_guidance="Use subprocess.run() with list arguments instead of shell=True, and validate all user inputs." if not passed else "",
    )


def _check_sec004(repo_path: str) -> CheckResult:
    """SEC-004: Missing auth on routes."""
    total_routes = 0
    unprotected = 0
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _ROUTE_DECORATOR.search(line):
                total_routes += 1
                # Check surrounding context (5 lines after decorator)
                context = "\n".join(lines[i:i + 7])
                if not _AUTH_INDICATOR.search(context):
                    unprotected += 1
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    if total_routes == 0:
        passed = True
    else:
        passed = (unprotected / total_routes) <= 0.5
    return CheckResult(
        check_id="SEC-004",
        name="Missing auth on routes",
        passed=passed,
        severity="high",
        deduction=0 if passed else severity_deduction("high"),
        locations=locations[:5] if not passed else [],
        details=f"{unprotected}/{total_routes} routes lack auth." if not passed else "",
        stride="spoofing",
        asvs_ref="V6.2.1",
        fix_guidance="Add authentication middleware or Depends() to all non-public route handlers." if not passed else "",
    )


def _check_sec005(repo_path: str) -> CheckResult:
    """SEC-005: Insecure crypto."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _INSECURE_CRYPTO.search(line):
                # Check if near password context
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
            elif _INSECURE_CRYPTO_ALGOS.search(line) and "import" in line.lower():
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-005",
        name="Insecure crypto",
        passed=passed,
        severity="high",
        deduction=0 if passed else severity_deduction("high"),
        locations=locations[:5],
        details=f"Found {len(locations)} insecure crypto usage(s)." if locations else "",
        stride="information_disclosure",
        asvs_ref="V11.1.1",
        fix_guidance="Replace MD5/SHA1 with bcrypt, scrypt, or Argon2 for password hashing." if not passed else "",
    )


def _check_sec006(repo_path: str) -> CheckResult:
    """SEC-006: Debug mode in production config."""
    locations = []
    config_patterns = {"settings", "config", ".env", "application"}
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts", ".yml", ".yaml")):
        if is_test_file(path):
            continue
        name_lower = path.stem.lower()
        if not any(p in name_lower for p in config_patterns):
            continue
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _DEBUG_MODE.search(line):
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-006",
        name="Debug mode in production",
        passed=passed,
        severity="high",
        deduction=0 if passed else severity_deduction("high"),
        locations=locations[:5],
        details=f"Found {len(locations)} debug mode flag(s) in config." if locations else "",
        stride="information_disclosure",
        asvs_ref="V13.4.1",
        fix_guidance="Set DEBUG=False in production config and control via environment variables." if not passed else "",
    )


def _check_sec007(repo_path: str) -> CheckResult:
    """SEC-007: CORS wildcard origin."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _CORS_WILDCARD.search(line):
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-007",
        name="CORS wildcard origin",
        passed=passed,
        severity="high",
        deduction=0 if passed else severity_deduction("high"),
        locations=locations[:5],
        details=f"Found {len(locations)} CORS wildcard origin(s)." if locations else "",
        stride="spoofing",
        asvs_ref="",
        fix_guidance="Replace CORS wildcard '*' with specific allowed origins." if not passed else "",
    )


def _check_sec008(repo_path: str) -> CheckResult:
    """SEC-008: Missing HTTPS enforcement."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts", ".yml", ".yaml")):
        if is_test_file(path):
            continue
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _HTTP_HARDCODED.search(line):
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    # Also check deployment configs
    deploy_files = ["Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                     "nginx.conf", "Procfile", "compose.yml"]
    for name in deploy_files:
        p = Path(repo_path) / name
        if p.exists():
            content = read_file_safe(p)
            for i, line in enumerate(content.splitlines(), 1):
                if _HTTP_HARDCODED.search(line):
                    locations.append({
                        "file": str(p),
                        "line": i,
                        "snippet": line.strip()[:120],
                    })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-008",
        name="Missing HTTPS enforcement",
        passed=passed,
        severity="medium",
        deduction=0 if passed else severity_deduction("medium"),
        locations=locations[:5],
        details=f"Found {len(locations)} hardcoded HTTP URL(s)." if locations else "",
        stride="",
        asvs_ref="",
        fix_guidance="Replace hardcoded http:// URLs with https:// and configure TLS at the load balancer." if not passed else "",
    )


def _check_sec009(repo_path: str) -> CheckResult:
    """SEC-009: Verbose error exposure."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _VERBOSE_ERROR.search(line):
                # Check if near a response return (within 3 lines)
                context = "\n".join(lines[max(0, i - 3):i + 4])
                if _RESPONSE_RETURN.search(context):
                    locations.append({
                        "file": str(path),
                        "line": i + 1,
                        "snippet": line.strip()[:120],
                    })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-009",
        name="Verbose error exposure",
        passed=passed,
        severity="medium",
        deduction=0 if passed else severity_deduction("medium"),
        locations=locations[:5],
        details=f"Found {len(locations)} verbose error exposure(s)." if locations else "",
        stride="information_disclosure",
        asvs_ref="V1.7.2",
        fix_guidance="Return generic error messages to clients and log detailed errors server-side only." if not passed else "",
    )


def _check_sec010(repo_path: str) -> CheckResult:
    """SEC-010: PII in logs."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _LOG_CALL.search(line) and _PII_VARS.search(line):
                locations.append({
                    "file": str(path),
                    "line": i + 1,
                    "snippet": line.strip()[:120],
                })
            elif _PII_VARS.search(line):
                # Check adjacent lines for log calls
                context = "\n".join(lines[max(0, i - 3):i + 4])
                if _LOG_CALL.search(context) and _PII_VARS.search(line):
                    # Only flag if the PII var is in the log context
                    pass  # Already handled above or too noisy
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-010",
        name="PII in logs",
        passed=passed,
        severity="medium",
        deduction=0 if passed else severity_deduction("medium"),
        locations=locations[:5],
        details=f"Found {len(locations)} PII logging risk(s)." if locations else "",
        stride="information_disclosure",
        asvs_ref="V14.3.3",
        fix_guidance="Remove sensitive data from log statements and use structured logging with field redaction." if not passed else "",
    )


def _check_sec011(repo_path: str) -> CheckResult:
    """SEC-011: Missing input validation on route handlers."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py",)):
        content = read_file_safe(path)
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if _ROUTE_DECORATOR.search(line):
                # Look at function signature (next few lines)
                func_lines = "\n".join(lines[i:i + 10])
                # Check for raw params without validation
                if re.search(r"def\s+\w+\s*\([^)]*\b(?:request)\b", func_lines):
                    # Has request param — check for body/query validation
                    if not re.search(
                        r"(?:Pydantic|BaseModel|Body\s*\(|Query\s*\(|Path\s*\(|Depends\s*\(|Schema)",
                        func_lines,
                    ):
                        locations.append({
                            "file": str(path),
                            "line": i + 1,
                            "snippet": line.strip()[:120],
                        })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-011",
        name="Missing input validation",
        passed=passed,
        severity="medium",
        deduction=0 if passed else severity_deduction("medium"),
        locations=locations[:5],
        details=f"Found {len(locations)} route(s) without input validation." if locations else "",
        stride="tampering",
        asvs_ref="",
        fix_guidance="Add Pydantic models or framework validators to all route parameters and request bodies." if not passed else "",
    )


def _check_sec012(repo_path: str) -> CheckResult:
    """SEC-012: Insecure default config."""
    locations = []
    for path in iter_source_files(repo_path, extensions=(".py", ".js", ".ts")):
        content = read_file_safe(path)
        for i, line in enumerate(content.splitlines(), 1):
            if _INSECURE_DEFAULT.search(line):
                locations.append({
                    "file": str(path),
                    "line": i,
                    "snippet": line.strip()[:120],
                })
    passed = len(locations) == 0
    return CheckResult(
        check_id="SEC-012",
        name="Insecure default config",
        passed=passed,
        severity="medium",
        deduction=0 if passed else severity_deduction("medium"),
        locations=locations[:5],
        details=f"Found {len(locations)} insecure default(s)." if locations else "",
        stride="",
        asvs_ref="V13.4.2",
        fix_guidance="Replace insecure default credentials with configuration that requires explicit setup." if not passed else "",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_security_checks(repo_path: str) -> list[CheckResult]:
    """Run all 12 security checks against the repository."""
    return [
        _check_sec001(repo_path),
        _check_sec002(repo_path),
        _check_sec003(repo_path),
        _check_sec004(repo_path),
        _check_sec005(repo_path),
        _check_sec006(repo_path),
        _check_sec007(repo_path),
        _check_sec008(repo_path),
        _check_sec009(repo_path),
        _check_sec010(repo_path),
        _check_sec011(repo_path),
        _check_sec012(repo_path),
    ]
