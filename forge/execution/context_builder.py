"""Context builder for FORGE audit agents.

Selects relevant files from the codebase and builds prompt context
that fits within token budgets. Each audit pass receives only the
files relevant to its analysis focus.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from forge.schemas import (
    AuditPassType,
    CodebaseMap,
)

logger = logging.getLogger(__name__)

# Approximate tokens per character (conservative for code)
CHARS_PER_TOKEN = 4
# Default max tokens for file contents in a single audit pass
DEFAULT_TOKEN_BUDGET = 80_000
# Files to skip when scanning
SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi",
    ".zip", ".tar", ".gz", ".br",
    ".pyc", ".pyo", ".so", ".dll",
    ".lock", ".map",
}
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".next", ".nuxt",
    "dist", "build", ".cache", "coverage", ".venv", "venv",
    ".artifacts", ".local-test",
}


def build_file_tree(repo_path: str, max_depth: int = 4) -> str:
    """Build a directory listing of the repo, respecting depth limits."""
    lines: list[str] = []
    root = Path(repo_path)

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip excluded directories
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in SKIP_DIRS and not d.startswith(".")
        ]

        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        if depth > max_depth:
            dirnames.clear()
            continue

        indent = "  " * depth
        dir_name = rel.name or "."
        lines.append(f"{indent}{dir_name}/")

        for f in sorted(filenames):
            if not f.startswith("."):
                lines.append(f"{indent}  {f}")

    return "\n".join(lines[:500])  # Cap at 500 lines


def read_package_manifests(repo_path: str) -> str:
    """Read package.json, pyproject.toml, Cargo.toml, go.mod, etc."""
    manifest_names = [
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
        "requirements.txt", "Gemfile", "pom.xml", "build.gradle",
    ]
    parts: list[str] = []
    root = Path(repo_path)

    for name in manifest_names:
        manifest = root / name
        if manifest.exists():
            try:
                content = manifest.read_text(errors="replace")[:5000]
                parts.append(f"### {name}\n```\n{content}\n```\n")
            except OSError:
                continue

    return "\n".join(parts) if parts else "(no package manifests found)"


def read_file_safe(path: str, max_chars: int = 15_000) -> str:
    """Read a file, truncating if necessary."""
    try:
        content = Path(path).read_text(errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... (truncated at {max_chars} chars)"
        return content
    except OSError:
        return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return len(text) // CHARS_PER_TOKEN


def _should_skip_file(path: str) -> bool:
    """Check if a file should be excluded from context."""
    p = Path(path)
    if p.suffix.lower() in SKIP_EXTENSIONS:
        return True
    if any(skip in p.parts for skip in SKIP_DIRS):
        return True
    return False


# ── Security-pass file selection ──────────────────────────────────────

_AUTH_KEYWORDS = [
    "auth", "login", "session", "token", "jwt", "oauth", "passport",
    "middleware", "guard", "permission", "role", "rbac", "acl",
]

_DATA_KEYWORDS = [
    "input", "validation", "sanitize", "escape", "query", "sql",
    "form", "upload", "secret", "env", "config", "credential",
    "password", "encrypt", "hash", "csrf", "xss",
]

_INFRA_KEYWORDS = [
    "rate", "limit", "cors", "helmet", "https", "ssl", "tls",
    "header", "security", "error", "handler", "middleware",
    "config", "deploy", "docker", "nginx", "proxy",
]

_PASS_KEYWORDS: dict[AuditPassType, list[str]] = {
    AuditPassType.AUTH_FLOW: _AUTH_KEYWORDS,
    AuditPassType.DATA_HANDLING: _DATA_KEYWORDS,
    AuditPassType.INFRASTRUCTURE: _INFRA_KEYWORDS,
}


def _score_file_for_pass(
    file_path: str,
    audit_pass: AuditPassType,
    codebase_map: CodebaseMap,
) -> int:
    """Score how relevant a file is for a specific audit pass."""
    path_lower = file_path.lower()
    keywords = _PASS_KEYWORDS.get(audit_pass, [])
    score = 0

    # Keyword matches in path
    for kw in keywords:
        if kw in path_lower:
            score += 3

    # Entry points are always relevant for security
    for ep in codebase_map.entry_points:
        if ep.path == file_path:
            score += 5

    # Auth boundaries are critical for auth pass
    if audit_pass == AuditPassType.AUTH_FLOW:
        for ab in codebase_map.auth_boundaries:
            if ab.path in file_path:
                score += 10

    # Config files relevant for infra pass
    if audit_pass == AuditPassType.INFRASTRUCTURE:
        if any(cfg in path_lower for cfg in ["config", "env", ".env", "docker", "nginx"]):
            score += 5

    return score


def select_files_for_pass(
    repo_path: str,
    audit_pass: AuditPassType,
    codebase_map: CodebaseMap,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """Select and read files relevant to a specific audit pass.

    Returns formatted file contents within the token budget.
    """
    root = Path(repo_path)

    # Collect all source files with relevance scores
    scored_files: list[tuple[int, str]] = []
    for fe in codebase_map.files:
        abs_path = str(root / fe.path)
        if _should_skip_file(fe.path):
            continue
        score = _score_file_for_pass(fe.path, audit_pass, codebase_map)
        scored_files.append((score, fe.path))

    # Sort by score descending
    scored_files.sort(key=lambda x: x[0], reverse=True)

    # Read files until budget exhausted
    parts: list[str] = []
    tokens_used = 0

    for score, rel_path in scored_files:
        if score == 0 and tokens_used > token_budget * 0.5:
            break  # Stop adding irrelevant files after half budget used

        abs_path = str(root / rel_path)
        content = read_file_safe(abs_path)
        if not content:
            continue

        file_tokens = _estimate_tokens(content)
        if tokens_used + file_tokens > token_budget:
            if tokens_used > 0:
                break
            # First file exceeds budget — truncate it
            max_chars = (token_budget - tokens_used) * CHARS_PER_TOKEN
            content = content[:max_chars] + "\n... (truncated)"

        parts.append(f"### {rel_path} (relevance: {score})\n```\n{content}\n```\n")
        tokens_used += file_tokens

    logger.info(
        "Selected %d files for %s pass (~%d tokens)",
        len(parts), audit_pass.value, tokens_used,
    )

    return "\n".join(parts) if parts else "(no relevant files found)"


# ── Quality-pass file selection (uses different keywords) ─────────────

_ERROR_HANDLING_KEYWORDS = [
    "error", "catch", "try", "exception", "boundary", "fallback",
    "handler", "throw", "reject", "fail",
]

_CODE_PATTERNS_KEYWORDS = [
    "util", "helper", "common", "shared", "lib", "service",
    "controller", "model", "schema", "type",
]

_PERF_KEYWORDS = [
    "query", "database", "cache", "pagination", "loop", "fetch",
    "api", "request", "pool", "connection",
]

QUALITY_PASS_KEYWORDS: dict[AuditPassType, list[str]] = {
    AuditPassType.ERROR_HANDLING: _ERROR_HANDLING_KEYWORDS,
    AuditPassType.CODE_PATTERNS: _CODE_PATTERNS_KEYWORDS,
    AuditPassType.PERFORMANCE: _PERF_KEYWORDS,
}


def select_files_for_quality_pass(
    repo_path: str,
    audit_pass: AuditPassType,
    codebase_map: CodebaseMap,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """Select files relevant to a quality audit pass."""
    root = Path(repo_path)
    keywords = QUALITY_PASS_KEYWORDS.get(audit_pass, [])

    scored_files: list[tuple[int, str]] = []
    for fe in codebase_map.files:
        if _should_skip_file(fe.path):
            continue
        path_lower = fe.path.lower()
        score = sum(3 for kw in keywords if kw in path_lower)
        scored_files.append((score, fe.path))

    scored_files.sort(key=lambda x: x[0], reverse=True)

    parts: list[str] = []
    tokens_used = 0

    for score, rel_path in scored_files:
        if score == 0 and tokens_used > token_budget * 0.5:
            break

        content = read_file_safe(str(root / rel_path))
        if not content:
            continue

        file_tokens = _estimate_tokens(content)
        if tokens_used + file_tokens > token_budget:
            if tokens_used > 0:
                break
            max_chars = (token_budget - tokens_used) * CHARS_PER_TOKEN
            content = content[:max_chars] + "\n... (truncated)"

        parts.append(f"### {rel_path}\n```\n{content}\n```\n")
        tokens_used += file_tokens

    return "\n".join(parts) if parts else "(no relevant files found)"


def build_codebase_inventory(repo_path: str) -> list[dict]:
    """Walk the repo and build a file inventory for the CodebaseMap.

    Returns list of dicts with path, language, loc.
    """
    root = Path(repo_path)
    inventory: list[dict] = []

    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
        ".rs": "rust", ".java": "java", ".rb": "ruby",
        ".php": "php", ".cs": "csharp", ".cpp": "cpp",
        ".c": "c", ".swift": "swift", ".kt": "kotlin",
        ".vue": "vue", ".svelte": "svelte",
        ".css": "css", ".scss": "scss", ".html": "html",
        ".sql": "sql", ".sh": "bash", ".yaml": "yaml",
        ".yml": "yaml", ".json": "json", ".toml": "toml",
        ".md": "markdown",
    }

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            if _should_skip_file(str(fpath)):
                continue

            suffix = fpath.suffix.lower()
            language = lang_map.get(suffix, "")
            if not language:
                continue  # Skip unknown file types

            try:
                loc = sum(1 for _ in fpath.open(errors="replace"))
            except OSError:
                loc = 0

            rel = str(fpath.relative_to(root))
            inventory.append({
                "path": rel,
                "language": language,
                "loc": loc,
            })

    return inventory
