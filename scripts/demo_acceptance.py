from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4


API_BASE_URL = "http://127.0.0.1:8000"
TERMINAL_TASK_STATUSES = {"SUCCEEDED", "FAILED", "PAUSED"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = PROJECT_ROOT / ".runtime" / "temp" / "sample.mp4"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / ".runtime"


class DemoError(RuntimeError):
    """A concise, user-facing acceptance failure."""


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the four local acceptance demos against the API fixed at "
            f"{API_BASE_URL}."
        ),
        epilog=(
            "The worker-crash demo waits for the injected crash marker before "
            "starting one replacement Worker. It stops only that subprocess on "
            "success, timeout, or Ctrl+C; pre-existing API/Worker processes are "
            "never stopped. No LAN or mobile access is enabled."
        ),
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default="all",
        choices=("normal", "auto-retry", "worker-crash", "cache-hit", "all"),
        help="scenario to run (default: all)",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=DEFAULT_VIDEO,
        help=f"MP4/MOV input (default: {DEFAULT_VIDEO})",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=90.0,
        help="maximum seconds for each task to reach its expected result (default: 90)",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive_float,
        default=0.5,
        help="task polling interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--frame-count",
        type=int,
        choices=range(4, 13),
        default=8,
        metavar="4..12",
        help="number of frames to extract (default: 8)",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def encode_multipart(
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    token = boundary or f"----observable-pipeline-{uuid4().hex}"
    newline = b"\r\n"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                f"--{token}".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"),
                b"",
                value.encode("utf-8"),
            )
        )
    media_type = "video/quicktime" if file_path.suffix.lower() == ".mov" else "video/mp4"
    safe_name = file_path.name.replace('"', "_")
    chunks.extend(
        (
            f"--{token}".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_name}"'
            ).encode("utf-8"),
            f"Content-Type: {media_type}".encode("ascii"),
            b"",
            file_path.read_bytes(),
            f"--{token}--".encode("ascii"),
            b"",
        )
    )
    return newline.join(chunks), f"multipart/form-data; boundary={token}"


def request_json(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        data=body,
        headers={"Accept": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise DemoError(f"API {method} {path} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise DemoError(f"cannot reach {API_BASE_URL}: {error.reason}") from error
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise DemoError(f"API {method} {path} returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise DemoError(f"API {method} {path} returned a non-object JSON value")
    return decoded


def create_task(
    video: Path,
    *,
    frame_count: int,
    fault_plan: str,
    profile: str,
) -> str:
    body, content_type = encode_multipart(
        {
            "frame_count": str(frame_count),
            "analysis_profile": profile,
            "fault_plan": fault_plan,
            "idempotency_key": f"acceptance-{uuid4().hex}",
        },
        "file",
        video,
    )
    created = request_json(
        "POST",
        "/api/tasks",
        body=body,
        headers={"Content-Type": content_type},
        timeout=30.0,
    )
    task_id = created.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise DemoError("task creation response did not include task_id")
    return task_id


def get_task(task_id: str) -> dict[str, Any]:
    return request_json("GET", f"/api/tasks/{task_id}")


def get_events(task_id: str) -> list[dict[str, Any]]:
    payload = request_json("GET", f"/api/tasks/{task_id}/events?limit=1000")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise DemoError("events response did not contain an items list")
    return [item for item in items if isinstance(item, dict)]


def task_step(task: dict[str, Any], key: str) -> dict[str, Any]:
    for step in task.get("steps", []):
        if step.get("key") == key:
            return step
    raise DemoError(f"task {task.get('id', '<unknown>')} has no {key} step")


def attempt_statuses(task: dict[str, Any], step_key: str) -> list[str]:
    return [
        str(attempt.get("status"))
        for attempt in task_step(task, step_key).get("attempts", [])
    ]


def event_count(events: Iterable[dict[str, Any]], event_type: str) -> int:
    return sum(event.get("event_type") == event_type for event in events)


def wait_for_task(
    task_id: str,
    *,
    timeout: float,
    poll_interval: float,
    observer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: tuple[str, int] | None = None
    while time.monotonic() < deadline:
        task = get_task(task_id)
        current = (str(task.get("status")), int(task.get("progress", 0)))
        if current != last_status:
            print(f"  task={task_id} status={current[0]} progress={current[1]}%")
            last_status = current
        if observer is not None:
            observer(task)
        if current[0] in TERMINAL_TASK_STATUSES:
            return task
        time.sleep(poll_interval)
    raise DemoError(f"task {task_id} did not finish within {timeout:g}s")


def print_attempt_evidence(task: dict[str, Any]) -> None:
    for step in task.get("steps", []):
        statuses = [str(item.get("status")) for item in step.get("attempts", [])]
        evidence = ",".join(statuses) if statuses else "none"
        print(
            f"    {step.get('key')}: {step.get('status')} "
            f"attempts={step.get('attempt_count')} [{evidence}]"
        )


def require_succeeded(task: dict[str, Any], label: str) -> None:
    if task.get("status") != "SUCCEEDED":
        print_attempt_evidence(task)
        raise DemoError(f"{label} ended as {task.get('status')}, expected SUCCEEDED")


def unique_profile(label: str) -> str:
    return f"acceptance-{label}-{uuid4().hex[:10]}"


def run_normal(args: argparse.Namespace) -> None:
    print("[normal] creating an uncached task")
    task_id = create_task(
        args.video,
        frame_count=args.frame_count,
        fault_plan="none",
        profile=unique_profile("normal"),
    )
    task = wait_for_task(task_id, timeout=args.timeout, poll_interval=args.poll_interval)
    require_succeeded(task, "normal scenario")
    statuses = [step.get("status") for step in task.get("steps", [])]
    if statuses != ["SUCCEEDED"] * 5:
        raise DemoError(f"normal scenario expected five executed steps, got {statuses}")
    print_attempt_evidence(task)
    print("[normal] PASS: five steps executed successfully")


def run_auto_retry(args: argparse.Namespace) -> None:
    print("[auto-retry] injecting one retryable analysis failure")
    task_id = create_task(
        args.video,
        frame_count=args.frame_count,
        fault_plan="analyze_fail_once_at_50",
        profile=unique_profile("retry"),
    )
    task = wait_for_task(task_id, timeout=args.timeout, poll_interval=args.poll_interval)
    require_succeeded(task, "auto-retry scenario")
    statuses = attempt_statuses(task, "analyze_frames")
    if statuses != ["FAILED", "SUCCEEDED"]:
        raise DemoError(f"auto-retry evidence mismatch: analyze attempts={statuses}")
    events = get_events(task_id)
    if event_count(events, "retry_scheduled") < 1:
        raise DemoError("auto-retry scenario has no retry_scheduled event")
    print_attempt_evidence(task)
    print("[auto-retry] PASS: analyze attempts=[FAILED,SUCCEEDED], retry event recorded")


def replacement_worker_environment() -> dict[str, str]:
    runtime_root = Path(os.getenv("APP_RUNTIME_ROOT", str(DEFAULT_RUNTIME_ROOT))).resolve()
    temp_root = runtime_root / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "APP_RUNTIME_ROOT": str(runtime_root),
        "APP_ENABLE_FAULT_INJECTION": "1",
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
    }


def start_replacement_worker() -> subprocess.Popen[bytes]:
    print("  crash marker observed; starting one replacement Worker")
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=PROJECT_ROOT,
        env=replacement_worker_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_owned_process(process: Any | None, *, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def run_worker_crash(args: argparse.Namespace) -> None:
    print("[worker-crash] injecting Worker exit during analysis")
    task_id = create_task(
        args.video,
        frame_count=args.frame_count,
        fault_plan="worker_exit_at_40",
        profile=unique_profile("crash"),
    )
    runtime_root = Path(os.getenv("APP_RUNTIME_ROOT", str(DEFAULT_RUNTIME_ROOT))).resolve()
    marker = runtime_root / "temp" / "faults" / f"{task_id}-worker-exit.used"
    replacement: subprocess.Popen[bytes] | None = None
    marker_observed = False

    def observe(task: dict[str, Any]) -> None:
        nonlocal replacement, marker_observed
        if marker.exists():
            marker_observed = True
            if replacement is None and task.get("status") not in TERMINAL_TASK_STATUSES:
                replacement = start_replacement_worker()

    try:
        task = wait_for_task(
            task_id,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            observer=observe,
        )
        require_succeeded(task, "worker-crash scenario")
        if not marker_observed:
            raise DemoError("worker-crash marker was not observed; fault injection may be disabled")
        statuses = attempt_statuses(task, "analyze_frames")
        if statuses != ["LOST", "SUCCEEDED"]:
            raise DemoError(f"worker-crash evidence mismatch: analyze attempts={statuses}")
        events = get_events(task_id)
        if event_count(events, "lease_expired") < 1:
            raise DemoError("worker-crash scenario has no lease_expired event")
        print_attempt_evidence(task)
        print("[worker-crash] PASS: analyze attempts=[LOST,SUCCEEDED], lease recovered")
    finally:
        stop_owned_process(replacement)


def run_cache_hit(args: argparse.Namespace) -> None:
    print("[cache-hit] creating a fresh baseline task")
    profile = unique_profile("cache")
    baseline_id = create_task(
        args.video,
        frame_count=args.frame_count,
        fault_plan="none",
        profile=profile,
    )
    baseline = wait_for_task(
        baseline_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    require_succeeded(baseline, "cache baseline")
    if [step.get("status") for step in baseline.get("steps", [])] != ["SUCCEEDED"] * 5:
        raise DemoError("cache baseline did not execute all five steps")

    print("  rerunning the baseline with identical input and parameters")
    rerun = request_json("POST", f"/api/tasks/{baseline_id}/rerun")
    rerun_id = rerun.get("task_id")
    if not isinstance(rerun_id, str) or not rerun_id:
        raise DemoError("rerun response did not include task_id")
    task = wait_for_task(rerun_id, timeout=args.timeout, poll_interval=args.poll_interval)
    require_succeeded(task, "cache-hit rerun")
    statuses = [step.get("status") for step in task.get("steps", [])]
    events = get_events(rerun_id)
    hits = event_count(events, "cache_hit")
    if statuses != ["SKIPPED"] * 5 or hits != 5:
        raise DemoError(f"cache evidence mismatch: statuses={statuses}, cache_hit={hits}")
    print_attempt_evidence(task)
    print("[cache-hit] PASS: five steps skipped with five cache_hit events")


SCENARIOS: dict[str, Callable[[argparse.Namespace], None]] = {
    "normal": run_normal,
    "auto-retry": run_auto_retry,
    "cache-hit": run_cache_hit,
    # This scenario intentionally terminates the active Worker, so keep it last
    # when running the combined acceptance command.
    "worker-crash": run_worker_crash,
}


def ensure_ready(video: Path) -> None:
    if not video.is_file():
        raise DemoError(f"sample video not found: {video}")
    if video.suffix.lower() not in {".mp4", ".mov"}:
        raise DemoError("--video must point to an MP4 or MOV file")
    health = request_json("GET", "/api/health")
    if health.get("status") != "ok":
        raise DemoError(f"API health is {health.get('status')}: {health}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.video = args.video.expanduser().resolve()
    try:
        ensure_ready(args.video)
        selected = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
        for index, name in enumerate(selected, start=1):
            if index > 1:
                print()
            SCENARIOS[name](args)
    except KeyboardInterrupt:
        print("\nInterrupted; any replacement Worker started by this script was stopped.", file=sys.stderr)
        return 130
    except DemoError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(selected)} acceptance scenario(s) completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
