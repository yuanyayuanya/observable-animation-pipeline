from __future__ import annotations

import pytest

from app.domain.enums import StepStatus, TaskStatus
from app.domain.errors import IllegalTransitionError
from app.domain.state_machine import (
    derive_task_status,
    ensure_step_transition,
    ensure_task_transition,
    weighted_progress,
)


def test_step_state_machine_accepts_documented_transitions() -> None:
    assert ensure_step_transition("PENDING", "READY") == StepStatus.READY
    assert ensure_step_transition("READY", "RUNNING") == StepStatus.RUNNING
    assert ensure_step_transition("RUNNING", "SKIPPED") == StepStatus.SKIPPED
    assert (
        ensure_step_transition("RUNNING", "RETRY_WAIT")
        == StepStatus.RETRY_WAIT
    )
    assert ensure_step_transition("RETRY_WAIT", "READY") == StepStatus.READY
    assert ensure_step_transition("FAILED", "READY") == StepStatus.READY


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (StepStatus.PENDING, StepStatus.SUCCEEDED),
        (StepStatus.READY, StepStatus.FAILED),
        (StepStatus.SUCCEEDED, StepStatus.READY),
        (StepStatus.SKIPPED, StepStatus.RUNNING),
    ],
)
def test_step_state_machine_rejects_illegal_transitions(
    current: StepStatus, target: StepStatus
) -> None:
    with pytest.raises(IllegalTransitionError):
        ensure_step_transition(current, target)


def test_task_state_machine_allows_manual_recovery_from_failure() -> None:
    assert ensure_task_transition("FAILED", "RUNNING") == TaskStatus.RUNNING
    with pytest.raises(IllegalTransitionError):
        ensure_task_transition("SUCCEEDED", "RUNNING")


def test_task_status_is_derived_from_step_states() -> None:
    assert derive_task_status(["PENDING"] * 5) == TaskStatus.PENDING
    assert (
        derive_task_status(["SUCCEEDED", "READY", "PENDING"])
        == TaskStatus.RUNNING
    )
    assert (
        derive_task_status(["SUCCEEDED", "FAILED", "PENDING"])
        == TaskStatus.FAILED
    )
    assert (
        derive_task_status(["SUCCEEDED", "SKIPPED"])
        == TaskStatus.SUCCEEDED
    )


def test_weighted_progress_uses_pipeline_weights_and_terminal_success() -> None:
    assert (
        weighted_progress(
            [
                ("validate_input", "SUCCEEDED", 10),
                ("extract_frames", "RUNNING", 50),
                ("analyze_frames", "PENDING", 0),
                ("build_contact_sheet", "PENDING", 0),
                ("package_result", "PENDING", 0),
            ]
        )
        == 15
    )
