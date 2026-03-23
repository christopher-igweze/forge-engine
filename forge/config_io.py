"""Shared config I/O — used by cli.py and setup_wizard.py.

Single source of truth for loading/saving ~/.vibe2prod/config.json
with atomic writes and secure permissions.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_PATH = Path.home() / ".vibe2prod" / "config.json"


def load_config() -> dict:
    """Load config from ~/.vibe2prod/config.json."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(data: dict) -> None:
    """Write config atomically with owner-only permissions (0o600)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=CONFIG_PATH.parent,
        prefix=".config_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
