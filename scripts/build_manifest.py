#!/usr/bin/env python3
"""Generate forge/manifest.json from project state.

Run before each release: python scripts/build_manifest.py
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST_PATH = ROOT / "forge" / "manifest.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        if line.strip().startswith("version"):
            return line.split("=")[1].strip().strip('"')
    raise ValueError("Version not found in pyproject.toml")


def build_skills() -> dict:
    skills = {}
    skills_dir = ROOT / "forge" / "skills"
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            skills[skill_dir.name] = {
                "hash": sha256_file(skill_file),
                "file": f"skills/{skill_dir.name}/SKILL.md",
                "install_to": f"~/.claude/commands/{skill_dir.name}.md",
            }
    return skills


def main():
    # Load existing manifest to preserve manual fields
    existing = {}
    if MANIFEST_PATH.exists():
        existing = json.loads(MANIFEST_PATH.read_text())

    manifest = {
        "version": get_version(),
        "skills": build_skills(),
        "hooks": existing.get("hooks", {}),
        "mcp": existing.get("mcp", {
            "name": "forge",
            "command": "forge-mcp",
            "args": [],
            "env": ["OPENROUTER_API_KEY"],
            "scope": "user",
        }),
        "config_schema_version": existing.get("config_schema_version", 1),
        "migrations": existing.get("migrations", {}),
        "deprecated_flags": existing.get("deprecated_flags", {}),
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Manifest written: {MANIFEST_PATH}")
    print(f"  Version: {manifest['version']}")
    print(f"  Skills: {list(manifest['skills'].keys())}")


if __name__ == "__main__":
    main()
