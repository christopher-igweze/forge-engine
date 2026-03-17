"""Stable finding fingerprints for cross-scan tracking."""
from __future__ import annotations

import hashlib
import re


def fingerprint(finding: dict) -> str:
    """Generate a stable fingerprint for a finding.

    Hash components:
    - category (security, quality, architecture, performance)
    - audit_pass (auth_flow, data_handling, etc.)
    - primary file path (relative to repo root)
    - line range bucket (floor to nearest 10 lines)
    - normalized title (lowercase, strip file paths/line numbers/LOC counts)
    - CWE ID if present
    """
    norm_title = _normalize_title(finding.get("title", ""))
    loc = (finding.get("locations") or [{}])[0]
    file_path = loc.get("file_path", "")
    line_bucket = (loc.get("line_start", 0) // 10) * 10

    components = [
        finding.get("category", ""),
        finding.get("audit_pass", ""),
        file_path,
        str(line_bucket),
        norm_title,
        finding.get("cwe_id", ""),
    ]
    raw = "|".join(components)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_title(title: str) -> str:
    """Normalize finding title for stable fingerprinting.

    Strips: file paths, line numbers, LOC counts, specific numbers.
    """
    # Remove file paths (forward/backslash paths)
    result = re.sub(r"[/\\][\w./\\-]+", "", title)
    # Remove numbers (line counts, LOC, etc.)
    result = re.sub(r"\d+", "N", result)
    # Collapse whitespace
    result = re.sub(r"\s+", " ", result).lower().strip()
    return result
