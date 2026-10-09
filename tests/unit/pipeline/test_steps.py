from __future__ import annotations

import json
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.domain.contracts import ArtifactInput, StepContext
from app.domain.errors import RetryableStepError, StepTimeoutError
from app.pipeline.artifacts import ArtifactStore
from app.pipeline.faults import normalize_fault_plan
from app.pipeline.registry import DEFAULT_STEP_REGISTRY
from app.pipeline.steps import InjectedPermanentStepError


def _make_video(path: Path, frames: int = 16, fps: int = 8) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (160, 96)
    )
    assert writer.isOpened()
    try:
        for index in range(frames):
            image = np.zeros((96, 160, 3), dtype=np.uint8)
            image[:] = (20 + index * 3, 50, 90)
            cv2.circle(image, (20 + index * 6, 48), 12, (40, 210, 150), -1)
            writer.write(image)
    finally:
        writer.release()


def _context(
    store: ArtifactStore,
    step_key: str,
    attempt: int,
    inputs: list[ArtifactInput],
    params: dict | None = None,
    progress: list[tuple[int, str]] | None = None,
) -> StepContext:
    events = progress if progress is not None else []
    attempt_id = f"{step_key}-{attempt}"
    return StepContext(
        task_id="task-1",
        step_id=f"step-{step_key}",
        attempt_id=attempt_id,
        step_key=step_key,
        step_version="1",
        params=params or {},
        runtime_root=store.runtime_root,
        staging_dir=store.prepare_staging(attempt_id),
        input_artifacts=tuple(inputs),
        report_progress=lambda value, message: events.append((value, message)),
    )


def _commit_step(
    store: ArtifactStore,
    step_key: str,
    attempt: int,
    inputs: list[ArtifactInput],
    params: dict | None = None,
) -> list[ArtifactInput]:
    definition = DEFAULT_STEP_REGISTRY.get(step_key)
    context = _context(store, step_key, attempt, inputs, params)
    result = definition.executor.execute(context)
    cache_key = store.build_cache_key(step_key, definition.version, context.params, inputs)
    records = store.commit(cache_key, context.staging_dir, result.artifacts)
    return [
        ArtifactInput(record.artifact.kind, record.path, record.sha256) for record in records
    ]


def test_five_steps_execute_end_to_end_with_small_video(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    upload = store.runtime_root / "uploads" / "task-1" / "source.mp4"
    _make_video(upload)
    source = ArtifactInput("source_video", upload, store.sha256_file(upload))
    params = {"frame_count": 4, "analysis_profile": "test"}

    metadata = _commit_step(store, "validate_input", 1, [source], params)
    frames = _commit_step(store, "extract_frames", 1, [source, *metadata], params)
    frame_inputs = [artifact for artifact in frames if artifact.kind == "frame"]
    analysis = _commit_step(store, "analyze_frames", 1, frame_inputs, params)
    contact = _commit_step(store, "build_contact_sheet", 1, [*frame_inputs, *analysis], params)
    package = _commit_step(
        store,
        "package_result",
        1,
        [*metadata, *frame_inputs, *analysis, *contact],
        params,
    )

    assert len(frame_inputs) == 4
    analysis_payload = json.loads(analysis[0].path.read_text(encoding="utf-8"))
    assert len(analysis_payload["frames"]) == 4
    assert contact[0].path.name == "contact-sheet.jpg"
    archive = next(artifact.path for artifact in package if artifact.kind == "result_package")
    with zipfile.ZipFile(archive) as result_zip:
        names = result_zip.namelist()
    assert "manifest.json" in names
    assert any(name.endswith("contact-sheet.jpg") for name in names)


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("temporary_failure_once", RetryableStepError),
        ("timeout_once", StepTimeoutError),
    ],
)
def test_analysis_fault_is_injected_only_once(
    tmp_path: Path, mode: str, error_type: type[Exception]
) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    frames: list[ArtifactInput] = []
    for index in range(4):
        path = store.runtime_root / "artifacts" / "seed" / f"frame-{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), np.full((32, 48, 3), 30 + index, dtype=np.uint8))
        frames.append(ArtifactInput("frame", path, store.sha256_file(path)))
    params = {
        "analysis_profile": "test",
        "fault_plan": {"step": "analyze_frames", "mode": mode, "at_progress": 25},
    }
    executor = DEFAULT_STEP_REGISTRY.get("analyze_frames").executor

    first = _context(store, "analyze_frames", 1, frames, params)
    with pytest.raises(error_type):
        executor.execute(first)

    second = _context(store, "analyze_frames", 2, frames, params)
    result = executor.execute(second)
    assert result.metrics["frames_analyzed"] == 4


def test_registry_has_fixed_order_weights_versions_and_retries() -> None:
    assert DEFAULT_STEP_REGISTRY.keys == (
        "validate_input",
        "extract_frames",
        "analyze_frames",
        "build_contact_sheet",
        "package_result",
    )
    assert [step.weight for step in DEFAULT_STEP_REGISTRY] == [5, 20, 45, 20, 10]
    assert [step.max_retries for step in DEFAULT_STEP_REGISTRY] == [0, 1, 2, 1, 1]
    assert all(step.version == "1" for step in DEFAULT_STEP_REGISTRY)


def test_documented_api_fault_strings_are_normalized() -> None:
    analysis_failure = normalize_fault_plan("analyze_fail_once_at_50", "analyze_frames")
    analysis_timeout = normalize_fault_plan("analyze_timeout_once", "analyze_frames")
    contact_failure = normalize_fault_plan(
        "contact_sheet_permanent_fail", "build_contact_sheet"
    )

    assert analysis_failure is not None
    assert (analysis_failure.mode, analysis_failure.at_progress) == (
        "temporary_failure_once",
        50,
    )
    assert analysis_timeout is not None and analysis_timeout.mode == "timeout_once"
    assert contact_failure is not None and contact_failure.mode == "permanent_fail"
    assert normalize_fault_plan("analyze_timeout_once", "build_contact_sheet") is None


@pytest.mark.parametrize(
    ("api_value", "error_type"),
    [
        ("analyze_fail_once_at_50", RetryableStepError),
        ("analyze_timeout_once", StepTimeoutError),
    ],
)
def test_documented_analysis_fault_strings_execute_once(
    tmp_path: Path, api_value: str, error_type: type[Exception]
) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    frames: list[ArtifactInput] = []
    for index in range(4):
        path = store.runtime_root / "artifacts" / "api-seed" / f"frame-{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), np.full((32, 48, 3), 60 + index, dtype=np.uint8))
        frames.append(ArtifactInput("frame", path, store.sha256_file(path)))
    executor = DEFAULT_STEP_REGISTRY.get("analyze_frames").executor
    params = {"fault_plan": api_value}

    with pytest.raises(error_type):
        executor.execute(_context(store, "analyze_frames", 1, frames, params))
    assert executor.execute(
        _context(store, "analyze_frames", 2, frames, params)
    ).metrics["frames_analyzed"] == 4


def test_documented_contact_sheet_failure_is_non_retryable(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runtime")
    executor = DEFAULT_STEP_REGISTRY.get("build_contact_sheet").executor
    context = _context(
        store,
        "build_contact_sheet",
        1,
        [],
        {"fault_plan": "contact_sheet_permanent_fail"},
    )

    with pytest.raises(InjectedPermanentStepError) as error:
        executor.execute(context)
    assert error.value.retryable is False
