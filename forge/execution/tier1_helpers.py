"""Framework detection and rate limiter injection helpers for Tier 1 fixes.

Extracted from tier_router.py to separate framework-specific logic
from the core tier routing dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge.schemas import AuditFinding

logger = logging.getLogger(__name__)


def _detect_framework(repo_path: str) -> str:
    """Detect the primary backend framework from package manifests."""
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
