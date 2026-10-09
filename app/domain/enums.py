from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    TIMED_OUT = "TIMED_OUT"


class ErrorKind(StrEnum):
    PERMANENT_INPUT = "PERMANENT_INPUT"
    RETRYABLE_STEP = "RETRYABLE_STEP"
    STEP_TIMEOUT = "STEP_TIMEOUT"
    WORKER_LOST = "WORKER_LOST"
    ARTIFACT_VALIDATION = "ARTIFACT_VALIDATION"
    INTERNAL_INVARIANT = "INTERNAL_INVARIANT"

