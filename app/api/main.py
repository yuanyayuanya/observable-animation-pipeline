from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import (
    ArtifactView,
    AttemptView,
    ErrorBody,
    ErrorResponse,
    EventList,
    EventView,
    HealthView,
    LinkSet,
    RetryAccepted,
    StepView,
    TaskCreated,
    TaskCommandAccepted,
    TaskDetail,
    TaskList,
    TaskSummary,
)
from app.bootstrap import bootstrap_settings
from app.config import Settings
from app.db.engine import create_schema, create_session_factory, create_sqlite_engine
from app.db.repository import PipelineRepository
from app.domain.errors import IllegalTransitionError, PipelineError
from app.ids import new_id
from app.uploads import store_upload


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = (settings or bootstrap_settings()).validated()
    resolved.ensure_directories()
    engine = create_sqlite_engine(resolved.database_path)
    create_schema(engine)
    repository = PipelineRepository(
        create_session_factory(engine), lease_seconds=resolved.lease_seconds
    )
    static_dir = Path(__file__).resolve().parents[1] / "static"

    app = FastAPI(title="Observable Animation Pipeline", version="0.1.0")
    app.state.settings = resolved
    app.state.repository = repository
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return _error_response(
            request,
            422,
            "REQUEST_INVALID",
            "请求参数不符合要求",
            {"issues": error.errors()},
        )

    @app.exception_handler(PipelineError)
    async def pipeline_error(request: Request, error: PipelineError):
        status = 409 if isinstance(error, IllegalTransitionError) else 400
        return _error_response(
            request,
            status,
            error.kind.value,
            str(error),
            {},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        return _error_response(
            request,
            error.status_code,
            "HTTP_ERROR",
            str(error.detail),
            {"status_code": error.status_code},
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health", response_model=HealthView)
    def health() -> HealthView:
        database = "ok" if resolved.database_path.parent.is_dir() else "unavailable"
        artifacts = "ok" if resolved.artifacts_dir.is_dir() else "unavailable"
        status = "ok" if database == artifacts == "ok" else "degraded"
        return HealthView(
            status=status,
            database=database,
            artifact_store=artifacts,
        )

    @app.post("/api/tasks", response_model=TaskCreated, status_code=201)
    def create_task(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        frame_count: Annotated[int, Form(ge=4, le=12)] = 8,
        analysis_profile: Annotated[str, Form()] = "default",
        fault_plan: Annotated[str, Form()] = "none",
        form_idempotency_key: Annotated[
            str | None, Form(alias="idempotency_key")
        ] = None,
        header_idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
    ) -> TaskCreated:
        task_id = new_id("tsk")
        receipt = store_upload(file.file, file.filename, task_id, resolved)
        key = form_idempotency_key or header_idempotency_key or uuid4().hex
        effective_fault = fault_plan if resolved.enable_fault_injection else "none"
        params = {
            "frame_count": frame_count,
            "analysis_profile": analysis_profile,
            "fault_plan": effective_fault,
            "max_upload_bytes": resolved.max_upload_bytes,
            "max_video_seconds": resolved.max_video_seconds,
        }
        try:
            creation = repository.create_task(
                task_id=task_id,
                input_sha256=receipt.sha256,
                original_filename=receipt.original_filename,
                input_relative_path=receipt.relative_path,
                idempotency_key=key,
                params=params,
                now=datetime.now(UTC),
            )
        except Exception:
            shutil.rmtree(resolved.uploads_dir / task_id, ignore_errors=True)
            raise
        if not creation.created:
            shutil.rmtree(resolved.uploads_dir / task_id, ignore_errors=True)
        return TaskCreated(
            task_id=creation.task_id,
            status=creation.status.value,
            created=creation.created,
            links=LinkSet(self=f"/api/tasks/{creation.task_id}"),
        )

    @app.get("/api/tasks", response_model=TaskList)
    def list_tasks(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TaskList:
        items = repository.list_tasks(limit=limit, offset=offset)
        return TaskList(
            items=[
                TaskSummary(
                    id=item.id,
                    status=item.status.value,
                    progress=item.progress,
                    filename=item.original_filename,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in items
            ],
            total=repository.count_tasks(),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/tasks/{task_id}", response_model=TaskDetail)
    def get_task(task_id: str) -> TaskDetail:
        detail = repository.get_task_detail(task_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _task_detail(detail)

    @app.get("/api/tasks/{task_id}/events", response_model=EventList)
    def get_events(
        task_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> EventList:
        if repository.get_task_detail(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        events = repository.list_events(
            task_id, after_sequence=after_sequence, limit=limit
        )
        return EventList(
            items=[
                EventView(
                    sequence=item.sequence,
                    task_id=item.task_id,
                    step_id=item.step_id,
                    event_type=item.event_type,
                    payload=dict(item.payload),
                    created_at=item.created_at,
                )
                for item in events
            ],
            next_sequence=events[-1].sequence if events else after_sequence,
        )

    @app.post(
        "/api/tasks/{task_id}/steps/{step_key}/retry",
        response_model=RetryAccepted,
    )
    def retry_step(
        task_id: str,
        step_key: str,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
    ) -> RetryAccepted:
        try:
            repository.retry_step(
                task_id=task_id,
                step_key=step_key,
                request_id=idempotency_key or uuid4().hex,
                now=datetime.now(UTC),
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return RetryAccepted(task_id=task_id, step_key=step_key, status="READY")

    @app.post("/api/tasks/{task_id}/rerun", response_model=TaskCreated)
    def rerun_task(task_id: str, ignore_cache: bool = False) -> TaskCreated:
        source = repository.get_task_detail(task_id)
        if source is None:
            raise HTTPException(status_code=404, detail="task not found")
        params = dict(source.params)
        if ignore_cache:
            params["cache_bust"] = uuid4().hex
        creation = repository.create_task(
            task_id=new_id("tsk"),
            input_sha256=source.input_sha256,
            original_filename=source.original_filename,
            input_relative_path=source.input_relative_path,
            idempotency_key=f"rerun:{uuid4().hex}",
            params=params,
            now=datetime.now(UTC),
        )
        return TaskCreated(
            task_id=creation.task_id,
            status=creation.status.value,
            created=True,
            links=LinkSet(self=f"/api/tasks/{creation.task_id}"),
        )

    @app.post(
        "/api/tasks/{task_id}/resume",
        response_model=TaskCommandAccepted,
    )
    def resume_task(task_id: str) -> TaskCommandAccepted:
        try:
            status = repository.resume_task(task_id, datetime.now(UTC))
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return TaskCommandAccepted(task_id=task_id, status=status.value)

    @app.get("/api/artifacts/{artifact_id}")
    def download_artifact(artifact_id: str) -> FileResponse:
        artifact = repository.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        path = resolved.require_runtime_path(
            resolved.runtime_root / artifact.relative_path
        )
        if not path.is_file():
            raise HTTPException(status_code=410, detail="artifact file is missing")
        return FileResponse(
            path,
            media_type=artifact.media_type,
            filename=path.name,
        )

    return app


def _task_detail(detail: Any) -> TaskDetail:
    return TaskDetail(
        id=detail.id,
        status=detail.status.value,
        progress=detail.progress,
        filename=detail.original_filename,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
        input_sha256=detail.input_sha256,
        params=dict(detail.params),
        steps=[
            StepView(
                id=step.id,
                key=step.key,
                ordinal=step.ordinal,
                status=step.status.value,
                progress=step.progress,
                attempt_count=step.attempt_count,
                max_attempts=step.max_attempts,
                next_run_at=step.next_attempt_at,
                lease_expires_at=step.lease_expires_at,
                last_heartbeat_at=(
                    step.attempts[-1].heartbeat_at if step.attempts else None
                ),
                last_error=step.last_error_message,
                attempts=[
                    AttemptView(
                        id=attempt.id,
                        attempt_no=attempt.attempt_no,
                        status=attempt.status.value,
                        worker_id=attempt.worker_id,
                        started_at=attempt.started_at,
                        finished_at=attempt.finished_at,
                        heartbeat_at=attempt.heartbeat_at,
                        error_type=attempt.error_type,
                        error_message=attempt.error_message,
                    )
                    for attempt in step.attempts
                ],
            )
            for step in detail.steps
        ],
        artifacts=[
            ArtifactView(
                id=artifact.id,
                step_id=artifact.step_id,
                kind=artifact.kind,
                media_type=artifact.media_type,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                download_url=f"/api/artifacts/{artifact.id}",
                metadata=dict(artifact.metadata),
                created_at=artifact.created_at,
            )
            for artifact in detail.artifacts
        ],
    )


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any],
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details,
            request_id=getattr(request.state, "request_id", f"req_{uuid4().hex}"),
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


app = create_app()
