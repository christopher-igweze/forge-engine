"""Tier 1 deterministic fix handlers for FORGE remediation.

Each handler applies a rule-based fix without requiring an LLM.
Extracted from tier_router.py to separate fix logic from routing dispatch.
"""

from __future__ import annotations

import logging
import os
import re

from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    RemediationItem,
)
from forge.execution.tier1_helpers import (
    _detect_framework,
    _add_express_rate_limiter,
    _add_fastapi_rate_limiter,
    _add_flask_rate_limiter,
    _find_react_src,
    _ERROR_BOUNDARY_TSX,
    _ERROR_BOUNDARY_JSX,
)

logger = logging.getLogger(__name__)


# Mapping from template ID → deterministic fix function.
# These are simple, validated patches that don't need an LLM.
# Phase 2 ships a small set; Phase 3 expands via a plugin registry.
TIER1_TEMPLATES: dict[str, str] = {
    "replace-hardcoded-secret": "tier1_replace_secret",
    "add-rate-limiter": "tier1_add_rate_limiter",
    "create-env-example": "tier1_create_env_example",
    "add-error-boundary": "tier1_add_error_boundary",
}


def _find_template(finding: AuditFinding, item: RemediationItem) -> str:
    """Match a finding to a Tier 1 fix template."""
    # Check if triage classifier already assigned a template
    if hasattr(item, "fix_template_id") and getattr(item, "fix_template_id", ""):
        # RemediationItem doesn't have fix_template_id, but the triage
        # decision did. We check if the approach mentions a template.
        pass

    # Match by finding content keywords
    text = f"{finding.title} {finding.description} {finding.suggested_fix}".lower()

    from forge.prompts.triage_classifier import TIER_1_PATTERNS

    for pattern_info in TIER_1_PATTERNS:
        keywords = pattern_info.get("keywords", [])
        if any(kw.lower() in text for kw in keywords):
            return pattern_info.get("template_id", "")

    return ""


def _run_tier1_fix(
    template_id: str,
    finding: AuditFinding,
    repo_path: str,
) -> CoderFixResult:
    """Execute a Tier 1 deterministic fix.

    Phase 2: Stub implementations that log the action.
    Phase 3: Full AST-aware templates with rollback.
    """
    if template_id == "replace-hardcoded-secret":
        return _tier1_replace_secret(finding, repo_path)
    elif template_id == "add-rate-limiter":
        return _tier1_add_rate_limiter(finding, repo_path)
    elif template_id == "create-env-example":
        return _tier1_create_env_example(finding, repo_path)
    elif template_id == "add-error-boundary":
        return _tier1_add_error_boundary(finding, repo_path)
    else:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Unknown template: {template_id}",
            error_message=f"No handler for template_id={template_id}",
        )


def _tier1_replace_secret(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Replace hardcoded secrets with environment variable references.

    Phase 2: Identifies the secret location and replaces with env var.
    """
    files_changed = []

    for loc in finding.locations:
        file_path = os.path.join(repo_path, loc.file_path)
        if not os.path.isfile(file_path):
            continue

        try:
            with open(file_path, "r") as f:
                content = f.read()

            # Simple pattern: replace hardcoded string assignments
            # This is intentionally conservative — only replaces obvious patterns
            original = content
            # Replace patterns like: API_KEY = "hardcoded_value"
            content = re.sub(
                r'([A-Z_]*(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)[A-Z_]*)\s*=\s*["\']([^"\']{8,})["\']',
                r'\1 = os.environ.get("\1", "")',
                content,
            )

            if content != original:
                # Ensure os import exists
                if "import os" not in content:
                    content = "import os\n" + content
                with open(file_path, "w") as f:
                    f.write(content)
                files_changed.append(loc.file_path)

        except Exception as e:
            logger.error("Tier 1 secret replacement failed in %s: %s", loc.file_path, e)

    if files_changed:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=files_changed,
            summary=f"Replaced hardcoded secrets with env vars in {len(files_changed)} file(s)",
        )

    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary="Could not find replaceable secrets in referenced files",
        error_message="No matching patterns found",
    )


def _tier1_add_rate_limiter(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Add rate limiting middleware to an API route.

    Detects the framework (Express, FastAPI, Flask) and injects the
    appropriate rate-limiting middleware/dependency.
    """
    files_changed: list[str] = []
    framework = _detect_framework(repo_path)

    if framework == "express":
        files_changed = _add_express_rate_limiter(repo_path, finding)
    elif framework == "fastapi":
        files_changed = _add_fastapi_rate_limiter(repo_path, finding)
    elif framework == "flask":
        files_changed = _add_flask_rate_limiter(repo_path, finding)
    else:
        logger.info("Tier 1 rate limiter: unknown framework — deferring to Tier 2")
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Rate limiter: could not detect framework (got '{framework}')",
            error_message="Promoting to Tier 2 for AI-assisted fix",
        )

    if files_changed:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=files_changed,
            summary=f"Added {framework} rate limiting middleware in {len(files_changed)} file(s)",
        )

    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary=f"Rate limiter injection failed for {framework}",
        error_message="Could not find suitable injection point",
    )


def _tier1_create_env_example(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Create a .env.example file from existing .env.

    Strips values, keeps keys with placeholder comments.
    """
    env_path = os.path.join(repo_path, ".env")
    example_path = os.path.join(repo_path, ".env.example")

    if not os.path.isfile(env_path):
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary="No .env file found to create example from",
        )

    if os.path.isfile(example_path):
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.SKIPPED,
            summary=".env.example already exists",
        )

    try:
        lines = []
        with open(env_path, "r") as f:
            for line in f:
                line = line.rstrip()
                if not line or line.startswith("#"):
                    lines.append(line)
                    continue
                if "=" in line:
                    key = line.split("=", 1)[0].strip()
                    lines.append(f"{key}=")
                else:
                    lines.append(line)

        with open(example_path, "w") as f:
            f.write("# Environment variables — copy to .env and fill in values\n")
            f.write("\n".join(lines) + "\n")

        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=[".env.example"],
            summary="Created .env.example from .env (values stripped)",
        )
    except Exception as e:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Failed to create .env.example: {e}",
            error_message=str(e),
        )


def _tier1_add_error_boundary(finding: AuditFinding, repo_path: str) -> CoderFixResult:
    """Add React ErrorBoundary component and wrap the root app.

    Creates an ErrorBoundary component if none exists and wraps the
    main App export in the nearest entry point (App.tsx/App.jsx).
    """
    files_changed: list[str] = []

    # Find the React source directory
    src_dir = _find_react_src(repo_path)
    if not src_dir:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary="Could not find React source directory",
            error_message="Promoting to Tier 2 for AI-assisted fix",
        )

    # Create ErrorBoundary component if it doesn't exist
    boundary_path = None
    for ext in (".tsx", ".jsx", ".js"):
        candidate = os.path.join(src_dir, "components", f"ErrorBoundary{ext}")
        if os.path.isfile(candidate):
            boundary_path = candidate
            break

    if not boundary_path:
        # Determine if project uses TypeScript
        is_ts = any(
            f.endswith(".tsx") or f.endswith(".ts")
            for f in os.listdir(src_dir)
            if os.path.isfile(os.path.join(src_dir, f))
        )
        ext = ".tsx" if is_ts else ".jsx"
        comp_dir = os.path.join(src_dir, "components")
        os.makedirs(comp_dir, exist_ok=True)
        boundary_path = os.path.join(comp_dir, f"ErrorBoundary{ext}")

        boundary_code = _ERROR_BOUNDARY_TSX if is_ts else _ERROR_BOUNDARY_JSX
        with open(boundary_path, "w") as f:
            f.write(boundary_code)
        rel = os.path.relpath(boundary_path, repo_path)
        files_changed.append(rel)
        logger.info("Created ErrorBoundary component: %s", rel)

    # Find and wrap the App component
    app_file = None
    for name in ("App.tsx", "App.jsx", "App.js", "app.tsx", "app.jsx"):
        candidate = os.path.join(src_dir, name)
        if os.path.isfile(candidate):
            app_file = candidate
            break

    if app_file:
        with open(app_file, "r") as f:
            content = f.read()

        if "ErrorBoundary" not in content:
            # Add import
            import_line = 'import ErrorBoundary from "./components/ErrorBoundary";\n'

            # Insert after the last import
            last_import = 0
            for m in re.finditer(r'^import\s+.+;?\s*$', content, re.MULTILINE):
                last_import = m.end()

            if last_import > 0:
                content = content[:last_import] + "\n" + import_line + content[last_import:]
            else:
                content = import_line + content

            # Wrap the default export's JSX return
            # Look for: return ( ... ) or return <...>
            content = re.sub(
                r'(return\s*\(\s*\n?)(\s*)(<)',
                r'\1\2<ErrorBoundary>\n\2\3',
                content,
                count=1,
            )
            # Close the wrapper before the closing paren
            content = re.sub(
                r'(\n)(\s*)(\)\s*;?\s*})',
                r'\1\2  </ErrorBoundary>\n\2\3',
                content,
                count=1,
            )

            with open(app_file, "w") as f:
                f.write(content)
            rel = os.path.relpath(app_file, repo_path)
            files_changed.append(rel)

    if files_changed:
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.COMPLETED,
            files_changed=files_changed,
            summary=f"Added ErrorBoundary component and wrapped App in {len(files_changed)} file(s)",
        )

    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.FAILED_RETRYABLE,
        summary="Could not inject ErrorBoundary — no suitable App component found",
        error_message="Promoting to Tier 2 for AI-assisted fix",
    )
