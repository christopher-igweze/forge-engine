"""Build a Daytona snapshot for the vibe2prod backend.

Each forge-engine PyPI release gets a corresponding Daytona snapshot named
``vibe2prod-{version}``. The snapshot bakes in the OS deps, the Opengrep
binary, and the published vibe2prod PyPI package so the backend's
sandbox provisioning can skip the inline image build (saves ~30-60s
per scan and avoids registry-fetch hangs).

Usage:
    DAYTONA_API_KEY=... python scripts/build_daytona_snapshot.py

The version is read from pyproject.toml. If a snapshot with the same
name already exists, the script exits 0 (idempotent — safe to re-run
in CI).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — Python 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found]

from daytona import (
    CreateSnapshotParams,
    Daytona,
    DaytonaConfig,
    Image,
    Resources,
)
from daytona.common.errors import DaytonaError


OPENGREP_VERSION = "v1.19.0"
OPENGREP_URL = (
    f"https://github.com/opengrep/opengrep/releases/download/"
    f"{OPENGREP_VERSION}/opengrep_manylinux_x86"
)


def _read_version() -> str:
    """Read the package version from pyproject.toml."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def _snapshot_exists(daytona: Daytona, name: str) -> bool:
    """Return True if a snapshot with this exact name already exists."""
    try:
        snapshots = daytona.snapshot.list()
    except Exception as exc:
        print(f"warning: could not list existing snapshots: {exc}", file=sys.stderr)
        return False
    for snap in snapshots:
        if getattr(snap, "name", None) == name:
            return True
    return False


def main() -> int:
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        print("error: DAYTONA_API_KEY is required", file=sys.stderr)
        return 2

    version = _read_version()
    snapshot_name = f"vibe2prod-{version}"

    daytona = Daytona(
        DaytonaConfig(
            api_key=api_key,
            api_url=os.environ.get("DAYTONA_API_URL") or "https://app.daytona.io/api",
            target=os.environ.get("DAYTONA_TARGET") or None,
        )
    )

    if _snapshot_exists(daytona, snapshot_name):
        print(f"snapshot {snapshot_name!r} already exists — nothing to do")
        return 0

    image = (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update && "
            "apt-get install -y --no-install-recommends "
            "git build-essential curl ca-certificates procps && "
            "rm -rf /var/lib/apt/lists/* && "
            f"curl -fsSL {OPENGREP_URL} -o /usr/local/bin/opengrep && "
            "chmod +x /usr/local/bin/opengrep && "
            "/usr/local/bin/opengrep --version"
        )
        .pip_install([f"vibe2prod=={version}"])
        .workdir("/home/daytona")
    )

    print(f"building snapshot {snapshot_name!r} (vibe2prod=={version})...")
    try:
        daytona.snapshot.create(
            CreateSnapshotParams(
                name=snapshot_name,
                image=image,
                resources=Resources(cpu=2, memory=4, disk=10),
            ),
            on_logs=lambda chunk: print(chunk, end=""),
        )
    except DaytonaError as exc:
        if "already exists" in str(exc).lower():
            # A previous failed build left a broken snapshot entry.
            # Delete it and retry once.
            print(f"\nsnapshot {snapshot_name!r} exists (likely broken) — deleting and retrying")
            try:
                # SDK delete() expects the snapshot object, not a name string.
                snapshots = daytona.snapshot.list()
                snap_obj = next((s for s in snapshots if getattr(s, "name", None) == snapshot_name), None)
                if snap_obj:
                    daytona.snapshot.delete(snap_obj)
                else:
                    print(f"warning: snapshot {snapshot_name!r} not found in list — trying create anyway", file=sys.stderr)
            except Exception as del_exc:
                print(f"warning: failed to delete stale snapshot: {del_exc}", file=sys.stderr)
            daytona.snapshot.create(
                CreateSnapshotParams(
                    name=snapshot_name,
                    image=image,
                    resources=Resources(cpu=2, memory=4, disk=10),
                ),
                on_logs=lambda chunk: print(chunk, end=""),
            )
        else:
            raise
    print(f"\nsnapshot {snapshot_name!r} created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
