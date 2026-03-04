"""Tests for ForgeContextBroker shared coordination layer."""

import asyncio
import pytest

from forge.execution.context_broker import (
    ForgeContextBroker,
    FixProgress,
    FixStatus,
)


@pytest.fixture
def broker():
    return ForgeContextBroker()


class TestFileClaims:
    @pytest.mark.asyncio
    async def test_claim_files_no_conflict(self, broker):
        conflicts = await broker.claim_files("F-001", ["src/a.js", "src/b.js"])
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_claim_files_with_conflict(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        conflicts = await broker.claim_files("F-002", ["src/a.js", "src/c.js"])
        assert conflicts == ["src/a.js"]

    @pytest.mark.asyncio
    async def test_release_files(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        await broker.release_files("F-001")
        conflicts = await broker.claim_files("F-002", ["src/a.js"])
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_same_finding_reclaim(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        conflicts = await broker.claim_files("F-001", ["src/a.js"])
        assert conflicts == []  # Same finding can reclaim its own files


class TestFixProgress:
    @pytest.mark.asyncio
    async def test_record_completion(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        await broker.record_completion("F-001", "diff --git a/src/a.js ...", "Fixed SQL injection")
        summary = await broker.get_status_summary()
        assert summary.get("completed", 0) == 1

    @pytest.mark.asyncio
    async def test_record_failure(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        await broker.record_failure("F-001", "could not fix")
        summary = await broker.get_status_summary()
        assert summary.get("failed", 0) == 1

    @pytest.mark.asyncio
    async def test_completion_releases_files(self, broker):
        await broker.claim_files("F-001", ["src/a.js"])
        await broker.record_completion("F-001", "some diff")
        conflicts = await broker.claim_files("F-002", ["src/a.js"])
        assert conflicts == []


class TestPriorChangesContext:
    @pytest.mark.asyncio
    async def test_empty_when_no_completions(self, broker):
        ctx = await broker.get_prior_changes_context("F-001")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_includes_other_diffs(self, broker):
        await broker.record_completion("F-001", "diff content here", "Fixed X")
        ctx = await broker.get_prior_changes_context("F-002")
        assert "diff content here" in ctx
        assert "F-001" in ctx

    @pytest.mark.asyncio
    async def test_excludes_own_diffs(self, broker):
        await broker.record_completion("F-001", "my own diff")
        ctx = await broker.get_prior_changes_context("F-001")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_truncates_long_diffs(self, broker):
        long_diff = "x" * 5000
        await broker.record_completion("F-001", long_diff)
        ctx = await broker.get_prior_changes_context("F-002")
        assert "truncated" in ctx

    @pytest.mark.asyncio
    async def test_limits_to_last_5(self, broker):
        for i in range(10):
            await broker.record_completion(f"F-{i:03d}", f"diff {i}")
        ctx = await broker.get_prior_changes_context("F-999")
        # Should only contain last 5 diffs
        assert "F-005" in ctx
        assert "F-009" in ctx
        # First diffs should be excluded
        assert "F-000" not in ctx
