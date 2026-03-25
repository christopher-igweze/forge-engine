"""Generate remediation items from failed deterministic checks.

Deterministic checks have known fix patterns — no LLM needed.
Items are grouped by dimension and merged into the remediation plan.
"""
from __future__ import annotations

from forge.evaluation.checks import CheckResult


# Fix templates keyed by check_id prefix (dimension) or specific check_id.
# Each template has: approach, acceptance_criteria, estimated_files, group
_FIX_TEMPLATES: dict[str, dict] = {
    # ── Security ──
    "SEC-001": {
        "approach": "Remove hardcoded secrets from source code. Use environment variables or a secrets manager (e.g., dotenv, AWS Secrets Manager). Add the secret patterns to .gitignore and rotate any exposed credentials.",
        "acceptance_criteria": ["No hardcoded secrets in source code", "Secrets loaded from environment variables", "Exposed credentials rotated"],
        "group": "secrets-hardening",
    },
    "SEC-002": {
        "approach": "Replace string concatenation in SQL queries with parameterized queries or ORM methods. Use query parameters (? or %s placeholders) instead of f-strings or .format().",
        "acceptance_criteria": ["All SQL queries use parameterized statements", "No string concatenation in database calls"],
        "group": "injection-prevention",
    },
    "SEC-003": {
        "approach": "Replace os.system() and subprocess calls with shell=True with subprocess.run() using list arguments. Validate and sanitize all user inputs before passing to system commands.",
        "acceptance_criteria": ["No shell=True in subprocess calls", "All system commands use list-form arguments", "User inputs validated before command construction"],
        "group": "injection-prevention",
    },
    "SEC-004": {
        "approach": "Add authentication middleware or decorators to all route handlers. Ensure every endpoint requires valid credentials unless explicitly marked as public.",
        "acceptance_criteria": ["All non-public routes require authentication", "Auth middleware applied consistently"],
        "group": "auth-hardening",
    },
    "SEC-005": {
        "approach": "Replace MD5/SHA1 password hashing with bcrypt, scrypt, or Argon2. Use a library like passlib or bcrypt for secure password storage.",
        "acceptance_criteria": ["No MD5/SHA1 used for password hashing", "Passwords hashed with bcrypt/scrypt/Argon2"],
        "group": "crypto-hardening",
    },
    "SEC-006": {
        "approach": "Set DEBUG=False in production configuration. Use environment-specific config files or environment variables to control debug mode.",
        "acceptance_criteria": ["DEBUG=False in production config", "Debug mode controlled by environment variable"],
        "group": "config-hardening",
    },
    "SEC-007": {
        "approach": "Replace CORS wildcard origin ('*') with specific allowed origins. List only the domains that need cross-origin access.",
        "acceptance_criteria": ["CORS allow_origins lists specific domains", "No wildcard '*' in production CORS config"],
        "group": "config-hardening",
    },
    "SEC-008": {
        "approach": "Configure HTTPS/TLS in deployment manifests. Add TLS termination at the load balancer or reverse proxy level.",
        "acceptance_criteria": ["HTTPS enforced in deployment config", "HTTP redirects to HTTPS"],
        "group": "config-hardening",
    },
    "SEC-009": {
        "approach": "Implement error handling middleware that returns generic error messages to clients. Log detailed errors server-side only.",
        "acceptance_criteria": ["No raw exceptions exposed in HTTP responses", "Generic error messages returned to clients", "Detailed errors logged server-side"],
        "group": "error-handling",
    },
    "SEC-010": {
        "approach": "Remove sensitive data (passwords, tokens, SSNs) from log statements. Use structured logging with field redaction for sensitive values.",
        "acceptance_criteria": ["No PII or secrets adjacent to log/print statements", "Sensitive fields redacted in logs"],
        "group": "data-protection",
    },
    "SEC-011": {
        "approach": "Add input validation (Pydantic models, JSON Schema, or framework validators) to all route parameters and request bodies.",
        "acceptance_criteria": ["All endpoint inputs validated with schema", "Invalid inputs rejected with clear error messages"],
        "group": "input-validation",
    },
    "SEC-012": {
        "approach": "Replace insecure default values (password='password', secret='changeme') with proper configuration that requires explicit setup.",
        "acceptance_criteria": ["No insecure default credentials in code", "Configuration requires explicit values"],
        "group": "config-hardening",
    },
    # ── Reliability ──
    "REL-001": {
        "approach": "Add try/except blocks at API route boundaries. Return structured error responses (JSON with error code and message) instead of letting exceptions propagate.",
        "acceptance_criteria": ["All API endpoints have error handling", "Structured error responses returned"],
        "group": "error-handling",
    },
    "REL-002": {
        "approach": "Add a /health or /healthz endpoint that returns 200 OK when the service is ready. Include basic dependency checks (database, cache).",
        "acceptance_criteria": ["Health check endpoint exists and returns 200", "Checks key dependencies (DB, cache)"],
        "group": "observability",
    },
    "REL-003": {
        "approach": "Register a SIGTERM signal handler that initiates graceful shutdown — stop accepting new requests, finish in-flight work, close connections, then exit.",
        "acceptance_criteria": ["SIGTERM handler registered", "Graceful shutdown completes in-flight requests"],
        "group": "reliability",
    },
    "REL-004": {
        "approach": "Replace bare 'except: pass' blocks with specific exception handling. At minimum, log the exception before continuing.",
        "acceptance_criteria": ["No bare except:pass blocks", "All caught exceptions are logged"],
        "group": "error-handling",
    },
    "REL-005": {
        "approach": "Add timeout= parameter to all HTTP client calls (requests.get, httpx.get, etc.). Use reasonable defaults (5-30s depending on the endpoint).",
        "acceptance_criteria": ["All HTTP client calls have explicit timeout", "Timeouts appropriate for each endpoint"],
        "group": "reliability",
    },
    "REL-006": {
        "approach": "Add retry logic with exponential backoff to external API calls. Use a retry decorator (tenacity, backoff) or implement retry loops.",
        "acceptance_criteria": ["External calls have retry logic", "Exponential backoff prevents thundering herd"],
        "group": "reliability",
    },
    "REL-007": {
        "approach": "Configure connection pooling for database connections. Set pool_size, max_overflow, and pool_recycle parameters.",
        "acceptance_criteria": ["Database connection pool configured", "Pool parameters tuned for expected load"],
        "group": "reliability",
    },
    # ── Maintainability ──
    "MNT-001": {
        "approach": "Split large classes (>500 lines) into smaller, focused classes. Extract responsibilities into separate modules using composition.",
        "acceptance_criteria": ["No class exceeds 500 lines", "Each class has a single responsibility"],
        "group": "refactoring",
    },
    "MNT-002": {
        "approach": "Reduce cyclomatic complexity (>20) by extracting helper functions, using early returns, and simplifying conditional logic.",
        "acceptance_criteria": ["No function has cyclomatic complexity >20", "Complex logic extracted into named helpers"],
        "group": "refactoring",
    },
    "MNT-003": {
        "approach": "Reduce nesting depth (>4 levels) using guard clauses (early returns), extracting inner blocks into functions, or inverting conditions.",
        "acceptance_criteria": ["No code block nested >4 levels deep", "Guard clauses used for preconditions"],
        "group": "refactoring",
    },
    "MNT-004": {
        "approach": "Extract duplicated code blocks (>20 identical lines) into shared functions or modules. Use the DRY principle.",
        "acceptance_criteria": ["No significant code duplication", "Shared logic extracted to reusable functions"],
        "group": "refactoring",
    },
    "MNT-005": {
        "approach": "Break circular imports by extracting shared types into a separate module, using dependency injection, or restructuring the import graph.",
        "acceptance_criteria": ["No circular import cycles", "Clear module dependency hierarchy"],
        "group": "refactoring",
    },
    # ── Test Quality ──
    "TST-001": {
        "approach": "Create test files for the project. Add at least unit tests for core business logic. Use pytest (Python), jest (JS/TS), or the framework's built-in test runner.",
        "acceptance_criteria": ["Test files exist", "Core business logic has unit tests"],
        "group": "testing",
    },
    "TST-002": {
        "approach": "Add integration tests alongside existing unit tests. Test API endpoints, database operations, and external service interactions.",
        "acceptance_criteria": ["Both unit and integration tests exist", "Integration tests cover key workflows"],
        "group": "testing",
    },
    "TST-003": {
        "approach": "Add assertions to empty test functions. Each test should verify specific behavior with assert/expect statements.",
        "acceptance_criteria": ["No test functions without assertions", "Each test verifies specific behavior"],
        "group": "testing",
    },
    "TST-004": {
        "approach": "Add tests for critical paths: authentication, data mutation, payment processing, and core business workflows.",
        "acceptance_criteria": ["Auth flow has dedicated tests", "Critical data mutations tested", "Core workflows have end-to-end tests"],
        "group": "testing",
    },
    "TST-005": {
        "approach": "Increase test coverage by adding tests for untested modules. Aim for at least a 0.3 test-to-source file ratio.",
        "acceptance_criteria": ["Test-to-source ratio >= 0.3", "Major modules have corresponding test files"],
        "group": "testing",
    },
    "TST-006": {
        "approach": "Add test configuration (pytest.ini, jest.config.js, etc.) to standardize test discovery, output format, and coverage settings.",
        "acceptance_criteria": ["Test configuration file exists", "Test runner configured with sensible defaults"],
        "group": "testing",
    },
    "TST-007": {
        "approach": "Set a minimum code coverage threshold (>=60%) in the test configuration. Add coverage reporting to CI/CD pipeline.",
        "acceptance_criteria": ["Coverage threshold configured (>=60%)", "Coverage reports generated on test runs"],
        "group": "testing",
    },
    # ── Performance ──
    "PRF-001": {
        "approach": "Replace N+1 query patterns (DB call inside a loop) with batch queries, JOINs, or eager loading (select_related/prefetch_related).",
        "acceptance_criteria": ["No DB calls inside loops", "Batch queries or JOINs used instead"],
        "group": "performance",
    },
    "PRF-002": {
        "approach": "Add LIMIT clauses to all SELECT queries. Use pagination for list endpoints to prevent unbounded result sets.",
        "acceptance_criteria": ["All queries have LIMIT clause", "List endpoints use pagination"],
        "group": "performance",
    },
    "PRF-003": {
        "approach": "Add offset/limit or cursor-based pagination parameters to list API endpoints.",
        "acceptance_criteria": ["List endpoints accept pagination parameters", "Default page size configured"],
        "group": "performance",
    },
    "PRF-004": {
        "approach": "Replace synchronous blocking calls (requests.get, time.sleep, file I/O) in async functions with their async equivalents (httpx, asyncio.sleep, aiofiles).",
        "acceptance_criteria": ["No blocking calls in async functions", "Async equivalents used throughout"],
        "group": "performance",
    },
    "PRF-005": {
        "approach": "Add caching for expensive or frequently-accessed data. Use functools.lru_cache, Redis, or framework-level caching.",
        "acceptance_criteria": ["Caching implemented for expensive operations", "Cache invalidation strategy defined"],
        "group": "performance",
    },
    # ── Documentation ──
    "DOC-001": {
        "approach": "Create a README.md with: project description, setup instructions, usage examples, and contributing guidelines.",
        "acceptance_criteria": ["README.md exists", "Contains setup and usage instructions"],
        "group": "documentation",
    },
    "DOC-002": {
        "approach": "Expand README.md to at least 10 substantive lines covering: what the project does, how to install it, how to use it, and how to contribute.",
        "acceptance_criteria": ["README has >= 10 lines", "Covers installation, usage, and contribution"],
        "group": "documentation",
    },
    "DOC-003": {
        "approach": "Add API documentation using OpenAPI/Swagger (auto-generated from route definitions) or a dedicated docs page.",
        "acceptance_criteria": ["API documentation exists", "All endpoints documented with request/response schemas"],
        "group": "documentation",
    },
    "DOC-004": {
        "approach": "Add docstrings to public functions (>50% coverage). Include parameter descriptions, return types, and usage examples for complex functions.",
        "acceptance_criteria": [">50% of public functions have docstrings", "Complex functions have usage examples"],
        "group": "documentation",
    },
    "DOC-005": {
        "approach": "Create an Architecture Decision Records (ADR) directory (docs/adr/ or docs/decisions/) with at least one record documenting a key architectural choice.",
        "acceptance_criteria": ["ADR directory exists", "At least one decision documented"],
        "group": "documentation",
    },
    "DOC-006": {
        "approach": "Create a CHANGELOG.md tracking notable changes. Use Keep a Changelog format with sections for Added, Changed, Fixed, Removed.",
        "acceptance_criteria": ["CHANGELOG.md exists", "Follows structured format"],
        "group": "documentation",
    },
    # ── Operations ──
    "OPS-001": {
        "approach": "Add CI/CD configuration (.github/workflows/, .gitlab-ci.yml, or Jenkinsfile) with at minimum: lint, test, and build steps.",
        "acceptance_criteria": ["CI/CD config exists", "Pipeline runs lint + test + build"],
        "group": "operations",
    },
    "OPS-002": {
        "approach": "Create a Dockerfile with multi-stage build. Define a production-ready container image with minimal attack surface.",
        "acceptance_criteria": ["Dockerfile exists", "Multi-stage build separates build and runtime"],
        "group": "operations",
    },
    "OPS-003": {
        "approach": "Replace print() statements with structured logging (logging module, structlog). Use appropriate log levels (INFO, WARNING, ERROR).",
        "acceptance_criteria": ["No raw print() for logging", "Structured logging with appropriate levels"],
        "group": "observability",
    },
    "OPS-004": {
        "approach": "Add validation for environment variables at startup. Use Pydantic Settings, environs, or manual validation with clear error messages for missing/invalid vars.",
        "acceptance_criteria": ["All env vars validated at startup", "Clear error messages for missing vars"],
        "group": "config-hardening",
    },
    "OPS-005": {
        "approach": "Create a .env.example file documenting all required and optional environment variables with descriptions and example values.",
        "acceptance_criteria": [".env.example exists", "All env vars documented with descriptions"],
        "group": "documentation",
    },
    "OPS-006": {
        "approach": "Add linter configuration (.eslintrc, ruff.toml, or pyproject.toml [tool.ruff]) with sensible defaults for the project's language.",
        "acceptance_criteria": ["Linter config exists", "Rules configured for the project's language"],
        "group": "operations",
    },
}

# Default template for unknown check IDs
_DEFAULT_TEMPLATE = {
    "approach": "Review and address the failed check. Consult the check documentation for specific guidance.",
    "acceptance_criteria": ["Check passes on re-scan"],
    "group": "general",
}

# Dimension prefix to category mapping for priority assignment
_DIMENSION_PRIORITY = {
    "SEC": 1,   # Security first
    "REL": 2,
    "TST": 3,
    "MNT": 4,
    "PRF": 5,
    "OPS": 6,
    "DOC": 7,
}


def generate_check_remediation_items(
    failed_checks: list[CheckResult],
    repo_path: str | None = None,
) -> list[dict]:
    """Convert failed deterministic checks into remediation item dicts.

    Returns items compatible with RemediationPlan.items format.
    Each item is Tier 1 (deterministic, no LLM needed).
    Checks suppressed in .forgeignore are excluded.
    """
    # Load .forgeignore to skip suppressed checks
    suppressed_ids: set[str] = set()
    if repo_path:
        try:
            from forge.execution.forgeignore import ForgeIgnore
            forgeignore = ForgeIgnore.load(repo_path)
            for check in failed_checks:
                finding_dict = {
                    "check_id": check.check_id,
                    "title": check.name,
                    "severity": check.severity,
                    "locations": check.locations,
                }
                is_sup, _ = forgeignore.is_suppressed(finding_dict)
                if is_sup:
                    suppressed_ids.add(check.check_id)
        except Exception:
            pass  # Non-fatal — proceed without suppression

    items = []
    for check in failed_checks:
        if check.passed:
            continue
        if check.check_id in suppressed_ids:
            continue

        template = _FIX_TEMPLATES.get(check.check_id, _DEFAULT_TEMPLATE)
        prefix = check.check_id.split("-")[0]
        base_priority = _DIMENSION_PRIORITY.get(prefix, 5)

        # Higher severity = lower priority number (more urgent)
        severity_boost = {"critical": 0, "high": 0, "medium": 1, "low": 2}.get(
            check.severity, 2
        )

        files = [loc.get("file", loc.get("file_path", "")) for loc in check.locations if loc]
        files = [f for f in files if f]  # filter empty

        items.append({
            "finding_id": check.check_id,
            "title": f"[{check.check_id}] {check.name}",
            "tier": 1,  # Deterministic — known fix pattern
            "priority": base_priority + severity_boost,
            "estimated_files": max(1, len(files)),
            "files_to_modify": files or [],
            "depends_on": [],
            "acceptance_criteria": template["acceptance_criteria"],
            "approach": template["approach"],
            "group": template["group"],
        })

    # Sort by priority
    items.sort(key=lambda x: x["priority"])
    return items
