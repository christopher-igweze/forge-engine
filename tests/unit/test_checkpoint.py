"""Tests for checkpoint save/load/restore round-trips."""

import json
import os

import pytest

from forge.execution.checkpoint import (
    CheckpointPhase,
    ForgeCheckpoint,
    clear_checkpoints,
    get_latest_checkpoint,
    load_checkpoint,
    restore_state,
    save_checkpoint,
)
from forge.schemas import (
    AuditFinding,
    CodebaseMap,
    FileEntry,
    FindingCategory,
    FindingSeverity,
    FindingLocation,
    ForgeExecutionState,
)


@pytest.fixture
def state_with_data(tmp_path):
    state = ForgeExecutionState(
        repo_path=str(tmp_path),
        artifacts_dir=str(tmp_path / "artifacts"),
        codebase_map=CodebaseMap(
            files=[FileEntry(path="src/app.ts", language="typescript", loc=100)],
            loc_total=100,
            file_count=1,
            primary_language="typescript",
        ),
    )
    state.all_findings.append(
        AuditFinding(
            id="F-test0001",
            title="Test finding",
            description="Test description",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            locations=[FindingLocation(file_path="src/app.ts")],
        )
    )
    return state


class TestCheckpointRoundTrip:
    def test_save_and_load(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp = load_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY)
        assert cp is not None
        assert cp.forge_run_id == state_with_data.forge_run_id
        assert cp.phase == CheckpointPhase.DISCOVERY
        assert len(cp.all_findings) == 1
        assert cp.all_findings[0]["title"] == "Test finding"

    def test_load_nonexistent(self, tmp_path):
        cp = load_checkpoint(str(tmp_path), CheckpointPhase.VALIDATION)
        assert cp is None

    def test_restore_state(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp = load_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY)
        restored = restore_state(cp)
        assert restored.forge_run_id == state_with_data.forge_run_id
        assert len(restored.all_findings) == 1
        assert restored.all_findings[0].title == "Test finding"
        assert restored.codebase_map is not None
        assert restored.codebase_map.loc_total == 100

    def test_restore_preserves_finding_fields(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp = load_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY)
        restored = restore_state(cp)
        finding = restored.all_findings[0]
        assert finding.id == "F-test0001"
        assert finding.category == FindingCategory.SECURITY
        assert finding.severity == FindingSeverity.HIGH
        assert len(finding.locations) == 1
        assert finding.locations[0].file_path == "src/app.ts"

    def test_restore_preserves_codebase_map(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp = load_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY)
        restored = restore_state(cp)
        assert restored.codebase_map.file_count == 1
        assert restored.codebase_map.primary_language == "typescript"
        assert len(restored.codebase_map.files) == 1
        assert restored.codebase_map.files[0].path == "src/app.ts"

    def test_checkpoint_file_is_valid_json(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp_path = os.path.join(
            str(tmp_path), ".forge-checkpoints", "discovery_complete.json"
        )
        assert os.path.isfile(cp_path)
        with open(cp_path) as f:
            data = json.load(f)
        assert data["phase"] == "discovery_complete"
        assert data["forge_run_id"] == state_with_data.forge_run_id


class TestGetLatestCheckpoint:
    def test_returns_latest_phase(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        save_checkpoint(str(tmp_path), CheckpointPhase.TRIAGE, state_with_data)
        cp = get_latest_checkpoint(str(tmp_path))
        assert cp is not None
        assert cp.phase == CheckpointPhase.TRIAGE

    def test_returns_none_when_empty(self, tmp_path):
        cp = get_latest_checkpoint(str(tmp_path))
        assert cp is None

    def test_returns_only_existing(self, tmp_path, state_with_data):
        """If only DISCOVERY exists, returns DISCOVERY even though TRIAGE is later."""
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp = get_latest_checkpoint(str(tmp_path))
        assert cp is not None
        assert cp.phase == CheckpointPhase.DISCOVERY


class TestClearCheckpoints:
    def test_clears_all(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        save_checkpoint(str(tmp_path), CheckpointPhase.TRIAGE, state_with_data)
        clear_checkpoints(str(tmp_path))
        assert get_latest_checkpoint(str(tmp_path)) is None

    def test_clear_nonexistent_dir(self, tmp_path):
        # Should not raise
        clear_checkpoints(str(tmp_path))

    def test_clear_removes_directory(self, tmp_path, state_with_data):
        save_checkpoint(str(tmp_path), CheckpointPhase.DISCOVERY, state_with_data)
        cp_dir = os.path.join(str(tmp_path), ".forge-checkpoints")
        assert os.path.isdir(cp_dir)
        clear_checkpoints(str(tmp_path))
        assert not os.path.isdir(cp_dir)
