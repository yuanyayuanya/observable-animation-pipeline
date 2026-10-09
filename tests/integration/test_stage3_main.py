from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import create_app
from app.config import Settings
from app.db.engine import create_schema, create_session_factory, create_sqlite_engine
from app.db.models import ResourceLease
from app.db.repository import PipelineRepository


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _create_task(repository: PipelineRepository, suffix: str) -> None:
    repository.create_task(
        input_sha256=suffix[0] * 64,
        original_filename=f"{suffix}.mp4",
        input_relative_path=f"uploads/{suffix}/source.mp4",
        idempotency_key=f"create-{suffix}",
        params={"frame_count": 8},
        now=NOW,
    )


def test_task_list_total_is_not_limited_to_the_current_page(tmp_path) -> None:
    settings = Settings(runtime_root=tmp_path / "runtime").validated()
    app = create_app(settings)
    try:
        for suffix in ("alpha", "bravo", "charlie"):
            _create_task(app.state.repository, suffix)

        with TestClient(app) as client:
            response = client.get("/api/tasks?limit=1&offset=0")

        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["total"] == 3
    finally:
        app.state.engine.dispose()


def test_step_heartbeat_extends_owned_resource_lease(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "pipeline.db")
    create_schema(engine)
    sessions = create_session_factory(engine)
    repository = PipelineRepository(sessions, lease_seconds=15)
    try:
        _create_task(repository, "resource")
        claim = repository.claim_next_step("worker-resource", NOW)
        assert claim is not None
        assert repository.acquire_resource(claim, "gpu:0", NOW) is not None

        heartbeat_at = NOW + timedelta(seconds=5)
        repository.heartbeat(claim, 25, "still working", heartbeat_at)

        with sessions() as session:
            lease = session.scalar(
                select(ResourceLease).where(
                    ResourceLease.resource_key == "gpu:0"
                )
            )
            assert lease is not None
            assert lease.heartbeat_at == heartbeat_at
            assert lease.lease_expires_at == heartbeat_at + timedelta(seconds=15)
    finally:
        engine.dispose()

