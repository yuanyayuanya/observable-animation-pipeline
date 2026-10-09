from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.domain.enums import StepStatus, TaskStatus
from app.domain.errors import IllegalTransitionError


@dataclass(frozen=True, slots=True)
class PipelineStepSpec:
    key: str
    ordinal: int
    weight: int
    max_attempts: int
    version: str = "1"


PIPELINE_STEPS: tuple[PipelineStepSpec, ...] = (
    PipelineStepSpec("validate_input", 1, 5, 1),
    PipelineStepSpec("extract_frames", 2, 20, 2),
    PipelineStepSpec("analyze_frames", 3, 45, 3),
    PipelineStepSpec("build_contact_sheet", 4, 20, 2),
    PipelineStepSpec("package_result", 5, 10, 2),
)

STEP_WEIGHTS = {spec.key: spec.weight for spec in PIPELINE_STEPS}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY}),
    StepStatus.READY: frozenset({StepStatus.RUNNING, StepStatus.SKIPPED}),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.SKIPPED,
            StepStatus.RETRY_WAIT,
            StepStatus.FAILED,
            StepStatus.READY,
        }
    ),
    StepStatus.RETRY_WAIT: frozenset({StepStatus.READY}),
    StepStatus.FAILED: frozenset({StepStatus.READY}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
}

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.PAUSED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.PAUSED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING, TaskStatus.PAUSED}),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.SUCCEEDED: frozenset(),
}


def ensure_step_transition(
    current: StepStatus | str, target: StepStatus | str
) -> StepStatus:
    current_status = StepStatus(current)
    target_status = StepStatus(target)
    if current_status == target_status:
        return target_status
    if target_status not in _STEP_TRANSITIONS[current_status]:
        raise IllegalTransitionError(
            f"illegal step transition: {current_status} -> {target_status}"
        )
    return target_status


def ensure_task_transition(
    current: TaskStatus | str, target: TaskStatus | str
) -> TaskStatus:
    current_status = TaskStatus(current)
    target_status = TaskStatus(target)
    if current_status == target_status:
        return target_status
    if target_status not in _TASK_TRANSITIONS[current_status]:
        raise IllegalTransitionError(
            f"illegal task transition: {current_status} -> {target_status}"
        )
    return target_status


def derive_task_status(
    statuses: Iterable[StepStatus | str], *, paused: bool = False
) -> TaskStatus:
    values = tuple(StepStatus(status) for status in statuses)
    if not values:
        return TaskStatus.PENDING

    if paused and StepStatus.RUNNING not in values:
        return TaskStatus.PAUSED

    successful = {StepStatus.SUCCEEDED, StepStatus.SKIPPED}
    if all(status in successful for status in values):
        return TaskStatus.SUCCEEDED

    active = {StepStatus.RUNNING, StepStatus.READY, StepStatus.RETRY_WAIT}
    if any(status in active for status in values):
        return TaskStatus.RUNNING

    if StepStatus.FAILED in values:
        return TaskStatus.FAILED

    if any(status in successful for status in values):
        return TaskStatus.RUNNING
    return TaskStatus.PENDING


def weighted_progress(
    steps: Iterable[tuple[str, StepStatus | str, int]],
) -> int:
    total = 0.0
    seen_weight = 0
    successful = {StepStatus.SUCCEEDED, StepStatus.SKIPPED}

    for step_key, raw_status, raw_progress in steps:
        try:
            weight = STEP_WEIGHTS[step_key]
        except KeyError as error:
            raise ValueError(f"unknown pipeline step: {step_key}") from error
        status = StepStatus(raw_status)
        progress = 100 if status in successful else raw_progress
        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be between 0 and 100: {progress}")
        total += weight * progress / 100
        seen_weight += weight

    if seen_weight == 0:
        return 0
    return round(total)
