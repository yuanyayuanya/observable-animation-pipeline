from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LinkSet(ApiModel):
    self: str


class TaskCreated(ApiModel):
    task_id: str
    status: str
    created: bool
    links: LinkSet


class AttemptView(ApiModel):
    id: str
    attempt_no: int
    status: str
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class StepView(ApiModel):
    id: str
    key: str
    ordinal: int
    status: str
    progress: int = Field(ge=0, le=100)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    next_run_at: datetime | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None
    attempts: list[AttemptView] = Field(default_factory=list)


class ArtifactView(ApiModel):
    id: str
    step_id: str
    kind: str
    media_type: str
    sha256: str
    size_bytes: int = Field(ge=0)
    download_url: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class TaskSummary(ApiModel):
    id: str
    status: str
    progress: int = Field(ge=0, le=100)
    filename: str
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskSummary):
    input_sha256: str
    params: dict[str, Any]
    steps: list[StepView]
    artifacts: list[ArtifactView] = Field(default_factory=list)


class TaskList(ApiModel):
    items: list[TaskSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class EventView(ApiModel):
    sequence: int = Field(ge=1)
    task_id: str
    step_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EventList(ApiModel):
    items: list[EventView]
    next_sequence: int = Field(ge=0)


class RetryAccepted(ApiModel):
    task_id: str
    step_key: str
    status: str


class TaskCommandAccepted(ApiModel):
    task_id: str
    status: str


class HealthView(ApiModel):
    status: str
    database: str
    artifact_store: str


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorResponse(ApiModel):
    error: ErrorBody
