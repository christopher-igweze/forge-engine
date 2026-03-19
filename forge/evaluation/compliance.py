"""Compliance mapping for FORGE v3 evaluation.

Maps deterministic check IDs to OWASP ASVS requirements, STRIDE categories,
and NIST SSDF practices.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# ASVS Level 1 check mapping
# Format: category -> list of (check_id, requirement_ref, asvs_level)
# ---------------------------------------------------------------------------

ASVS_CHECK_MAP: dict[str, list[tuple[str, str, int]]] = {
    "V1 - Encoding/Sanitization": [
        ("SEC-002", "V1.5.3 - Parameterized queries", 1),
        ("SEC-009", "V1.7.2 - No raw error output", 1),
    ],
    "V6 - Authentication": [
        ("SEC-004", "V6.2.1 - Auth required on routes", 1),
        ("SEC-005", "V6.2.3 - Secure password storage", 1),
        ("SEC-006", "V6.3.1 - No debug auth bypass", 1),
    ],
    "V11 - Cryptography": [
        ("SEC-005", "V11.1.1 - No deprecated crypto", 1),
    ],
    "V13 - Configuration": [
        ("SEC-001", "V13.1.3 - No hardcoded secrets", 1),
        ("SEC-006", "V13.4.1 - Secure default config", 1),
        ("SEC-012", "V13.4.2 - No insecure defaults", 1),
    ],
    "V14 - Data Protection": [
        ("SEC-010", "V14.3.3 - No PII in logs", 1),
    ],
}

# ---------------------------------------------------------------------------
# STRIDE mapping: check_id -> STRIDE threat category
# ---------------------------------------------------------------------------

STRIDE_MAP: dict[str, str] = {
    "SEC-001": "Information Disclosure",
    "SEC-002": "Tampering",
    "SEC-003": "Tampering",
    "SEC-004": "Spoofing",
    "SEC-005": "Information Disclosure",
    "SEC-006": "Elevation of Privilege",
    "SEC-007": "Information Disclosure",
    "SEC-008": "Tampering",
    "SEC-009": "Information Disclosure",
    "SEC-010": "Information Disclosure",
    "SEC-011": "Denial of Service",
    "SEC-012": "Elevation of Privilege",
}

# ---------------------------------------------------------------------------
# NIST SSDF practice mapping: check_id -> (practice_id, practice_name)
# ---------------------------------------------------------------------------

NIST_SSDF_MAP: dict[str, tuple[str, str]] = {
    "SEC-001": ("PW.1.1", "Secure Coding Practices"),
    "SEC-002": ("PW.5.1", "Validate All Inputs"),
    "SEC-003": ("PW.6.1", "Verify Third-Party Components"),
    "SEC-004": ("PW.1.1", "Secure Coding Practices"),
    "SEC-005": ("PW.6.2", "Cryptographic Practices"),
    "SEC-006": ("PW.1.1", "Secure Coding Practices"),
    "SEC-007": ("PW.6.1", "Verify Third-Party Components"),
    "SEC-008": ("PW.5.1", "Validate All Inputs"),
    "SEC-009": ("PW.1.1", "Secure Coding Practices"),
    "SEC-010": ("PO.5.2", "Protect Sensitive Data"),
    "SEC-011": ("PW.1.1", "Secure Coding Practices"),
    "SEC-012": ("PW.9.1", "Secure Defaults"),
    "REL-001": ("PW.1.1", "Secure Coding Practices"),
    "REL-002": ("PW.1.1", "Secure Coding Practices"),
    "TST-001": ("PW.8.2", "Adequate Test Coverage"),
    "TST-002": ("PW.8.1", "Test Existence"),
    "OPS-001": ("PS.1.1", "CI/CD Pipeline Security"),
    "OPS-002": ("PO.3.2", "Dependency Management"),
}


def estimate_asvs_level(check_results: list) -> dict:
    """Estimate OWASP ASVS compliance level from check results.

    Returns dict with estimated_level, total_requirements, passed, failed,
    and per-category breakdown.
    """
    passed_ids = {r.check_id for r in check_results if r.passed}

    categories: dict[str, dict] = {}
    total_reqs = 0
    total_passed = 0

    for category, requirements in ASVS_CHECK_MAP.items():
        cat_passed = 0
        cat_total = len(requirements)
        total_reqs += cat_total
        details = []

        for check_id, req_ref, level in requirements:
            is_passed = check_id in passed_ids
            if is_passed:
                cat_passed += 1
                total_passed += 1
            details.append({
                "check_id": check_id,
                "requirement": req_ref,
                "level": level,
                "passed": is_passed,
            })

        categories[category] = {
            "passed": cat_passed,
            "total": cat_total,
            "details": details,
        }

    # We only map L1 requirements, so max achievable is level 1
    estimated_level = 1 if total_reqs > 0 and total_passed == total_reqs else 0

    return {
        "estimated_level": estimated_level,
        "max_mappable_level": 1,
        "total_requirements": total_reqs,
        "passed": total_passed,
        "failed": total_reqs - total_passed,
        "categories": categories,
    }


def get_stride_mapping(check_results: list) -> dict:
    """Map check results to STRIDE threat categories.

    Returns dict with per-category counts and coverage summary.
    """
    all_categories = [
        "Spoofing",
        "Tampering",
        "Repudiation",
        "Information Disclosure",
        "Denial of Service",
        "Elevation of Privilege",
    ]

    coverage: dict[str, dict] = {
        cat: {"checks": [], "mitigated": 0, "total": 0} for cat in all_categories
    }

    for result in check_results:
        # Use stride field from CheckResult if available, else fall back to map
        stride_cat = result.stride or STRIDE_MAP.get(result.check_id, "")
        if not stride_cat:
            continue

        entry = coverage.get(stride_cat)
        if entry is None:
            continue

        entry["total"] += 1
        entry["checks"].append(result.check_id)
        if result.passed:
            entry["mitigated"] += 1

    return {
        "categories": {
            cat: {
                "mitigated": info["mitigated"],
                "total": info["total"],
                "checks": info["checks"],
            }
            for cat, info in coverage.items()
        },
        "covered_categories": sum(
            1 for info in coverage.values() if info["total"] > 0
        ),
        "total_categories": len(all_categories),
    }


def get_nist_coverage(check_results: list) -> dict:
    """Map check results to NIST SSDF practices.

    Returns dict with per-practice status and coverage summary.
    """
    passed_ids = {r.check_id for r in check_results if r.passed}
    all_ids = {r.check_id for r in check_results}

    practices: dict[str, dict] = {}

    for check_id, (practice_id, practice_name) in NIST_SSDF_MAP.items():
        if check_id not in all_ids:
            continue

        if practice_id not in practices:
            practices[practice_id] = {
                "name": practice_name,
                "checks": [],
                "passed": 0,
                "total": 0,
            }

        practices[practice_id]["checks"].append(check_id)
        practices[practice_id]["total"] += 1
        if check_id in passed_ids:
            practices[practice_id]["passed"] += 1

    covered = sum(
        1 for p in practices.values() if p["passed"] == p["total"] and p["total"] > 0
    )

    return {
        "practices": practices,
        "covered": covered,
        "total": len(practices),
    }
