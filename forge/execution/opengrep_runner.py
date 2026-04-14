"""Opengrep SAST engine runner.

Runs Opengrep as a subprocess, parses JSON output, and converts
results to FORGE finding format. Falls back gracefully when
Opengrep is not installed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Severity mapping: Opengrep -> FORGE
SEVERITY_MAP = {
    "ERROR": "high",      # Opengrep ERROR = FORGE high (critical reserved for confirmed exploits)
    "WARNING": "medium",
    "INFO": "low",
}

# Category extraction from rule ID or metadata
CATEGORY_MAP = {
    "security": "security",
    "injection": "security",
    "xss": "security",
    "ssrf": "security",
    "crypto": "security",
    "auth": "security",
    "quality": "quality",
    "error": "quality",
    "performance": "performance",
    "n-plus-one": "performance",
    "sync": "performance",
}


@dataclass
class OpengrepFinding:
    """A single finding from Opengrep scan."""
    check_id: str              # Full rule ID (e.g., "forge.security.sql-injection-python")
    path: str                  # Relative file path
    line_start: int
    line_end: int
    col_start: int = 0
    col_end: int = 0
    message: str = ""
    severity: str = "medium"   # FORGE severity: critical/high/medium/low
    fingerprint: str = ""      # Opengrep's stable fingerprint
    metadata: dict = field(default_factory=dict)  # cwe, owasp, etc.
    snippet: str = ""          # Matched code line(s)
    fix: str | None = None     # Autofix suggestion if available
    source: str = "deterministic"
    category: str = "security" # security/quality/performance
    forge_check_id: str = ""   # e.g., "SEC-001" from rule metadata


def opengrep_available() -> bool:
    """Check if opengrep binary is available on PATH."""
    return shutil.which("opengrep") is not None


class OpengrepRunner:
    """Run Opengrep SAST scans and parse results."""

    # Directories that bloat scans without adding value — vendored deps,
    # build artifacts, generated code. Excluded from every Opengrep run.
    DEFAULT_EXCLUDES = (
        "node_modules",
        ".next",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "target",           # Rust / Java
        ".gradle",
        "coverage",
        ".coverage",
        "htmlcov",
        ".nuxt",
        "out",              # Next.js static export
        ".turbo",
        ".cache",
        "vendor",           # Go / PHP vendored deps
        "bower_components",
        ".git",
        ".svelte-kit",
        ".output",
    )

    def __init__(
        self,
        rules_dirs: list[str] | None = None,
        use_community_rules: bool = True,
        timeout: int = 900,
        excludes: tuple[str, ...] | None = None,
        extra_excludes: list[str] | None = None,
        respect_gitignore: bool = True,
    ):
        """Initialize runner.

        Args:
            rules_dirs: Paths to directories containing YAML rules.
                        Defaults to forge/rules/ if None.
            use_community_rules: Also run community rules via --config auto.
            timeout: Max seconds for scan subprocess. Default 15 min covers
                     mid-size monorepos; smaller repos finish in seconds.
            excludes: Tuple of directory names to skip. Defaults to
                      DEFAULT_EXCLUDES (node_modules, .venv, etc.).
            extra_excludes: Additional exclude patterns sourced from
                      `.forgeignore` entries that target entire directories
                      (no rule_family / check_id / category). These get
                      appended to the default excludes for upfront skipping.
            respect_gitignore: Pass ``--use-git-ignore`` to Opengrep so it
                      honors the repo's ``.gitignore``. True by default.
        """
        self.timeout = timeout
        self.use_community_rules = use_community_rules
        self.excludes = excludes if excludes is not None else self.DEFAULT_EXCLUDES
        self.extra_excludes = tuple(extra_excludes or ())
        self.respect_gitignore = respect_gitignore

        if rules_dirs is None:
            # Default to forge/rules/ relative to this file
            default_rules = Path(__file__).parent.parent / "rules"
            self.rules_dirs = [str(default_rules)] if default_rules.is_dir() else []
        else:
            self.rules_dirs = rules_dirs

    def scan(self, repo_path: str) -> list[OpengrepFinding]:
        """Run Opengrep scan on a repository.

        Returns list of findings. Returns empty list if opengrep not available.
        """
        if not opengrep_available():
            logger.warning("Opengrep not installed — returning empty results")
            return []

        try:
            raw_output = self._run_opengrep(repo_path)
            return self._parse_results(raw_output, repo_path)
        except subprocess.TimeoutExpired:
            logger.error("Opengrep scan timed out after %ds", self.timeout)
            return []
        except Exception as e:
            logger.error("Opengrep scan failed: %s", e)
            return []

    def _run_opengrep(self, repo_path: str) -> dict:
        """Execute opengrep subprocess and return parsed JSON."""
        cmd = ["opengrep", "scan"]

        # Add custom rules directories
        for rules_dir in self.rules_dirs:
            if Path(rules_dir).is_dir():
                cmd.extend(["--config", rules_dir])

        # Add community rules if requested
        if self.use_community_rules:
            cmd.extend(["--config", "auto"])

        # Exclude bloat directories — Opengrep accepts glob patterns via
        # --exclude. Skipping node_modules / .venv / etc. cuts scan time
        # on monorepos from 5+ min to under 30s without losing findings
        # in user code.
        for excl in self.excludes:
            cmd.extend(["--exclude", excl])

        # Extra excludes from .forgeignore (file-only patterns with no
        # rule_family/check_id restriction — they mean "never scan here")
        for excl in self.extra_excludes:
            cmd.extend(["--exclude", excl])

        # Respect the repo's .gitignore so vendored / generated content
        # the project already flags as untracked is also skipped by SAST.
        if self.respect_gitignore:
            cmd.append("--use-git-ignore")

        logger.info(
            "Opengrep excludes — defaults=%d forgeignore=%d gitignore=%s",
            len(self.excludes),
            len(self.extra_excludes),
            "yes" if self.respect_gitignore else "no",
        )

        # JSON output, scan target
        cmd.extend(["--json", "--quiet", repo_path])

        logger.info("Running: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=repo_path,
        )

        # Opengrep returns exit code 1 when findings exist, 0 when clean
        # Only error on other exit codes
        if result.returncode not in (0, 1):
            # Try to parse anyway — sometimes warnings cause non-zero exit
            if result.stdout.strip().startswith("{"):
                pass  # We can still parse
            else:
                logger.error("Opengrep exited with code %d: %s",
                             result.returncode, result.stderr[:500])

        # Parse JSON from stdout
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error("Failed to parse Opengrep JSON output: %s",
                         result.stdout[:500])
            return {"results": [], "errors": []}

    def _parse_results(self, raw: dict, repo_path: str) -> list[OpengrepFinding]:
        """Parse Opengrep JSON output into OpengrepFinding list."""
        findings = []

        for result in raw.get("results", []):
            try:
                finding = self._convert_result(result, repo_path)
                if finding:
                    findings.append(finding)
            except Exception as e:
                logger.warning("Failed to parse Opengrep result: %s", e)

        # Log errors from opengrep
        for error in raw.get("errors", []):
            logger.debug("Opengrep error: %s", error)

        logger.info("Opengrep found %d findings (%d errors)",
                     len(findings), len(raw.get("errors", [])))

        return findings

    def _convert_result(self, result: dict, repo_path: str) -> OpengrepFinding | None:
        """Convert a single Opengrep JSON result to OpengrepFinding."""
        extra = result.get("extra", {})
        metadata = extra.get("metadata", {})

        # Get relative path
        path = result.get("path", "")
        try:
            path = str(Path(path).relative_to(repo_path))
        except ValueError:
            pass  # Already relative or different root

        # Skip ignored findings
        if extra.get("is_ignored", False):
            return None

        # Map severity
        og_severity = extra.get("severity", "WARNING")
        forge_severity = SEVERITY_MAP.get(og_severity, "medium")

        # Determine category from metadata or rule ID
        check_id = result.get("check_id", "")
        category = metadata.get("category", "")
        if not category:
            category = self._infer_category(check_id)

        # Extract forge-check-id from metadata if present (FORGE custom rules)
        forge_check_id = metadata.get("forge-check-id", "")

        # Extract CWE (normalize to string list)
        cwe_list = metadata.get("cwe", [])
        if isinstance(cwe_list, str):
            cwe_list = [cwe_list]

        # Extract OWASP
        owasp_list = metadata.get("owasp", [])
        if isinstance(owasp_list, str):
            owasp_list = [owasp_list]

        return OpengrepFinding(
            check_id=check_id,
            path=path,
            line_start=result.get("start", {}).get("line", 0),
            line_end=result.get("end", {}).get("line", 0),
            col_start=result.get("start", {}).get("col", 0),
            col_end=result.get("end", {}).get("col", 0),
            message=extra.get("message", ""),
            severity=forge_severity,
            fingerprint=extra.get("fingerprint", ""),
            metadata={
                "cwe": cwe_list,
                "owasp": owasp_list,
                "references": metadata.get("references", []),
                "source_rule_url": metadata.get("source-rule-url", ""),
                "confidence": metadata.get("confidence", "MEDIUM"),
                "impact": metadata.get("impact", "MEDIUM"),
                "engine_kind": extra.get("engine_kind", ""),
            },
            snippet=extra.get("lines", "").strip(),
            fix=extra.get("fix"),
            source="deterministic",
            category=category,
            forge_check_id=forge_check_id,
        )

    def _infer_category(self, check_id: str) -> str:
        """Infer finding category from rule ID."""
        check_lower = check_id.lower()

        # Check for FORGE custom rule prefix
        if check_lower.startswith("forge."):
            parts = check_lower.split(".")
            if len(parts) >= 2:
                return parts[1]  # forge.security.xxx -> security

        # Check community rule patterns
        for keyword, category in CATEGORY_MAP.items():
            if keyword in check_lower:
                return category

        # Default to security for unknown rules
        return "security"


def to_audit_finding(og_finding: OpengrepFinding) -> dict:
    """Convert OpengrepFinding to a dict compatible with FORGE's AuditFinding schema.

    This allows Opengrep findings to flow through the existing FORGE pipeline
    (baseline comparison, .forgeignore, severity calibration, report generation).
    """
    # Map severity string to match FindingSeverity enum values
    severity = og_finding.severity  # already mapped to forge format

    # Get first CWE and OWASP
    cwe_list = og_finding.metadata.get("cwe", [])
    owasp_list = og_finding.metadata.get("owasp", [])

    # Extract CWE ID (e.g., "CWE-78" from "CWE-78: Improper Neutralization...")
    cwe_id = ""
    if cwe_list:
        cwe_str = cwe_list[0]
        if ":" in cwe_str:
            cwe_id = cwe_str.split(":")[0].strip()
        else:
            cwe_id = cwe_str

    owasp_ref = owasp_list[0] if owasp_list else ""

    return {
        "title": og_finding.message[:120] if og_finding.message else og_finding.check_id,
        "description": og_finding.message,
        "category": og_finding.category,
        "severity": severity,
        "locations": [{
            "file_path": og_finding.path,
            "line_start": og_finding.line_start,
            "line_end": og_finding.line_end,
            "snippet": og_finding.snippet,
        }],
        "confidence": 0.95 if og_finding.metadata.get("confidence") == "HIGH" else 0.85,
        "cwe_id": cwe_id,
        "owasp_ref": owasp_ref,
        "data_flow": f"{og_finding.path}:{og_finding.line_start} [{og_finding.check_id}]",
        "source": "deterministic",
        "fingerprint": og_finding.fingerprint,
        "suggested_fix": og_finding.fix or "",
        "forge_check_id": og_finding.forge_check_id,
    }
