"""Shared coordination layer for parallel remediation agents.

Provides file-level claims, fix progress tracking, and accumulated
diff context so parallel coders can coordinate without conflicts.

In-memory for now — designed for easy Redis upgrade later.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class FixStatus(str, Enum):
    """Status of a fix in the shared context."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DEFERRED = "deferred"


@dataclass
class FixProgress:
    """Progress record for a single finding's fix."""
    finding_id: str
    status: FixStatus = FixStatus.PENDING
    claimed_files: list[str] = field(default_factory=list)
    diff: str = ""
    summary: str = ""


class ForgeContextBroker:
    """Shared coordination layer for parallel remediation agents.

    Thread-safe via asyncio.Lock for concurrent coroutine access.
    All methods are async to allow future Redis/external store upgrade.
    """

    def __init__(self) -> None:
        self._file_claims: dict[str, str] = {}  # file_path → finding_id
        self._lock = asyncio.Lock()
        self._fixes: dict[str, FixProgress] = {}  # finding_id → FixProgress
        self._completed_diffs: list[tuple[str, str]] = []  # [(finding_id, diff)]

    async def claim_files(self, finding_id: str, files: list[str]) -> list[str]:
        """Claim files for exclusive modification.

        Returns list of file paths that are already claimed by other findings.
        Caller should adapt its approach for conflicted files.
        """
        conflicts = []
        async with self._lock:
            for f in files:
                existing = self._file_claims.get(f)
                if existing and existing != finding_id:
                    conflicts.append(f)
                else:
                    self._file_claims[f] = finding_id

            # Track progress
            if finding_id not in self._fixes:
                self._fixes[finding_id] = FixProgress(finding_id=finding_id)
            self._fixes[finding_id].status = FixStatus.IN_PROGRESS
            self._fixes[finding_id].claimed_files = [
                f for f in files if f not in conflicts
            ]

        if conflicts:
            logger.info(
                "Context broker: %s has file conflicts with: %s",
                finding_id, conflicts,
            )
        return conflicts

    async def release_files(self, finding_id: str) -> None:
        """Release all file claims for a finding."""
        async with self._lock:
            to_remove = [
                f for f, fid in self._file_claims.items()
                if fid == finding_id
            ]
            for f in to_remove:
                del self._file_claims[f]

    async def record_completion(
        self, finding_id: str, diff: str, summary: str = ""
    ) -> None:
        """Record a completed fix with its diff for downstream context."""
        async with self._lock:
            if finding_id in self._fixes:
                self._fixes[finding_id].status = FixStatus.COMPLETED
                self._fixes[finding_id].diff = diff
                self._fixes[finding_id].summary = summary
            self._completed_diffs.append((finding_id, diff))

            # Release file claims
            to_remove = [
                f for f, fid in self._file_claims.items()
                if fid == finding_id
            ]
            for f in to_remove:
                del self._file_claims[f]

    async def record_failure(self, finding_id: str, reason: str = "") -> None:
        """Record a failed fix attempt."""
        async with self._lock:
            if finding_id in self._fixes:
                self._fixes[finding_id].status = FixStatus.FAILED
                self._fixes[finding_id].summary = reason

            # Release file claims
            to_remove = [
                f for f, fid in self._file_claims.items()
                if fid == finding_id
            ]
            for f in to_remove:
                del self._file_claims[f]

    async def get_prior_changes_context(self, finding_id: str) -> str:
        """Get accumulated context from completed fixes for a coder.

        Returns a formatted string showing what other coders have already
        changed, so this coder can avoid conflicts and build on their work.
        """
        async with self._lock:
            if not self._completed_diffs:
                return ""

            parts = []
            for fid, diff in self._completed_diffs:
                if fid == finding_id:
                    continue  # Don't include own previous attempts
                if diff:
                    # Truncate long diffs
                    d = diff[:3000]
                    if len(diff) > 3000:
                        d += "\n... (truncated)"
                    parts.append(f"### Fix {fid}\n```diff\n{d}\n```")

            if not parts:
                return ""

            return (
                "The following fixes have already been applied by other agents. "
                "Your changes must be compatible with these modifications:\n\n"
                + "\n\n".join(parts[-5:])  # Last 5 diffs max to limit context
            )

    async def get_claimed_files(self) -> dict[str, str]:
        """Get current file claims (for debugging/logging)."""
        async with self._lock:
            return dict(self._file_claims)

    async def get_status_summary(self) -> dict[str, int]:
        """Get summary of fix statuses."""
        async with self._lock:
            counts: dict[str, int] = {}
            for fp in self._fixes.values():
                key = fp.status.value
                counts[key] = counts.get(key, 0) + 1
            return counts
