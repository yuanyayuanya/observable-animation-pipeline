from __future__ import annotations

from app.domain.enums import ErrorKind


class PipelineError(Exception):
    kind = ErrorKind.INTERNAL_INVARIANT
    retryable = False


class PermanentInputError(PipelineError):
    kind = ErrorKind.PERMANENT_INPUT


class RetryableStepError(PipelineError):
    kind = ErrorKind.RETRYABLE_STEP
    retryable = True


class StepTimeoutError(PipelineError):
    kind = ErrorKind.STEP_TIMEOUT
    retryable = True


class ArtifactValidationError(PipelineError):
    kind = ErrorKind.ARTIFACT_VALIDATION
    retryable = True


class StaleLeaseError(PipelineError):
    kind = ErrorKind.WORKER_LOST


class IllegalTransitionError(PipelineError):
    pass


class PathBoundaryError(PipelineError):
    pass

