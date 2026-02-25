"""Tier-based dispatch router for FORGE remediation.

Routes findings to the appropriate fix mechanism based on their
assigned tier from the Triage Classifier (Agent 6):

  Tier 0: Auto-skip (invalid / false-positive)
  Tier 1: Deterministic patch (no LLM, uses tier1/ rules engine)
  Tier 2: Scoped AI fix (1-3 files, routed to inner loop with Tier 2 coder)
  Tier 3: Architectural AI fix (5-15 files, routed to inner loop with Tier 3 coder)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from forge.schemas import (
    AuditFinding,
    CoderFixResult,
    FixOutcome,
    RemediationItem,
    RemediationPlan,
    RemediationTier,
)

if TYPE_CHECKING:
    from forge.config import ForgeConfig
    from forge.schemas import ForgeExecutionState

logger = logging.getLogger(__name__)


# ── Tier 1 Fix Templates ────────────────────────────────────────────

# Mapping from template ID → deterministic fix function.
# These are simple, validated patches that don't need an LLM.
# Phase 2 ships a small set; Phase 3 expands via a plugin registry.

TIER1_TEMPLATES: dict[str, str] = {
    "replace-hardcoded-secret": "tier1_replace_secret",
    "add-rate-limiter": "tier1_add_rate_limiter",
    "create-env-example": "tier1_create_env_example",
    "add-error-boundary": "tier1_add_error_boundary",
}


def apply_tier0(
    finding: AuditFinding,
    item: RemediationItem,
) -> CoderFixResult:
    """Handle Tier 0 findings — auto-skip with log."""
    logger.info("Tier 0: auto-skipping %s — %s", finding.id, finding.title)
    return CoderFixResult(
        finding_id=finding.id,
        outcome=FixOutcome.SKIPPED,
        summary=f"Auto-skipped: {finding.title} (Tier 0 — invalid/false-positive)",
    )


def apply_tier1(
    finding: AuditFinding,
    item: RemediationItem,
    repo_path: str,
) -> CoderFixResult:
    """Handle Tier 1 findings — deterministic fix from template.

    For Phase 2, this dispatches to simple rule-based fixers.
    Phase 3 will add a full template engine with AST manipulation.
    """
    template_id = _find_template(finding, item)

    if not template_id:
        logger.warning(
            "Tier 1: no template found for %s — deferring to Tier 2",
            finding.id,
        )
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"No Tier 1 template matched for: {finding.title}",
            error_message="No matching template — will be escalated to Tier 2",
        )

    handler_name = TIER1_TEMPLATES.get(template_id, "")
    logger.info(
        "Tier 1: applying template %s (%s) for %s",
        template_id, handler_name, finding.id,
    )

    try:
        result = _run_tier1_fix(template_id, finding, repo_path)
        return result
    except Exception as e:
        logger.error("Tier 1 fix failed for %s: %s", finding.id, e)
        return CoderFixResult(
            finding_id=finding.id,
            outcome=FixOutcome.FAILED_RETRYABLE,
            summary=f"Tier 1 template {template_id} failed",
            error_message=str(e),
        )


def route_plan_items(
    plan: RemediationPlan,
    findings: list[AuditFinding],
    state: ForgeExecutionState,
    repo_path: str,
    cfg: ForgeConfig,
) -> tuple[list[RemediationItem], list[RemediationItem]]:
    """Split plan items into deterministic (Tier 0-1) and AI (Tier 2-3).

    Tier 0 and Tier 1 are handled synchronously before the async
    inner/middle/outer loops run.

    Returns:
        (handled_items, ai_items) — items that were resolved immediately
        vs items that need the full coder pipeline.
    """
    finding_map: dict[str, AuditFinding] = {f.id: f for f in findings}
    handled: list[RemediationItem] = []
    ai_items: list[RemediationItem] = []

    for item in plan.items:
        finding = finding_map.get(item.finding_id)
        if not finding:
            logger.warning("Finding %s not found — skipping", item.finding_id)
            continue

        if item.tier == RemediationTier.TIER_0:
            result = apply_tier0(finding, item)
            state.completed_fixes.append(result)
            handled.append(item)

        elif item.tier == RemediationTier.TIER_1:
            if not cfg.enable_tier1_rules:
                logger.info("Tier 1 rules disabled — promoting %s to Tier 2", finding.id)
                item.tier = RemediationTier.TIER_2
                ai_items.append(item)
                continue

            result = apply_tier1(finding, item, repo_path)
            if result.outcome in (FixOutcome.COMPLETED, FixOutcome.SKIPPED):
                state.completed_fixes.append(result)
                handled.append(item)
            else:
                # Failed Tier 1 → promote to Tier 2
                logger.info("Tier 1 failed for %s — promoting to Tier 2", finding.id)
                item.tier = RemediationTier.TIER_2
                ai_items.append(item)

        elif item.tier in (RemediationTier.TIER_2, RemediationTier.TIER_3):
            ai_items.append(item)

        else:
            logger.warning("Unknown tier %s for %s — treating as Tier 2", item.tier, finding.id)
            item.tier = RemediationTier.TIER_2
            ai_items.append(item)

    logger.info(
        "Tier router: %d handled (Tier 0/1), %d need AI (Tier 2/3)",
        len(handled), len(ai_items),
    )
    return handled, ai_items


# ── Tier 1 Internal Fix Handlers ─────────────────────────────────────


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
    import os

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
    import os
    import re

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
    import os

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
    import os

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
    import os
    import re

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


# ── Framework Detection & Rate Limiter Helpers ────────────────────────


def _detect_framework(repo_path: str) -> str:
    """Detect the primary backend framework from package manifests."""
    import json
    import os

    # Check package.json for Node.js frameworks
    pkg_json = os.path.join(repo_path, "package.json")
    if os.path.isfile(pkg_json):
        try:
            with open(pkg_json) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "express" in deps:
                return "express"
            if "fastify" in deps:
                return "express"  # Similar middleware pattern
            if "koa" in deps:
                return "express"  # Similar enough
        except (json.JSONDecodeError, OSError):
            pass

    # Check pyproject.toml / requirements.txt for Python frameworks
    for req_file in ("pyproject.toml", "requirements.txt"):
        req_path = os.path.join(repo_path, req_file)
        if os.path.isfile(req_path):
            try:
                content = open(req_path).read().lower()
                if "fastapi" in content:
                    return "fastapi"
                if "flask" in content:
                    return "flask"
                if "django" in content:
                    return "flask"  # Similar middleware pattern
            except OSError:
                pass

    return "unknown"


def _add_express_rate_limiter(repo_path: str, finding: AuditFinding) -> list[str]:
    """Inject express-rate-limit middleware into an Express app."""
    import os
    import re

    files_changed: list[str] = []

    # Find the main Express app file
    candidates = [
        "src/app.js", "src/app.ts", "src/index.js", "src/index.ts",
        "app.js", "app.ts", "index.js", "index.ts",
        "server.js", "server.ts", "src/server.js", "src/server.ts",
    ]
    app_file = None
    for c in candidates:
        full = os.path.join(repo_path, c)
        if os.path.isfile(full):
            app_file = full
            break

    if not app_file:
        return []

    with open(app_file, "r") as f:
        content = f.read()

    if "rateLimit" in content or "rate-limit" in content or "rateLimiter" in content:
        return []  # Already has rate limiting

    # Add import and middleware
    rate_limit_import = 'const rateLimit = require("express-rate-limit");\n'
    rate_limit_middleware = """
const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100, // Limit each IP to 100 requests per window
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: "Too many requests, please try again later." },
});
"""

    # Insert import after existing requires/imports
    last_import = 0
    for m in re.finditer(
        r'^(?:const|let|var|import)\s+.+(?:require|from).+$',
        content, re.MULTILINE,
    ):
        last_import = m.end()

    if last_import > 0:
        content = (
            content[:last_import] + "\n" + rate_limit_import +
            rate_limit_middleware + content[last_import:]
        )
    else:
        content = rate_limit_import + rate_limit_middleware + "\n" + content

    # Add app.use(limiter) after app creation
    app_use_pattern = re.search(
        r'((?:const|let|var)\s+app\s*=\s*express\(\).*?;)',
        content,
    )
    if app_use_pattern:
        insert_pos = app_use_pattern.end()
        content = content[:insert_pos] + "\napp.use(limiter);\n" + content[insert_pos:]

    with open(app_file, "w") as f:
        f.write(content)

    rel = os.path.relpath(app_file, repo_path)
    files_changed.append(rel)
    return files_changed


def _add_fastapi_rate_limiter(repo_path: str, finding: AuditFinding) -> list[str]:
    """Add slowapi rate limiting to a FastAPI app."""
    import os
    import re

    files_changed: list[str] = []

    candidates = [
        "src/main.py", "main.py", "app/main.py", "src/app.py", "app.py",
    ]
    app_file = None
    for c in candidates:
        full = os.path.join(repo_path, c)
        if os.path.isfile(full):
            app_file = full
            break

    if not app_file:
        return []

    with open(app_file, "r") as f:
        content = f.read()

    if "slowapi" in content or "RateLimiter" in content:
        return []

    rate_limit_code = (
        "from slowapi import Limiter, _rate_limit_exceeded_handler\n"
        "from slowapi.util import get_remote_address\n"
        "from slowapi.errors import RateLimitExceeded\n"
    )

    setup_code = (
        '\nlimiter = Limiter(key_func=get_remote_address, default_limits=["100/15minutes"])\n'
    )

    # Insert imports at top
    last_import = 0
    for m in re.finditer(r'^(?:from|import)\s+.+$', content, re.MULTILINE):
        last_import = m.end()

    if last_import > 0:
        content = content[:last_import] + "\n" + rate_limit_code + content[last_import:]
    else:
        content = rate_limit_code + content

    # Add limiter setup after app creation
    app_pattern = re.search(r'(app\s*=\s*FastAPI\(.+?\))', content, re.DOTALL)
    if app_pattern:
        insert_pos = app_pattern.end()
        attach_code = (
            setup_code +
            "app.state.limiter = limiter\n"
            "app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)\n"
        )
        content = content[:insert_pos] + attach_code + content[insert_pos:]

    with open(app_file, "w") as f:
        f.write(content)

    rel = os.path.relpath(app_file, repo_path)
    files_changed.append(rel)
    return files_changed


def _add_flask_rate_limiter(repo_path: str, finding: AuditFinding) -> list[str]:
    """Add Flask-Limiter to a Flask app."""
    import os
    import re

    files_changed: list[str] = []

    candidates = [
        "src/app.py", "app.py", "src/main.py", "main.py", "app/__init__.py",
    ]
    app_file = None
    for c in candidates:
        full = os.path.join(repo_path, c)
        if os.path.isfile(full):
            app_file = full
            break

    if not app_file:
        return []

    with open(app_file, "r") as f:
        content = f.read()

    if "flask_limiter" in content or "Limiter" in content:
        return []

    import_line = "from flask_limiter import Limiter\nfrom flask_limiter.util import get_remote_address\n"

    last_import = 0
    for m in re.finditer(r'^(?:from|import)\s+.+$', content, re.MULTILINE):
        last_import = m.end()

    if last_import > 0:
        content = content[:last_import] + "\n" + import_line + content[last_import:]
    else:
        content = import_line + content

    # Add limiter after app creation
    app_pattern = re.search(r'(app\s*=\s*Flask\(.+?\))', content)
    if app_pattern:
        insert_pos = app_pattern.end()
        setup = (
            '\nlimiter = Limiter(\n'
            '    get_remote_address,\n'
            '    app=app,\n'
            '    default_limits=["100 per 15 minutes"],\n'
            '    storage_uri="memory://",\n'
            ')\n'
        )
        content = content[:insert_pos] + setup + content[insert_pos:]

    with open(app_file, "w") as f:
        f.write(content)

    rel = os.path.relpath(app_file, repo_path)
    files_changed.append(rel)
    return files_changed


def _find_react_src(repo_path: str) -> str | None:
    """Find the React source directory."""
    import os

    candidates = [
        os.path.join(repo_path, "src"),
        os.path.join(repo_path, "app"),
        os.path.join(repo_path, "client", "src"),
        os.path.join(repo_path, "frontend", "src"),
    ]
    for d in candidates:
        if os.path.isdir(d):
            # Verify it looks like React (has .jsx/.tsx files)
            for f in os.listdir(d):
                if f.endswith((".jsx", ".tsx")):
                    return d
    return None


# ── Error Boundary Templates ─────────────────────────────────────────

_ERROR_BOUNDARY_TSX = '''import React, { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        this.props.fallback || (
          <div style={{ padding: "2rem", textAlign: "center" }}>
            <h2>Something went wrong</h2>
            <p>Please refresh the page or try again later.</p>
            <button onClick={() => this.setState({ hasError: false, error: null })}>
              Try Again
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
'''

_ERROR_BOUNDARY_JSX = '''import React, { Component } from "react";

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback || (
          <div style={{ padding: "2rem", textAlign: "center" }}>
            <h2>Something went wrong</h2>
            <p>Please refresh the page or try again later.</p>
            <button onClick={() => this.setState({ hasError: false, error: null })}>
              Try Again
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
'''
