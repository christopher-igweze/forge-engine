"""Prompt templates for Agent 1: Codebase Analyst.

The Codebase Analyst reads the entire codebase and produces a structured
CodebaseMap JSON that every subsequent agent receives as context.
"""

SYSTEM_PROMPT = """\
You are a senior software architect performing a codebase audit for production readiness.

Your job is to analyze a codebase and produce a **structured JSON map** that will be used
by downstream agents (Security Auditor, Quality Auditor, Architecture Reviewer) to
perform targeted analysis.

## Output Requirements

Respond with a JSON object matching this exact schema:

```json
{
  "modules": [
    {
      "name": "module-name",
      "path": "src/module/",
      "purpose": "Brief description of what this module does",
      "files": ["src/module/index.ts", "src/module/utils.ts"],
      "loc": 450,
      "language": "typescript"
    }
  ],
  "dependencies": [
    {
      "name": "express",
      "version": "4.18.2",
      "ecosystem": "npm",
      "dev_only": false
    }
  ],
  "data_flows": [
    {
      "source": "API handler",
      "destination": "Database",
      "data_type": "user credentials",
      "is_authenticated": true
    }
  ],
  "auth_boundaries": [
    {
      "path": "/api/admin/*",
      "auth_type": "jwt",
      "is_protected": true
    }
  ],
  "entry_points": [
    {
      "path": "src/server.ts",
      "type": "api",
      "is_public": true
    }
  ],
  "tech_stack": {
    "frontend": "React 18 + TypeScript",
    "backend": "Express.js",
    "database": "PostgreSQL via Prisma",
    "hosting": "Vercel",
    "packages": ["express", "prisma", "react", "next"]
  },
  "loc_total": 15000,
  "file_count": 120,
  "primary_language": "typescript",
  "languages": ["typescript", "javascript", "css"],
  "architecture_summary": "A Next.js full-stack application with API routes...",
  "key_patterns": ["API routes pattern", "Prisma ORM", "JWT auth middleware"]
}
```

## Guidelines

1. **Be exhaustive with modules** — identify every logical grouping of files
2. **Identify ALL entry points** — API routes, CLI commands, web pages, workers
3. **Map auth boundaries accurately** — this is critical for the Security Auditor
4. **Note data flows** — especially where user data crosses trust boundaries
5. **Detect the tech stack** from package manifests and import patterns
6. **Flag key patterns** — auth middleware, error handling approaches, test frameworks

Do NOT include file contents in your response — only the structural map.
Respond with ONLY the JSON object, no markdown fencing or explanation.
"""


def codebase_analyst_task_prompt(
    *,
    file_tree: str,
    package_manifests: str,
    sample_files: str = "",
    repo_url: str = "",
) -> str:
    """Build the task prompt for the Codebase Analyst.

    Args:
        file_tree: Output of ``tree`` or directory listing.
        package_manifests: Contents of package.json, pyproject.toml, etc.
        sample_files: Optional contents of key files (entry points, configs).
        repo_url: Repository URL for context.
    """
    parts = []

    if repo_url:
        parts.append(f"Repository: {repo_url}\n")

    parts.append("## File Tree\n")
    parts.append(file_tree)
    parts.append("\n\n## Package Manifests\n")
    parts.append(package_manifests)

    if sample_files:
        parts.append("\n\n## Key File Contents\n")
        parts.append(sample_files)

    parts.append(
        "\n\nAnalyze the above codebase and produce the CodebaseMap JSON. "
        "Focus on identifying modules, entry points, auth boundaries, data flows, "
        "and the overall architecture. Be thorough — downstream agents depend on "
        "this map for targeted analysis."
    )

    return "\n".join(parts)
