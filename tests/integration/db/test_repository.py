from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import text

from app.db import create_schema, create_session_factory, create_sqlite_engine
from app.db.models import StepAttempt, Task, TaskStep
from app.db.repository import PipelineRepository
from app.domain.contracts import ProducedArtifact
from app.domain.enums import AttemptStatus, StepStatus, TaskStatus
from app.domain.errors import IllegalTransitionError, StaleLeaseError
from app.worker.protocol import ClaimedStep, CommittedArtifact


NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "pipeline.db")
    create_schema(engine)
    sessions = create_session_factory(engine)
    repository = PipelineRepository(sessions, lease_seconds=15)
    try:
        yield engine, sessions, repository
    finally:
        engine.dispose()


def _create_task(repository: PipelineRepository, suffix: str = "1") -> str:
    result = repository.create_task(
        input_sha256="a" * 64,
        original_filename="demo.mp4",
        input_relative_path=f"uploads/task-{suffix}/source.mp4",
        idempotency_key=f"create-{suffix}",
        params={"frame_count": 8, "analysis_profile": "default"},
        now=NOW,
    )
    assert result.created is True
    return result.task_id


def _artifact(
    kind: str,
    path: str,
    marker: str,
    *,
    cache_marker: str | None = None,
) -> CommittedArtifact:
    cache_marker = cache_marker or marker
    return CommittedArtifact(
        artifact=ProducedArtifact(
            kind=kind,
            relative_path=path,
            media_type="application/octet-stream",
        ),
        sha256=marker * 64,
        size_bytes=10,
        cache_key=(cache_marker.upper() * 64)[:64],
    )


def _claim(repository: PipelineRepository, offset: int = 0) -> ClaimedStep:
    claim = repository.claim_next_step("worker-1", NOW + timedelta(seconds=offset))
    assert claim is not None
    return claim


@pytest.mark.integration
def test_sqlite_configuration_and_task_persistence(tmp_path: Path) -> None:
    database_path = tmp_path / "persistent.db"
    engine = create_sqlite_engine(database_path)
    create_schema(engine)
    sessions = create_session_factory(engine)
    repository = PipelineRepository(sessions)
    task_id = _create_task(repository, "persistent")

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
    engine.dispose()

    reopened = create_sqlite_engine(database_path)
    create_schema(reopened)
    reopened_repository = PipelineRepository(create_session_factory(reopened))
    detail = reopened_repository.get_task_detail(task_id)
    assert detail is not None
    assert detail.original_filename == "demo.mp4"
    assert detail.input_relative_path == "uploads/task-persistent/source.mp4"
    assert detail.status == TaskStatus.PENDING
    assert [step.key for step in detail.steps] == [
        "validate_input",
        "extract_frames",
        "analyze_frames",
        "build_contact_sheet",
        "package_result",
    ]
    assert detail.steps[0].status == StepStatus.READY
    assert all(step.status == StepStatus.PENDING for step in detail.steps[1:])
    reopened.dispose()


@pytest.mark.integration
def test_idempotent_create_and_query_methods(database) -> None:
    _, _, repository = database
    task_id = _create_task(repository, "query")
    duplicate = repository.create_task(
        input_sha256="b" * 64,
        original_filename="ignored.mov",
        input_relative_path="uploads/ignored/source.mov",
        idempotency_key="create-query",
        params={"frame_count": 4},
        now=NOW + timedelta(seconds=1),
    )
    assert duplicate.created is False
    assert duplicate.task_id == task_id

    listed = repository.list_tasks()
    assert [task.id for task in listed] == [task_id]
    events = repository.list_events(task_id)
    assert [event.event_type for event in events] == ["task_created"]
    source = repository.get_step_inputs(
        repository.get_task_detail(task_id).steps[0].id  # type: ignore[union-attr]
    )
    assert [(item.kind, item.path.as_posix()) for item in source] == [
        ("source_video", "uploads/task-query/source.mp4")
    ]


@pytest.mark.integration
def test_create_task_accepts_preallocated_id_and_idempotency_still_wins(
    database,
) -> None:
    _, _, repository = database
    provided_id = "tsk_" + "1" * 32
    created = repository.create_task(
        task_id=provided_id,
        input_sha256="a" * 64,
        original_filename="preuploaded.mp4",
        input_relative_path=f"uploads/{provided_id}/source.mp4",
        idempotency_key="preallocated-create",
        params={"frame_count": 8},
        now=NOW,
    )
    assert created.created is True
    assert created.task_id == provided_id

    duplicate = repository.create_task(
        task_id="tsk_" + "2" * 32,
        input_sha256="b" * 64,
        original_filename="ignored.mp4",
        input_relative_path="uploads/ignored/source.mp4",
        idempotency_key="preallocated-create",
        params={},
        now=NOW + timedelta(seconds=1),
    )
    assert duplicate.created is False
    assert duplicate.task_id == provided_id


@pytest.mark.integration
def test_two_claimers_cannot_claim_the_same_step(database) -> None:
    _, sessions, repository = database
    task_id = _create_task(repository, "concurrent")
    barrier = Barrier(2)

    def claim(worker_id: str) -> ClaimedStep | None:
        barrier.wait(timeout=5)
        return repository.claim_next_step(worker_id, NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    with sessions() as session:
        steps = session.scalars(
            __import__("sqlalchemy").select(TaskStep).where(
                TaskStep.task_id == task_id
            )
        ).all()
        attempts = session.scalars(__import__("sqlalchemy").select(StepAttempt)).all()
        assert sum(step.status == StepStatus.RUNNING.value for step in steps) == 1
        assert len(attempts) == 1


@pytest.mark.integration
def test_expired_lease_is_recovered_and_old_fencing_token_is_rejected(
    database,
) -> None:
    _, sessions, repository = database
    task_id = _create_task(repository, "lease")
    first = _claim(repository)
    repository.complete_step(first, [], NOW + timedelta(seconds=1))

    old_claim = _claim(repository, 2)
    assert old_claim.step_key == "extract_frames"
    recovered_at = NOW + timedelta(seconds=18)
    assert repository.recover_expired_leases(recovered_at) == 1
    replacement = repository.claim_next_step("worker-2", recovered_at)
    assert replacement is not None
    assert replacement.step_id == old_claim.step_id
    assert replacement.attempt_no == 2
    assert replacement.lease_token != old_claim.lease_token

    with pytest.raises(StaleLeaseError):
        repository.heartbeat(
            old_claim,
            progress=60,
            message="late heartbeat",
            now=recovered_at + timedelta(seconds=1),
        )

    with sessions() as session:
        old_attempt = session.get(StepAttempt, old_claim.attempt_id)
        assert old_attempt is not None
        assert old_attempt.status == AttemptStatus.LOST.value
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.RUNNING.value


@pytest.mark.integration
def test_manual_retry_adds_one_attempt_and_is_idempotent(database) -> None:
    _, _, repository = database
    task_id = _create_task(repository, "manual-retry")
    claim = _claim(repository)
    repository.fail_step(
        claim,
        error_type="PERMANENT_INPUT",
        error_message="bad input",
        retryable=False,
        now=NOW + timedelta(seconds=1),
    )

    assert repository.retry_step(
        task_id=task_id,
        step_key="validate_input",
        request_id="retry-1",
        now=NOW + timedelta(seconds=2),
    )
    assert not repository.retry_step(
        task_id=task_id,
        step_key="validate_input",
        request_id="retry-1",
        now=NOW + timedelta(seconds=3),
    )
    replacement = repository.claim_next_step(
        "worker-2", NOW + timedelta(seconds=4)
    )
    assert replacement is not None
    assert replacement.attempt_no == 2


@pytest.mark.integration
def test_due_retry_is_claimable_in_the_same_poll(database) -> None:
    _, _, repository = database
    _create_task(repository, "due-retry")
    validate = _claim(repository)
    repository.complete_step(validate, [], NOW + timedelta(seconds=1))
    first = _claim(repository, 2)
    assert first.step_key == "extract_frames"
    failed_at = NOW + timedelta(seconds=3)
    repository.fail_step(
        first,
        error_type="TRANSIENT",
        error_message="try again",
        retryable=True,
        now=failed_at,
    )

    replacement = repository.claim_next_step(
        "worker-2",
        failed_at + timedelta(seconds=2),
    )

    assert replacement is not None
    assert replacement.step_id == first.step_id
    assert replacement.attempt_no == 2


@pytest.mark.integration
def test_resume_task_derives_status_and_emits_event(database) -> None:
    _, sessions, repository = database
    task_id = _create_task(repository, "resume")
    with sessions.begin() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.PAUSED.value

    resumed = repository.resume_task(task_id, NOW + timedelta(seconds=1))

    assert resumed == TaskStatus.RUNNING
    detail = repository.get_task_detail(task_id)
    assert detail is not None
    assert detail.status == TaskStatus.RUNNING
    resumed_events = [
        event
        for event in repository.list_events(task_id)
        if event.event_type == "task_resumed"
    ]
    assert len(resumed_events) == 1
    assert resumed_events[0].payload == {"status": "RUNNING"}

    with pytest.raises(IllegalTransitionError):
        repository.resume_task(task_id, NOW + timedelta(seconds=2))


@pytest.mark.integration
def test_later_steps_receive_source_and_all_prior_artifacts(database) -> None:
    _, _, repository = database
    _create_task(repository, "inputs")

    validate = _claim(repository, 0)
    repository.complete_step(
        validate,
        [_artifact("input_metadata", "artifacts/validate/input.json", "b")],
        NOW + timedelta(seconds=1),
    )
    extract = _claim(repository, 2)
    repository.complete_step(
        extract,
        [
            _artifact(
                "frame",
                "artifacts/frames/001.jpg",
                "c",
                cache_marker="c",
            ),
            _artifact(
                "frame",
                "artifacts/frames/002.jpg",
                "d",
                cache_marker="c",
            ),
        ],
        NOW + timedelta(seconds=3),
    )
    analyze = _claim(repository, 4)
    repository.complete_step(
        analyze,
        [_artifact("analysis", "artifacts/analyze/analysis.json", "e")],
        NOW + timedelta(seconds=5),
    )

    build = _claim(repository, 6)
    build_inputs = repository.get_step_inputs(build.step_id)
    assert [item.kind for item in build_inputs] == [
        "source_video",
        "input_metadata",
        "frame",
        "frame",
        "analysis",
    ]
    assert [item.path.as_posix() for item in build_inputs] == [
        "uploads/task-inputs/source.mp4",
        "artifacts/validate/input.json",
        "artifacts/frames/001.jpg",
        "artifacts/frames/002.jpg",
        "artifacts/analyze/analysis.json",
    ]
    repository.complete_step(
        build,
        [_artifact("contact_sheet", "artifacts/build/contact.jpg", "f")],
        NOW + timedelta(seconds=7),
    )

    package = _claim(repository, 8)
    package_inputs = repository.get_step_inputs(package.step_id)
    assert [item.kind for item in package_inputs] == [
        "source_video",
        "input_metadata",
        "frame",
        "frame",
        "analysis",
        "contact_sheet",
    ]
    assert all(
        item.path.as_posix() != "artifacts/package/result.zip"
        for item in package_inputs
    )


@pytest.mark.integration
def test_cache_hit_copies_metadata_finishes_attempt_and_unlocks_next_step(
    database,
) -> None:
    _, _, repository = database
    first_task_id = _create_task(repository, "cache-source")
    source_claim = _claim(repository)
    committed = _artifact(
        "input_metadata", "artifacts/shared/input.json", "9"
    )
    repository.complete_step(
        source_claim, [committed], NOW + timedelta(seconds=1)
    )

    cached = repository.find_cached_artifacts(committed.cache_key)
    assert len(cached) == 1
    assert cached[0].task_id == first_task_id

    second_task_id = _create_task(repository, "cache-target")
    cache_claim = repository.claim_next_step(
        "cache-worker", NOW + timedelta(seconds=2)
    )
    assert cache_claim is not None
    assert cache_claim.task_id == second_task_id
    repository.complete_step_from_cache(
        cache_claim, cached, NOW + timedelta(seconds=3)
    )

    detail = repository.get_task_detail(second_task_id)
    assert detail is not None
    assert detail.steps[0].status == StepStatus.SKIPPED
    assert detail.steps[0].progress == 100
    assert detail.steps[0].attempts[0].status == AttemptStatus.SUCCEEDED
    assert detail.steps[0].attempts[0].finished_at is not None
    assert detail.steps[1].status == StepStatus.READY
    assert len(detail.artifacts) == 1
    copied = detail.artifacts[0]
    assert copied.id != cached[0].id
    assert copied.relative_path == cached[0].relative_path
    assert copied.sha256 == cached[0].sha256
    assert copied.cache_key == cached[0].cache_key
    assert any(
        event.event_type == "cache_hit"
        for event in repository.list_events(second_task_id)
    )
