from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.worker.runner import Worker


def _settings(runtime_root: Path) -> Settings:
    return Settings(
        runtime_root=runtime_root,
        enable_fault_injection=True,
        lease_seconds=30,
        heartbeat_seconds=5,
    ).validated()


def _write_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        8,
        (160, 90),
    )
    assert writer.isOpened()
    try:
        for index in range(12):
            frame = np.full((90, 160, 3), (24, 30, 44), dtype=np.uint8)
            cv2.circle(frame, (24 + index * 8, 45), 12, (40, 180, 120), -1)
            writer.write(frame)
    finally:
        writer.release()


def _create_task(
    client: TestClient,
    source: Path,
    *,
    idempotency_key: str,
) -> str:
    with source.open("rb") as handle:
        response = client.post(
            "/api/tasks",
            files={"file": (source.name, handle, "video/mp4")},
            data={
                "frame_count": "4",
                "analysis_profile": "default",
                "fault_plan": "none",
                "idempotency_key": idempotency_key,
            },
        )
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def _step(payload: dict[str, object], key: str) -> dict[str, object]:
    steps = payload["steps"]
    assert isinstance(steps, list)
    return next(step for step in steps if step["key"] == key)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_api_restart_preserves_task_and_worker_can_continue(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "restart-runtime"
    source = tmp_path / "restart.mp4"
    _write_video(source)
    settings = _settings(runtime_root)

    first_app = create_app(settings)
    try:
        with TestClient(first_app) as client:
            task_id = _create_task(
                client,
                source,
                idempotency_key="stage3-api-restart",
            )
            first_worker = Worker(
                first_app.state.repository,
                settings,
                worker_id="stage3-before-restart",
            )
            assert first_worker.run_once()
            before_restart = client.get(f"/api/tasks/{task_id}").json()
            assert _step(before_restart, "validate_input")["status"] == "SUCCEEDED"
    finally:
        first_app.state.engine.dispose()

    second_app = create_app(settings)
    try:
        with TestClient(second_app) as client:
            listing = client.get("/api/tasks")
            assert listing.status_code == 200
            assert [item["id"] for item in listing.json()["items"]] == [task_id]

            after_restart = client.get(f"/api/tasks/{task_id}")
            assert after_restart.status_code == 200
            assert _step(after_restart.json(), "validate_input")["status"] == "SUCCEEDED"

            second_worker = Worker(
                second_app.state.repository,
                settings,
                worker_id="stage3-after-restart",
            )
            assert [second_worker.run_once() for _ in range(4)] == [True] * 4

            finished = client.get(f"/api/tasks/{task_id}").json()
            assert finished["status"] == "SUCCEEDED"
            assert finished["progress"] == 100
            assert [step["attempt_count"] for step in finished["steps"]] == [1] * 5

            events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
            assert sum(event["event_type"] == "task_created" for event in events) == 1
            assert [event["sequence"] for event in events] == sorted(
                event["sequence"] for event in events
            )
    finally:
        second_app.state.engine.dispose()


def test_corrupt_mp4_fails_validation_once_without_retry(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"this is not an mp4 container")
    settings = _settings(tmp_path / "corrupt-runtime")
    app = create_app(settings)

    try:
        with TestClient(app) as client:
            task_id = _create_task(
                client,
                corrupt,
                idempotency_key="stage3-corrupt-input",
            )
            worker = Worker(
                app.state.repository,
                settings,
                worker_id="stage3-corrupt-worker",
            )

            assert worker.run_once()
            assert not worker.run_once()

            failed = client.get(f"/api/tasks/{task_id}").json()
            validation = _step(failed, "validate_input")
            assert failed["status"] == "FAILED"
            assert validation["status"] == "FAILED"
            assert validation["attempt_count"] == 1
            assert validation["max_attempts"] == 1
            assert len(validation["attempts"]) == 1
            assert validation["attempts"][0]["status"] == "FAILED"
            assert validation["attempts"][0]["error_type"] == "PERMANENT_INPUT"
            assert validation["next_run_at"] is None

            events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
            assert not any(event["event_type"] == "retry_scheduled" for event in events)
            failure = next(
                event for event in events if event["event_type"] == "step_failed"
            )
            assert failure["payload"] == {
                "error_type": "PERMANENT_INPUT",
                "retryable": False,
            }
    finally:
        app.state.engine.dispose()


def test_damaged_cache_is_quarantined_recomputed_and_recorded(
    tmp_path: Path,
) -> None:
    # Keep the runtime suffix short because quarantine names include two hashes,
    # which can otherwise exceed the legacy Windows path limit under pytest.
    runtime_root = tmp_path / "r"
    source = tmp_path / "v.mp4"
    _write_video(source)
    settings = _settings(runtime_root)
    app = create_app(settings)

    try:
        with TestClient(app) as client:
            source_task_id = _create_task(
                client,
                source,
                idempotency_key="stage3-cache-source",
            )
            worker = Worker(
                app.state.repository,
                settings,
                worker_id="stage3-cache-worker",
            )
            assert [worker.run_once() for _ in range(5)] == [True] * 5

            source_detail = app.state.repository.get_task_detail(source_task_id)
            assert source_detail is not None
            cached = next(
                artifact
                for artifact in source_detail.artifacts
                if artifact.kind == "input_metadata"
            )
            cached_path = runtime_root / cached.relative_path
            original_sha256 = cached.sha256
            cached_path.write_bytes(b"damaged cache payload")
            assert _sha256(cached_path) != original_sha256

            rerun = client.post(f"/api/tasks/{source_task_id}/rerun")
            assert rerun.status_code == 200, rerun.text
            rerun_id = rerun.json()["task_id"]
            assert [worker.run_once() for _ in range(5)] == [True] * 5

            rerun_detail = client.get(f"/api/tasks/{rerun_id}").json()
            assert rerun_detail["status"] == "SUCCEEDED"
            assert _step(rerun_detail, "validate_input")["status"] == "SUCCEEDED"
            assert [
                _step(rerun_detail, key)["status"]
                for key in (
                    "extract_frames",
                    "analyze_frames",
                    "build_contact_sheet",
                    "package_result",
                )
            ] == ["SKIPPED"] * 4

            events = client.get(f"/api/tasks/{rerun_id}/events").json()["items"]
            invalidated = [
                event for event in events if event["event_type"] == "cache_invalidated"
            ]
            assert len(invalidated) == 1
            assert invalidated[0]["payload"]["cache_key"] == cached.cache_key
            assert sum(event["event_type"] == "cache_hit" for event in events) == 4

            quarantined = list(
                (runtime_root / "quarantine").glob(f"{cached.cache_key}-*")
            )
            assert len(quarantined) == 1
            quarantined_payload = quarantined[0] / cached_path.name
            assert quarantined_payload.read_bytes() == b"damaged cache payload"
            assert cached_path.is_file()
            assert _sha256(cached_path) == original_sha256
    finally:
        app.state.engine.dispose()
