from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import demo_acceptance


def test_parse_args_defaults_to_all_and_local_sample() -> None:
    args = demo_acceptance.parse_args([])

    assert args.scenario == "all"
    assert args.video == demo_acceptance.DEFAULT_VIDEO
    assert args.timeout == 90.0
    assert args.poll_interval == 0.5
    assert args.frame_count == 8
    assert demo_acceptance.API_BASE_URL == "http://127.0.0.1:8000"


def test_parse_args_accepts_each_original_scenario(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"

    for scenario in ("normal", "auto-retry", "worker-crash", "cache-hit"):
        args = demo_acceptance.parse_args(
            [
                scenario,
                "--video",
                str(video),
                "--timeout",
                "12.5",
                "--poll-interval",
                "0.1",
                "--frame-count",
                "4",
            ]
        )
        assert args.scenario == scenario
        assert args.video == video
        assert args.timeout == 12.5
        assert args.poll_interval == 0.1
        assert args.frame_count == 4


@pytest.mark.parametrize(
    "arguments",
    (
        ["normal", "--timeout", "0"],
        ["normal", "--poll-interval", "-1"],
        ["normal", "--frame-count", "13"],
        ["mobile"],
    ),
)
def test_parse_args_rejects_unsafe_or_invalid_values(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        demo_acceptance.parse_args(arguments)


def test_encode_multipart_contains_fields_and_video(tmp_path: Path) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video-bytes")

    body, content_type = demo_acceptance.encode_multipart(
        {"frame_count": "8", "fault_plan": "none"},
        "file",
        video,
        boundary="fixed-boundary",
    )

    assert content_type == "multipart/form-data; boundary=fixed-boundary"
    assert b'name="frame_count"\r\n\r\n8' in body
    assert b'name="fault_plan"\r\n\r\nnone' in body
    assert b'name="file"; filename="sample.mp4"' in body
    assert b"Content-Type: video/mp4" in body
    assert b"video-bytes" in body
    assert body.endswith(b"--fixed-boundary--\r\n")


def test_attempt_and_event_evidence_helpers() -> None:
    task = {
        "id": "tsk_demo",
        "steps": [
            {
                "key": "analyze_frames",
                "attempts": [{"status": "FAILED"}, {"status": "SUCCEEDED"}],
            }
        ],
    }
    events = [
        {"event_type": "retry_scheduled"},
        {"event_type": "retry_scheduled"},
        {"event_type": "step_succeeded"},
    ]

    assert demo_acceptance.attempt_statuses(task, "analyze_frames") == [
        "FAILED",
        "SUCCEEDED",
    ]
    assert demo_acceptance.event_count(events, "retry_scheduled") == 2
    with pytest.raises(demo_acceptance.DemoError, match="no package_result step"):
        demo_acceptance.task_step(task, "package_result")


class FakeProcess:
    def __init__(self, *, running: bool = True, timeout_once: bool = False) -> None:
        self.running = running
        self.timeout_once = timeout_once
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        self.running = False
        return 0

    def kill(self) -> None:
        self.killed = True


def test_stop_owned_process_terminates_only_a_running_child() -> None:
    running = FakeProcess()
    finished = FakeProcess(running=False)

    demo_acceptance.stop_owned_process(running)
    demo_acceptance.stop_owned_process(finished)
    demo_acceptance.stop_owned_process(None)

    assert running.terminated is True
    assert running.killed is False
    assert running.wait_calls == 1
    assert finished.terminated is False


def test_stop_owned_process_kills_child_after_timeout() -> None:
    process = FakeProcess(timeout_once=True)

    demo_acceptance.stop_owned_process(process, timeout=0.01)

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
