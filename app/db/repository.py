from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Artifact,
    Event,
    ResourceLease,
    StepAttempt,
    Task,
    TaskStep,
)
from app.domain.enums import AttemptStatus, ErrorKind, StepStatus, TaskStatus
from app.domain.contracts import ArtifactInput
from app.domain.errors import IllegalTransitionError, StaleLeaseError
from app.domain.state_machine import (
    PIPELINE_STEPS,
    derive_task_status,
    ensure_step_transition,
    ensure_task_transition,
    weighted_progress,
)
from app.ids import new_id
from app.worker.protocol import ClaimedStep, CommittedArtifact


@dataclass(frozen=True, slots=True)
class TaskCreation:
    task_id: str
    status: TaskStatus
    created: bool


@dataclass(frozen=True, slots=True)
class TaskSummary:
    id: str
    status: TaskStatus
    progress: int
    original_filename: str
    input_relative_path: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    id: str
    attempt_no: int
    status: AttemptStatus
    worker_id: str
    progress: int
    message: str | None
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    id: str
    key: str
    ordinal: int
    status: StepStatus
    progress: int
    weight: int
    attempt_count: int
    max_attempts: int
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    last_error_type: str | None
    last_error_message: str | None
    attempts: tuple[AttemptSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    id: str
    task_id: str
    step_id: str
    attempt_id: str
    kind: str
    relative_path: str
    media_type: str
    sha256: str
    size_bytes: int
    cache_key: str
    metadata: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskDetail:
    id: str
    status: TaskStatus
    progress: int
    input_sha256: str
    original_filename: str
    input_relative_path: str
    params: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    steps: tuple[StepSnapshot, ...]
    artifacts: tuple[ArtifactSnapshot, ...]


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    sequence: int
    task_id: str
    step_id: str | None
    attempt_id: str | None
    event_type: str
    payload: Mapping[str, Any]
    created_at: datetime


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class PipelineRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = 15,
        retry_backoff_base_seconds: int = 2,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if retry_backoff_base_seconds < 1:
            raise ValueError("retry_backoff_base_seconds must be positive")
        self._sessions = session_factory
        self._lease_seconds = lease_seconds
        self._retry_backoff_base_seconds = retry_backoff_base_seconds

    @contextmanager
    def _write_transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            # SQLite ignores SELECT FOR UPDATE. BEGIN IMMEDIATE obtains the one
            # writer reservation before any candidate step is selected.
            session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_task(
        self,
        *,
        input_sha256: str,
        original_filename: str,
        input_relative_path: str,
        idempotency_key: str,
        params: Mapping[str, Any],
        now: datetime,
        task_id: str | None = None,
    ) -> TaskCreation:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            existing = session.scalar(
                select(Task).where(Task.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return TaskCreation(
                    task_id=existing.id,
                    status=TaskStatus(existing.status),
                    created=False,
                )

            new_task_id = task_id or new_id("tsk")
            if not new_task_id.startswith("tsk_"):
                raise ValueError("task_id must use the tsk_ prefix")
            if session.get(Task, new_task_id) is not None:
                raise ValueError(f"task_id already exists: {new_task_id}")

            task = Task(
                id=new_task_id,
                status=TaskStatus.PENDING.value,
                progress=0,
                input_sha256=input_sha256,
                original_filename=original_filename,
                input_relative_path=self._validated_relative_path(
                    input_relative_path
                ),
                idempotency_key=idempotency_key,
                params_json=dict(params),
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            session.flush()

            predecessor_id: str | None = None
            for index, spec in enumerate(PIPELINE_STEPS):
                step = TaskStep(
                    id=new_id("stp"),
                    task_id=task.id,
                    depends_on_step_id=predecessor_id,
                    step_key=spec.key,
                    step_version=spec.version,
                    ordinal=spec.ordinal,
                    weight=spec.weight,
                    status=(
                        StepStatus.READY.value
                        if index == 0
                        else StepStatus.PENDING.value
                    ),
                    progress=0,
                    attempt_count=0,
                    max_attempts=spec.max_attempts,
                    created_at=now,
                    updated_at=now,
                )
                session.add(step)
                session.flush()
                predecessor_id = step.id

            self._add_event(
                session,
                task_id=task.id,
                event_type="task_created",
                now=now,
                payload={"step_count": len(PIPELINE_STEPS)},
            )
            return TaskCreation(task.id, TaskStatus.PENDING, True)

    def list_tasks(self, *, limit: int = 50, offset: int = 0) -> list[TaskSummary]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self._sessions() as session:
            tasks = session.scalars(
                select(Task)
                .order_by(Task.created_at.desc(), Task.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            return [self._task_summary(task) for task in tasks]

    def count_tasks(self) -> int:
        with self._sessions() as session:
            return int(session.scalar(select(func.count(Task.id))) or 0)

    def get_task_detail(self, task_id: str) -> TaskDetail | None:
        with self._sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                return None
            steps = session.scalars(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.ordinal)
            ).all()
            attempts = session.scalars(
                select(StepAttempt)
                .join(TaskStep, TaskStep.id == StepAttempt.step_id)
                .where(TaskStep.task_id == task_id)
                .order_by(StepAttempt.step_id, StepAttempt.attempt_no)
            ).all()
            artifacts = session.scalars(
                select(Artifact)
                .where(Artifact.task_id == task_id)
                .order_by(Artifact.created_at, Artifact.id)
            ).all()

            attempts_by_step: dict[str, list[AttemptSnapshot]] = {}
            for attempt in attempts:
                attempts_by_step.setdefault(attempt.step_id, []).append(
                    self._attempt_snapshot(attempt)
                )
            step_snapshots = tuple(
                self._step_snapshot(
                    step, tuple(attempts_by_step.get(step.id, ()))
                )
                for step in steps
            )
            artifact_snapshots = tuple(
                self._artifact_snapshot(artifact) for artifact in artifacts
            )
            return TaskDetail(
                id=task.id,
                status=TaskStatus(task.status),
                progress=task.progress,
                input_sha256=task.input_sha256,
                original_filename=task.original_filename,
                input_relative_path=task.input_relative_path,
                params=dict(task.params_json),
                created_at=task.created_at,
                updated_at=task.updated_at,
                steps=step_snapshots,
                artifacts=artifact_snapshots,
            )

    def list_events(
        self, task_id: str, *, after_sequence: int = 0, limit: int = 200
    ) -> list[EventSnapshot]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._sessions() as session:
            events = session.scalars(
                select(Event)
                .where(
                    Event.task_id == task_id,
                    Event.sequence > after_sequence,
                )
                .order_by(Event.sequence)
                .limit(limit)
            ).all()
            return [self._event_snapshot(event) for event in events]

    def get_artifact(self, artifact_id: str) -> ArtifactSnapshot | None:
        with self._sessions() as session:
            artifact = session.get(Artifact, artifact_id)
            return (
                None if artifact is None else self._artifact_snapshot(artifact)
            )

    def find_cached_artifacts(
        self, cache_key: str
    ) -> tuple[ArtifactSnapshot, ...]:
        with self._sessions() as session:
            source_attempt_id = session.scalar(
                select(Artifact.attempt_id)
                .join(StepAttempt, StepAttempt.id == Artifact.attempt_id)
                .where(
                    Artifact.cache_key == cache_key,
                    StepAttempt.status == AttemptStatus.SUCCEEDED.value,
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .limit(1)
            )
            if source_attempt_id is None:
                return ()
            artifacts = session.scalars(
                select(Artifact)
                .where(
                    Artifact.attempt_id == source_attempt_id,
                    Artifact.cache_key == cache_key,
                )
                .order_by(Artifact.relative_path, Artifact.id)
            ).all()
            return tuple(self._artifact_snapshot(artifact) for artifact in artifacts)

    def get_step_inputs(self, step_id: str) -> tuple[ArtifactInput, ...]:
        with self._sessions() as session:
            step = session.get(TaskStep, step_id)
            if step is None:
                raise KeyError(f"step not found: {step_id}")
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")
            artifacts = session.scalars(
                select(Artifact)
                .join(TaskStep, TaskStep.id == Artifact.step_id)
                .where(
                    Artifact.task_id == task.id,
                    TaskStep.ordinal < step.ordinal,
                )
                .order_by(
                    TaskStep.ordinal,
                    Artifact.relative_path,
                    Artifact.id,
                )
            ).all()
            source = ArtifactInput(
                kind="source_video",
                path=Path(task.input_relative_path),
                sha256=task.input_sha256,
            )
            prior_outputs = tuple(
                ArtifactInput(
                    kind=artifact.kind,
                    path=Path(artifact.relative_path),
                    sha256=artifact.sha256,
                )
                for artifact in artifacts
            )
            return (source, *prior_outputs)

    def claim_next_step(self, worker_id: str, now: datetime) -> ClaimedStep | None:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            self._promote_due_retries(session, now)
            # The session factory intentionally disables autoflush. Persist retry
            # promotions so the runnable-step query observes READY immediately.
            session.flush()
            step = session.scalar(
                select(TaskStep)
                .join(Task, Task.id == TaskStep.task_id)
                .where(
                    TaskStep.status == StepStatus.READY.value,
                    or_(
                        TaskStep.next_attempt_at.is_(None),
                        TaskStep.next_attempt_at <= now,
                    ),
                    Task.status != TaskStatus.PAUSED.value,
                )
                .order_by(Task.created_at, TaskStep.ordinal)
                .limit(1)
            )
            if step is None:
                return None

            ensure_step_transition(step.status, StepStatus.RUNNING)
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")

            attempt_no = step.attempt_count + 1
            lease_token = secrets.token_urlsafe(32)
            lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            attempt = StepAttempt(
                id=new_id("att"),
                step_id=step.id,
                attempt_no=attempt_no,
                status=AttemptStatus.RUNNING.value,
                worker_id=worker_id,
                lease_token=lease_token,
                progress=0,
                started_at=now,
                heartbeat_at=now,
            )
            session.add(attempt)
            session.flush()

            step.status = StepStatus.RUNNING.value
            step.progress = 0
            step.attempt_count = attempt_no
            step.lease_token = lease_token
            step.lease_expires_at = lease_expires_at
            step.next_attempt_at = None
            step.updated_at = now
            step.last_error_type = None
            step.last_error_message = None
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type="step_claimed",
                now=now,
                payload={"worker_id": worker_id, "attempt_no": attempt_no},
            )
            self._refresh_task_snapshot(session, task, now)

            return ClaimedStep(
                task_id=task.id,
                step_id=step.id,
                step_key=step.step_key,
                attempt_id=attempt.id,
                attempt_no=attempt_no,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                params=dict(task.params_json),
            )

    def heartbeat(
        self,
        claim: ClaimedStep,
        progress: int,
        message: str,
        now: datetime,
    ) -> None:
        now = _aware_utc(now)
        if not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        with self._write_transaction() as session:
            step, attempt = self._require_current_claim(session, claim, now)
            step.progress = progress
            step.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            step.updated_at = now
            attempt.progress = progress
            attempt.message = message
            attempt.heartbeat_at = now
            session.execute(
                update(ResourceLease)
                .where(ResourceLease.owner_attempt_id == attempt.id)
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now
                    + timedelta(seconds=self._lease_seconds),
                )
            )
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type="progress_changed",
                now=now,
                payload={"progress": progress, "message": message},
            )
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")
            self._refresh_task_snapshot(session, task, now)

    def complete_step(
        self,
        claim: ClaimedStep,
        artifacts: list[CommittedArtifact],
        now: datetime,
    ) -> None:
        now = _aware_utc(now)
        cache_keys = {artifact.cache_key for artifact in artifacts}
        if len(cache_keys) > 1:
            raise ValueError("all step artifacts must use the same cache key")
        with self._write_transaction() as session:
            step, attempt = self._require_current_claim(session, claim, now)
            ensure_step_transition(step.status, StepStatus.SUCCEEDED)

            for committed in artifacts:
                relative_path = self._validated_relative_path(
                    committed.artifact.relative_path
                )
                artifact = Artifact(
                    id=new_id("art"),
                    task_id=step.task_id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    kind=committed.artifact.kind,
                    relative_path=relative_path,
                    media_type=committed.artifact.media_type,
                    sha256=committed.sha256,
                    size_bytes=committed.size_bytes,
                    cache_key=committed.cache_key,
                    metadata_json=dict(committed.artifact.metadata),
                    created_at=now,
                )
                session.add(artifact)
                self._add_event(
                    session,
                    task_id=step.task_id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    event_type="artifact_committed",
                    now=now,
                    payload={"artifact_id": artifact.id, "kind": artifact.kind},
                )

            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.progress = 100
            attempt.heartbeat_at = now
            attempt.finished_at = now
            step.status = StepStatus.SUCCEEDED.value
            step.progress = 100
            if cache_keys:
                step.cache_key = next(iter(cache_keys))
            step.lease_token = None
            step.lease_expires_at = None
            step.updated_at = now
            self._release_resources_for_attempt(session, attempt.id)

            downstream = session.scalars(
                select(TaskStep).where(TaskStep.depends_on_step_id == step.id)
            ).all()
            for dependent in downstream:
                if StepStatus(dependent.status) != StepStatus.PENDING:
                    raise IllegalTransitionError(
                        f"dependent step {dependent.id} is not pending"
                    )
                ensure_step_transition(dependent.status, StepStatus.READY)
                dependent.status = StepStatus.READY.value
                dependent.updated_at = now
                self._add_event(
                    session,
                    task_id=dependent.task_id,
                    step_id=dependent.id,
                    event_type="step_ready",
                    now=now,
                    payload={"unlocked_by": step.step_key},
                )

            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type="step_succeeded",
                now=now,
                payload={},
            )
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")
            self._refresh_task_snapshot(session, task, now)

    def complete_step_from_cache(
        self,
        claim: ClaimedStep,
        cached_artifacts: Sequence[ArtifactSnapshot],
        now: datetime,
    ) -> None:
        now = _aware_utc(now)
        if not cached_artifacts:
            raise ValueError("a cache hit must contain at least one artifact")
        cache_keys = {artifact.cache_key for artifact in cached_artifacts}
        if len(cache_keys) != 1:
            raise ValueError("all cached artifacts must use the same cache key")

        with self._write_transaction() as session:
            step, attempt = self._require_current_claim(session, claim, now)
            ensure_step_transition(step.status, StepStatus.SKIPPED)
            for cached in cached_artifacts:
                relative_path = self._validated_relative_path(cached.relative_path)
                session.add(
                    Artifact(
                        id=new_id("art"),
                        task_id=step.task_id,
                        step_id=step.id,
                        attempt_id=attempt.id,
                        kind=cached.kind,
                        relative_path=relative_path,
                        media_type=cached.media_type,
                        sha256=cached.sha256,
                        size_bytes=cached.size_bytes,
                        cache_key=cached.cache_key,
                        metadata_json=dict(cached.metadata),
                        created_at=now,
                    )
                )

            cache_key = next(iter(cache_keys))
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.progress = 100
            attempt.message = "cache hit"
            attempt.heartbeat_at = now
            attempt.finished_at = now
            step.status = StepStatus.SKIPPED.value
            step.progress = 100
            step.cache_key = cache_key
            step.lease_token = None
            step.lease_expires_at = None
            step.updated_at = now
            self._release_resources_for_attempt(session, attempt.id)
            self._unlock_downstream(session, step, now)
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type="cache_hit",
                now=now,
                payload={
                    "cache_key": cache_key,
                    "artifact_count": len(cached_artifacts),
                    "source_artifact_ids": [
                        artifact.id for artifact in cached_artifacts
                    ],
                },
            )
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")
            self._refresh_task_snapshot(session, task, now)

    def record_cache_invalidated(
        self,
        claim: ClaimedStep,
        *,
        cache_key: str,
        reason: str,
        now: datetime,
    ) -> None:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            step, attempt = self._require_current_claim(session, claim, now)
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type="cache_invalidated",
                now=now,
                payload={"cache_key": cache_key, "reason": reason},
            )

    def fail_step(
        self,
        claim: ClaimedStep,
        error_type: str,
        error_message: str,
        retryable: bool,
        now: datetime,
    ) -> None:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            step, attempt = self._require_current_claim(session, claim, now)
            should_retry = retryable and step.attempt_count < step.max_attempts
            target = StepStatus.RETRY_WAIT if should_retry else StepStatus.FAILED
            ensure_step_transition(step.status, target)

            attempt.status = (
                AttemptStatus.TIMED_OUT.value
                if error_type == ErrorKind.STEP_TIMEOUT.value
                else AttemptStatus.FAILED.value
            )
            attempt.error_type = error_type
            attempt.error_message = error_message
            attempt.heartbeat_at = now
            attempt.finished_at = now

            step.status = target.value
            step.lease_token = None
            step.lease_expires_at = None
            step.last_error_type = error_type
            step.last_error_message = error_message
            step.updated_at = now
            if should_retry:
                delay = self._retry_backoff_seconds(step.attempt_count)
                step.next_attempt_at = now + timedelta(seconds=delay)
                event_type = "retry_scheduled"
                payload = {"delay_seconds": delay, "error_type": error_type}
            else:
                step.next_attempt_at = None
                event_type = "step_failed"
                payload = {"error_type": error_type, "retryable": retryable}

            self._release_resources_for_attempt(session, attempt.id)
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                attempt_id=attempt.id,
                event_type=event_type,
                now=now,
                payload=payload,
            )
            task = session.get(Task, step.task_id)
            if task is None:
                raise RuntimeError(f"task missing for step {step.id}")
            self._refresh_task_snapshot(session, task, now)

    def recover_expired_leases(self, now: datetime) -> int:
        now = _aware_utc(now)
        recovered = 0
        affected_task_ids: set[str] = set()
        with self._write_transaction() as session:
            expired_steps = session.scalars(
                select(TaskStep).where(
                    TaskStep.status == StepStatus.RUNNING.value,
                    TaskStep.lease_expires_at.is_not(None),
                    TaskStep.lease_expires_at <= now,
                )
            ).all()
            for step in expired_steps:
                attempt = session.scalar(
                    select(StepAttempt).where(
                        StepAttempt.step_id == step.id,
                        StepAttempt.status == AttemptStatus.RUNNING.value,
                        StepAttempt.lease_token == step.lease_token,
                    )
                )
                if attempt is None:
                    raise RuntimeError(f"running attempt missing for step {step.id}")

                attempt.status = AttemptStatus.LOST.value
                attempt.error_type = ErrorKind.WORKER_LOST.value
                attempt.error_message = "worker lease expired"
                attempt.finished_at = now
                attempt.heartbeat_at = now

                target = (
                    StepStatus.READY
                    if step.attempt_count < step.max_attempts
                    else StepStatus.FAILED
                )
                ensure_step_transition(step.status, target)
                step.status = target.value
                step.progress = 0 if target == StepStatus.READY else step.progress
                step.lease_token = None
                step.lease_expires_at = None
                step.next_attempt_at = None
                step.last_error_type = ErrorKind.WORKER_LOST.value
                step.last_error_message = "worker lease expired"
                step.updated_at = now
                self._release_resources_for_attempt(session, attempt.id)
                self._add_event(
                    session,
                    task_id=step.task_id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    event_type="lease_expired",
                    now=now,
                    payload={"recovered_to": target.value},
                )
                affected_task_ids.add(step.task_id)
                recovered += 1

            for task_id in affected_task_ids:
                task = session.get(Task, task_id)
                if task is not None:
                    self._refresh_task_snapshot(session, task, now)
        return recovered

    def retry_failed_step(
        self,
        *,
        task_id: str,
        step_key: str,
        request_id: str,
        now: datetime,
    ) -> bool:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            prior_requests = session.scalars(
                select(Event).where(
                    Event.task_id == task_id,
                    Event.event_type == "manual_retry_requested",
                )
            ).all()
            if any(
                event.payload_json.get("request_id") == request_id
                for event in prior_requests
            ):
                return False

            step = session.scalar(
                select(TaskStep).where(
                    TaskStep.task_id == task_id,
                    TaskStep.step_key == step_key,
                )
            )
            if step is None:
                raise KeyError(f"step not found: {task_id}/{step_key}")
            if StepStatus(step.status) != StepStatus.FAILED:
                raise IllegalTransitionError(
                    f"only failed steps can be retried: {step.status}"
                )

            used_downstream = session.scalar(
                select(TaskStep.id)
                .where(
                    TaskStep.task_id == task_id,
                    TaskStep.ordinal > step.ordinal,
                    or_(
                        TaskStep.attempt_count > 0,
                        TaskStep.status != StepStatus.PENDING.value,
                    ),
                )
                .limit(1)
            )
            if used_downstream is not None:
                raise IllegalTransitionError(
                    "cannot retry an upstream step after a downstream step started"
                )

            ensure_step_transition(step.status, StepStatus.READY)
            step.status = StepStatus.READY.value
            step.progress = 0
            step.max_attempts += 1
            step.next_attempt_at = None
            step.last_error_type = None
            step.last_error_message = None
            step.updated_at = now
            self._add_event(
                session,
                task_id=task_id,
                step_id=step.id,
                event_type="manual_retry_requested",
                now=now,
                payload={"request_id": request_id},
            )
            task = session.get(Task, task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            self._refresh_task_snapshot(session, task, now)
            return True

    def retry_step(
        self,
        *,
        task_id: str,
        step_key: str,
        request_id: str,
        now: datetime,
    ) -> bool:
        return self.retry_failed_step(
            task_id=task_id,
            step_key=step_key,
            request_id=request_id,
            now=now,
        )

    def resume_task(self, task_id: str, now: datetime) -> TaskStatus:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if TaskStatus(task.status) != TaskStatus.PAUSED:
                raise IllegalTransitionError(
                    f"only paused tasks can be resumed: {task.status}"
                )

            steps = session.scalars(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.ordinal)
            ).all()
            target = derive_task_status(
                (step.status for step in steps),
                paused=False,
            )
            ensure_task_transition(task.status, target)
            task.status = target.value
            task.progress = weighted_progress(
                (step.step_key, step.status, step.progress) for step in steps
            )
            task.updated_at = now
            self._add_event(
                session,
                task_id=task_id,
                event_type="task_resumed",
                now=now,
                payload={"status": target.value},
            )
            return target

    def acquire_resource(
        self,
        claim: ClaimedStep,
        resource_key: str,
        now: datetime,
    ) -> str | None:
        now = _aware_utc(now)
        with self._write_transaction() as session:
            _, attempt = self._require_current_claim(session, claim, now)
            current = session.get(ResourceLease, resource_key)
            if current is not None and current.lease_expires_at > now:
                if current.owner_attempt_id == attempt.id:
                    return current.lease_token
                return None
            if current is not None:
                session.delete(current)
                session.flush()
            token = secrets.token_urlsafe(32)
            session.add(
                ResourceLease(
                    resource_key=resource_key,
                    owner_attempt_id=attempt.id,
                    worker_id=attempt.worker_id,
                    lease_token=token,
                    heartbeat_at=now,
                    lease_expires_at=now
                    + timedelta(seconds=self._lease_seconds),
                )
            )
            return token

    def release_resource(
        self, *, resource_key: str, lease_token: str
    ) -> bool:
        with self._write_transaction() as session:
            lease = session.get(ResourceLease, resource_key)
            if lease is None or not secrets.compare_digest(
                lease.lease_token, lease_token
            ):
                return False
            session.delete(lease)
            return True

    def _promote_due_retries(self, session: Session, now: datetime) -> None:
        due_steps = session.scalars(
            select(TaskStep).where(
                TaskStep.status == StepStatus.RETRY_WAIT.value,
                TaskStep.next_attempt_at <= now,
            )
        ).all()
        for step in due_steps:
            ensure_step_transition(step.status, StepStatus.READY)
            step.status = StepStatus.READY.value
            step.progress = 0
            step.next_attempt_at = None
            step.updated_at = now
            self._add_event(
                session,
                task_id=step.task_id,
                step_id=step.id,
                event_type="retry_ready",
                now=now,
                payload={},
            )

    def _unlock_downstream(
        self, session: Session, step: TaskStep, now: datetime
    ) -> None:
        downstream = session.scalars(
            select(TaskStep).where(TaskStep.depends_on_step_id == step.id)
        ).all()
        for dependent in downstream:
            if StepStatus(dependent.status) != StepStatus.PENDING:
                raise IllegalTransitionError(
                    f"dependent step {dependent.id} is not pending"
                )
            ensure_step_transition(dependent.status, StepStatus.READY)
            dependent.status = StepStatus.READY.value
            dependent.updated_at = now
            self._add_event(
                session,
                task_id=dependent.task_id,
                step_id=dependent.id,
                event_type="step_ready",
                now=now,
                payload={"unlocked_by": step.step_key},
            )

    def _require_current_claim(
        self, session: Session, claim: ClaimedStep, now: datetime
    ) -> tuple[TaskStep, StepAttempt]:
        step = session.get(TaskStep, claim.step_id)
        attempt = session.get(StepAttempt, claim.attempt_id)
        valid = (
            step is not None
            and attempt is not None
            and step.task_id == claim.task_id
            and attempt.step_id == step.id
            and StepStatus(step.status) == StepStatus.RUNNING
            and AttemptStatus(attempt.status) == AttemptStatus.RUNNING
            and step.lease_token is not None
            and secrets.compare_digest(step.lease_token, claim.lease_token)
            and secrets.compare_digest(attempt.lease_token, claim.lease_token)
            and step.lease_expires_at is not None
            and step.lease_expires_at > now
        )
        if not valid:
            raise StaleLeaseError(
                f"lease is stale for step {claim.step_id} and attempt {claim.attempt_id}"
            )
        return step, attempt

    def _refresh_task_snapshot(
        self, session: Session, task: Task, now: datetime
    ) -> None:
        steps = session.scalars(
            select(TaskStep)
            .where(TaskStep.task_id == task.id)
            .order_by(TaskStep.ordinal)
        ).all()
        target = derive_task_status(
            (step.status for step in steps),
            paused=TaskStatus(task.status) == TaskStatus.PAUSED,
        )
        ensure_task_transition(task.status, target)
        task.status = target.value
        task.progress = weighted_progress(
            (step.step_key, step.status, step.progress) for step in steps
        )
        task.updated_at = now

    def _retry_backoff_seconds(self, attempt_no: int) -> int:
        return min(30, self._retry_backoff_base_seconds * (2 ** (attempt_no - 1)))

    @staticmethod
    def _task_summary(task: Task) -> TaskSummary:
        return TaskSummary(
            id=task.id,
            status=TaskStatus(task.status),
            progress=task.progress,
            original_filename=task.original_filename,
            input_relative_path=task.input_relative_path,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    @staticmethod
    def _attempt_snapshot(attempt: StepAttempt) -> AttemptSnapshot:
        return AttemptSnapshot(
            id=attempt.id,
            attempt_no=attempt.attempt_no,
            status=AttemptStatus(attempt.status),
            worker_id=attempt.worker_id,
            progress=attempt.progress,
            message=attempt.message,
            started_at=attempt.started_at,
            heartbeat_at=attempt.heartbeat_at,
            finished_at=attempt.finished_at,
            error_type=attempt.error_type,
            error_message=attempt.error_message,
        )

    @staticmethod
    def _step_snapshot(
        step: TaskStep, attempts: tuple[AttemptSnapshot, ...]
    ) -> StepSnapshot:
        return StepSnapshot(
            id=step.id,
            key=step.step_key,
            ordinal=step.ordinal,
            status=StepStatus(step.status),
            progress=step.progress,
            weight=step.weight,
            attempt_count=step.attempt_count,
            max_attempts=step.max_attempts,
            lease_expires_at=step.lease_expires_at,
            next_attempt_at=step.next_attempt_at,
            last_error_type=step.last_error_type,
            last_error_message=step.last_error_message,
            attempts=attempts,
        )

    @staticmethod
    def _artifact_snapshot(artifact: Artifact) -> ArtifactSnapshot:
        return ArtifactSnapshot(
            id=artifact.id,
            task_id=artifact.task_id,
            step_id=artifact.step_id,
            attempt_id=artifact.attempt_id,
            kind=artifact.kind,
            relative_path=artifact.relative_path,
            media_type=artifact.media_type,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            cache_key=artifact.cache_key,
            metadata=dict(artifact.metadata_json),
            created_at=artifact.created_at,
        )

    @staticmethod
    def _event_snapshot(event: Event) -> EventSnapshot:
        return EventSnapshot(
            sequence=event.sequence,
            task_id=event.task_id,
            step_id=event.step_id,
            attempt_id=event.attempt_id,
            event_type=event.event_type,
            payload=dict(event.payload_json),
            created_at=event.created_at,
        )

    @staticmethod
    def _validated_relative_path(raw_path: str) -> str:
        normalized = raw_path.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"artifact path must be relative: {raw_path}")
        return path.as_posix()

    @staticmethod
    def _release_resources_for_attempt(
        session: Session, attempt_id: str
    ) -> None:
        session.execute(
            delete(ResourceLease).where(
                ResourceLease.owner_attempt_id == attempt_id
            )
        )

    @staticmethod
    def _add_event(
        session: Session,
        *,
        task_id: str,
        event_type: str,
        now: datetime,
        payload: Mapping[str, Any],
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        session.add(
            Event(
                task_id=task_id,
                step_id=step_id,
                attempt_id=attempt_id,
                event_type=event_type,
                payload_json=dict(payload),
                created_at=now,
            )
        )
