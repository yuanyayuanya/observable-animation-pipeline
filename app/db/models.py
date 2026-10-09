from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.domain.enums import AttemptStatus, StepStatus, TaskStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC without an offset in SQLite and restore an aware UTC value."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def _enum_values(enum_type: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_enum_values(TaskStatus)})", name="ck_tasks_status"
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_tasks_progress"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    input_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    params_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class TaskStep(Base):
    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "step_key", name="uq_task_steps_task_key"),
        UniqueConstraint("task_id", "ordinal", name="uq_task_steps_task_ordinal"),
        CheckConstraint(
            f"status IN ({_enum_values(StepStatus)})",
            name="ck_task_steps_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_steps_progress"),
        CheckConstraint("weight BETWEEN 1 AND 100", name="ck_task_steps_weight"),
        CheckConstraint("attempt_count >= 0", name="ck_task_steps_attempt_count"),
        CheckConstraint("max_attempts >= 1", name="ck_task_steps_max_attempts"),
        Index("ix_task_steps_runnable", "status", "next_attempt_at", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_step_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_steps.id", ondelete="RESTRICT"), nullable=True
    )
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class StepAttempt(Base):
    __tablename__ = "step_attempts"
    __table_args__ = (
        UniqueConstraint("step_id", "attempt_no", name="uq_attempts_step_number"),
        CheckConstraint(
            f"status IN ({_enum_values(AttemptStatus)})",
            name="ck_step_attempts_status",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_step_attempts_number"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_attempts_progress"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    step_id: Mapped[str] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "relative_path", name="uq_artifacts_attempt_path"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
        Index("ix_artifacts_cache_key", "cache_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("step_attempts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_sequence", "task_id", "sequence"),)

    sequence: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"), nullable=True
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("step_attempts.id", ondelete="CASCADE"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResourceLease(Base):
    __tablename__ = "resource_leases"

    resource_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("step_attempts.id", ondelete="CASCADE"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, index=True
    )
