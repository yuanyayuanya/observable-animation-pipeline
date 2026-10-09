from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.contracts import ArtifactInput, ProducedArtifact
from app.domain.errors import ArtifactValidationError, PathBoundaryError
from app.pipeline.artifacts import ArtifactStore


def test_cache_key_is_stable_for_parameter_and_input_order(tmp_path: Path) -> None:
    first = [
        ArtifactInput("frame", tmp_path / "b.jpg", "b" * 64),
        ArtifactInput("frame", tmp_path / "a.jpg", "a" * 64),
    ]
    second = list(reversed(first))

    key_one = ArtifactStore.build_cache_key(
        "analyze_frames", "1", {"z": [2, 1], "a": {"value": True}}, first
    )
    key_two = ArtifactStore.build_cache_key(
        "analyze_frames", "1", {"a": {"value": True}, "z": [2, 1]}, second
    )

    assert key_one == key_two
    assert len(key_one) == 64


def test_runtime_boundary_and_relative_artifact_escape_are_rejected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    with pytest.raises(PathBoundaryError):
        store.require_runtime_path(tmp_path / "outside.txt")

    staging = store.prepare_staging("attempt-1")
    (staging / "ok.txt").write_text("ok", encoding="utf-8")
    with pytest.raises(PathBoundaryError):
        store.commit(
            "a" * 64,
            staging,
            [ProducedArtifact("bad", "../ok.txt", "text/plain")],
        )


def test_commit_is_atomic_and_cache_damage_is_detected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    staging = store.prepare_staging("attempt-2")
    (staging / "nested").mkdir()
    (staging / "nested" / "value.json").write_text('{"ok":true}', encoding="utf-8")
    key = "b" * 64

    records = store.commit(
        key,
        staging,
        [ProducedArtifact("metadata", "nested/value.json", "application/json")],
    )

    assert not staging.exists()
    assert records[0].path == store.artifacts_root / key / "nested" / "value.json"
    assert store.is_cache_valid(records)

    records[0].path.write_text("damaged", encoding="utf-8")
    assert not store.is_cache_valid(records)


def test_commit_rejects_missing_or_empty_artifacts(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    staging = store.prepare_staging("attempt-3")
    (staging / "empty.txt").touch()

    with pytest.raises(ArtifactValidationError):
        store.commit(
            "c" * 64,
            staging,
            [ProducedArtifact("empty", "empty.txt", "text/plain")],
        )


def test_damaged_cache_can_be_quarantined_before_recompute(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    staging = store.prepare_staging("attempt-damaged")
    (staging / "value.txt").write_text("first", encoding="utf-8")
    key = "d" * 64
    records = store.commit(
        key,
        staging,
        [ProducedArtifact("value", "value.txt", "text/plain")],
    )
    records[0].path.write_text("damaged", encoding="utf-8")

    quarantined = store.quarantine_cache(key)

    assert quarantined is not None
    assert (quarantined / "value.txt").read_text(encoding="utf-8") == "damaged"
    assert not store.cache_path(key).exists()

    replacement = store.prepare_staging("attempt-replacement")
    (replacement / "value.txt").write_text("second", encoding="utf-8")
    assert store.commit(
        key,
        replacement,
        [ProducedArtifact("value", "value.txt", "text/plain")],
    )[0].path.read_text(encoding="utf-8") == "second"
