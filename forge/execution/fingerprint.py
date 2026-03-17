"""Stable finding fingerprints for cross-scan tracking."""
from __future__ import annotations

import hashlib
import re


def find_match(
    finding: dict,
    baseline_entries: dict[str, dict],
    threshold: float = 0.7,
) -> str | None:
    """Find the best matching fingerprint in the baseline for a finding.

    Returns the matching fingerprint string if similarity >= threshold, else None.
    Uses multiple signals:
    1. Exact fingerprint match (1.0)
    2. Same file + same category + similar title (0.8-0.9)
    3. Same category + very similar title (0.7-0.8)
    """
    fp = finding.get("fingerprint", "")
    if fp and fp in baseline_entries:
        return fp

    finding_title = finding.get("title", "")
    finding_category = finding.get("category", "")
    finding_cwe = finding.get("cwe_id", "")
    finding_audit_pass = finding.get("audit_pass", "")
    loc = (finding.get("locations") or [{}])[0]
    finding_file = loc.get("file_path", "")

    best_fp: str | None = None
    best_score = 0.0

    for candidate_fp, entry in baseline_entries.items():
        score = 0.0

        # File path match: +0.3
        entry_file = entry.get("file_path", "")
        if finding_file and entry_file and finding_file == entry_file:
            score += 0.3

        # Category match: +0.2
        if finding_category and finding_category == entry.get("category", ""):
            score += 0.2

        # Title similarity: 0.0-0.5
        title_sim = _title_similarity(finding_title, entry.get("title", ""))
        score += title_sim * 0.5

        # CWE match: +0.1
        if finding_cwe and finding_cwe == entry.get("cwe_id", ""):
            score += 0.1

        # Audit pass match: +0.1
        if finding_audit_pass and finding_audit_pass == entry.get("audit_pass", ""):
            score += 0.1

        if score > best_score:
            best_score = score
            best_fp = candidate_fp

    if best_score >= threshold:
        return best_fp
    return None


def _title_similarity(title_a: str, title_b: str) -> float:
    """Compute Jaccard similarity between normalized titles."""
    tokens_a = set(_normalize_title(title_a).split())
    tokens_b = set(_normalize_title(title_b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


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
        finding.get("category") or "",
        finding.get("audit_pass") or "",
        file_path or "",
        str(line_bucket),
        norm_title,
        finding.get("cwe_id") or "",
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
