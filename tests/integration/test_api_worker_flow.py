from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.worker.runner import Worker


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _write_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        8,
        (320, 180),
    )
    assert writer.isOpened()
    try:
        for index in range(16):
            frame = np.full((180, 320, 3), (24, 30, 44), dtype=np.uint8)
            cv2.circle(frame, (40 + index * 12, 90), 24, (40, 180, 120), -1)
            writer.write(frame)
    finally:
        writer.release()


def test_api_worker_pipeline_and_cache_rerun(tmp_path: Path) -> None:
    source = tmp_path / "demo.mp4"
    _write_video(source)
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        enable_fault_injection=True,
        lease_seconds=30,
        heartbeat_seconds=5,
    ).validated()
    app = create_app(settings)

    try:
        with TestClient(app) as client, source.open("rb") as handle:
            response = client.post(
                "/api/tasks",
                files={"file": ("demo.mp4", handle, "video/mp4")},
                data={
                    "frame_count": "4",
                    "analysis_profile": "default",
                    "fault_plan": "none",
                    "idempotency_key": "e2e-normal",
                },
            )
            assert response.status_code == 201, response.text
            task_id = response.json()["task_id"]

            worker = Worker(
                app.state.repository,
                settings,
                worker_id="integration-worker",
            )
            assert [worker.run_once() for _ in range(5)] == [True] * 5

            detail = client.get(f"/api/tasks/{task_id}")
            assert detail.status_code == 200
            payload = detail.json()
            assert payload["status"] == "SUCCEEDED"
            assert payload["progress"] == 100
            assert [step["status"] for step in payload["steps"]] == [
                "SUCCEEDED"
            ] * 5
            package = next(
                item for item in payload["artifacts"]
                if item["kind"] == "result_package"
            )
            download = client.get(package["download_url"])
            assert download.status_code == 200
            assert download.content.startswith(b"PK")

            events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
            assert events[0]["event_type"] == "task_created"
            assert any(item["event_type"] == "step_succeeded" for item in events)

            rerun = client.post(f"/api/tasks/{task_id}/rerun")
            assert rerun.status_code == 200
            rerun_id = rerun.json()["task_id"]
            assert [worker.run_once() for _ in range(5)] == [True] * 5
            rerun_detail = client.get(f"/api/tasks/{rerun_id}").json()
            assert rerun_detail["status"] == "SUCCEEDED"
            assert [step["status"] for step in rerun_detail["steps"]] == [
                "SKIPPED"
            ] * 5
            rerun_events = client.get(
                f"/api/tasks/{rerun_id}/events"
            ).json()["items"]
            assert sum(item["event_type"] == "cache_hit" for item in rerun_events) == 5
    finally:
        app.state.engine.dispose()


def test_retryable_analysis_failure_resumes_without_repeating_prior_steps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retry-demo.mp4"
    _write_video(source)
    settings = Settings(
        runtime_root=tmp_path / "runtime-retry",
        enable_fault_injection=True,
        lease_seconds=30,
        heartbeat_seconds=5,
    ).validated()
    app = create_app(settings)
    clock = MutableClock()

    try:
        with TestClient(app) as client, source.open("rb") as handle:
            created = client.post(
                "/api/tasks",
                files={"file": ("retry-demo.mp4", handle, "video/mp4")},
                data={
                    "frame_count": "4",
                    "analysis_profile": "default",
                    "fault_plan": "analyze_fail_once_at_50",
                },
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["task_id"]
            worker = Worker(
                app.state.repository,
                settings,
                worker_id="retry-worker",
                clock=clock,
            )

            assert worker.run_once()  # validate
            assert worker.run_once()  # extract
            assert worker.run_once()  # analyze fails once
            failed_once = client.get(f"/api/tasks/{task_id}").json()
            analyze = next(
                step for step in failed_once["steps"]
                if step["key"] == "analyze_frames"
            )
            assert analyze["status"] == "RETRY_WAIT"
            assert analyze["attempt_count"] == 1

            clock.value = datetime.fromisoformat(analyze["next_run_at"]) + timedelta(
                seconds=1
            )
            assert worker.run_once()  # analyze retry
            assert worker.run_once()  # contact sheet
            assert worker.run_once()  # package

            finished = client.get(f"/api/tasks/{task_id}").json()
            assert finished["status"] == "SUCCEEDED"
            attempts = {
                step["key"]: step["attempt_count"] for step in finished["steps"]
            }
            assert attempts == {
                "validate_input": 1,
                "extract_frames": 1,
                "analyze_frames": 2,
                "build_contact_sheet": 1,
                "package_result": 1,
            }
            analyze_attempts = next(
                step["attempts"] for step in finished["steps"]
                if step["key"] == "analyze_frames"
            )
            assert [item["status"] for item in analyze_attempts] == [
                "FAILED",
                "SUCCEEDED",
            ]
    finally:
        app.state.engine.dispose()


def test_manual_retry_resumes_a_non_retryable_step(tmp_path: Path) -> None:
    source = tmp_path / "manual-retry.mp4"
    _write_video(source)
    settings = Settings(
        runtime_root=tmp_path / "runtime-manual-retry",
        enable_fault_injection=True,
        lease_seconds=30,
        heartbeat_seconds=5,
    ).validated()
    app = create_app(settings)
    clock = MutableClock()

    try:
        with TestClient(app) as client, source.open("rb") as handle:
            created = client.post(
                "/api/tasks",
                files={"file": ("manual-retry.mp4", handle, "video/mp4")},
                data={
                    "frame_count": "4",
                    "analysis_profile": "default",
                    "fault_plan": "contact_sheet_permanent_fail",
                },
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["task_id"]
            worker = Worker(
                app.state.repository,
                settings,
                worker_id="manual-retry-worker",
                clock=clock,
            )

            assert [worker.run_once() for _ in range(4)] == [True] * 4
            failed = client.get(f"/api/tasks/{task_id}").json()
            contact = next(
                step for step in failed["steps"]
                if step["key"] == "build_contact_sheet"
            )
            assert failed["status"] == "FAILED"
            assert contact["status"] == "FAILED"
            assert contact["attempt_count"] == 1

            retried = client.post(
                f"/api/tasks/{task_id}/steps/build_contact_sheet/retry",
                headers={"Idempotency-Key": "manual-retry-1"},
            )
            assert retried.status_code == 200, retried.text
            assert [worker.run_once() for _ in range(2)] == [True] * 2

            finished = client.get(f"/api/tasks/{task_id}").json()
            contact = next(
                step for step in finished["steps"]
                if step["key"] == "build_contact_sheet"
            )
            assert finished["status"] == "SUCCEEDED"
            assert contact["status"] == "SUCCEEDED"
            assert contact["attempt_count"] == 2
            assert [item["status"] for item in contact["attempts"]] == [
                "FAILED",
                "SUCCEEDED",
            ]
    finally:
        app.state.engine.dispose()
