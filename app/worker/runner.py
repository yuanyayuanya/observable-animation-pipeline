from __future__ import annotations

import logging
import os
import shutil
import socket
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db.repository import ArtifactSnapshot, PipelineRepository
from app.domain.contracts import ArtifactInput, ProducedArtifact, StepContext
from app.domain.enums import ErrorKind
from app.domain.errors import PipelineError, StaleLeaseError
from app.pipeline.artifacts import ArtifactStore, StoredArtifact
from app.pipeline.registry import DEFAULT_STEP_REGISTRY, StepRegistry
from app.worker.protocol import ClaimedStep, CommittedArtifact


LOGGER = logging.getLogger(__name__)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class Worker:
    def __init__(
        self,
        repository: PipelineRepository,
        settings: Settings,
        *,
        registry: StepRegistry = DEFAULT_STEP_REGISTRY,
        worker_id: str | None = None,
        clock: SystemClock | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.registry = registry
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.clock = clock or SystemClock()
        self.store = ArtifactStore(settings.runtime_root)

    def run_once(self) -> bool:
        claim = self.repository.claim_next_step(self.worker_id, self.clock.now())
        if claim is None:
            return False
        self._execute_claim(claim)
        return True

    def run_forever(self) -> None:
        next_recovery = self.clock.now()
        LOGGER.info("worker %s started", self.worker_id)
        while True:
            now = self.clock.now()
            if now >= next_recovery:
                recovered = self.repository.recover_expired_leases(now)
                if recovered:
                    LOGGER.warning("recovered %s expired step lease(s)", recovered)
                next_recovery = now + self._recovery_interval()
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)

    def _execute_claim(self, claim: ClaimedStep) -> None:
        definition = self.registry.get(claim.step_key)
        inputs = self._absolute_inputs(
            self.repository.get_step_inputs(claim.step_id)
        )
        cache_key = self.store.build_cache_key(
            claim.step_key,
            definition.version,
            claim.params,
            inputs,
        )

        filesystem_cache_committed = False
        try:
            cached = self.repository.find_cached_artifacts(cache_key)
            if cached:
                cache_records = [
                    self._stored_from_snapshot(item) for item in cached
                ]
                if self.store.is_cache_valid(cache_records):
                    self.repository.complete_step_from_cache(
                        claim, cached, self.clock.now()
                    )
                    LOGGER.info("cache hit for %s/%s", claim.task_id, claim.step_key)
                    return
                self.repository.record_cache_invalidated(
                    claim,
                    cache_key=cache_key,
                    reason="artifact file missing, changed, or outside cache root",
                    now=self.clock.now(),
                )
                self.store.quarantine_cache(cache_key)

            staging = self.store.prepare_staging(claim.attempt_id)
            if claim.step_key == "analyze_frames":
                resource_token = self.repository.acquire_resource(
                    claim, "gpu:0", self.clock.now()
                )
                if resource_token is None:
                    raise _ResourceBusy("mock GPU slot is currently leased")

            context = StepContext(
                task_id=claim.task_id,
                step_id=claim.step_id,
                attempt_id=claim.attempt_id,
                step_key=claim.step_key,
                step_version=definition.version,
                params=claim.params,
                runtime_root=self.settings.runtime_root,
                staging_dir=staging,
                input_artifacts=inputs,
                report_progress=lambda progress, message: self._report_progress(
                    claim, progress, message
                ),
            )
            result = definition.executor.execute(context)
            stored = self.store.commit(cache_key, staging, result.artifacts)
            filesystem_cache_committed = True
            committed = [self._to_committed(item) for item in stored]
            self.repository.complete_step(claim, committed, self.clock.now())
            LOGGER.info("completed %s/%s", claim.task_id, claim.step_key)
        except StaleLeaseError:
            self._quarantine_untracked_cache(
                cache_key, filesystem_cache_committed
            )
            LOGGER.warning("discarded stale claim for %s", claim.step_id)
        except PipelineError as error:
            self._quarantine_untracked_cache(
                cache_key, filesystem_cache_committed
            )
            self._fail_claim(claim, error.kind.value, str(error), error.retryable)
        except Exception as error:
            self._quarantine_untracked_cache(
                cache_key, filesystem_cache_committed
            )
            LOGGER.exception("unexpected step failure for %s", claim.step_id)
            self._fail_claim(
                claim,
                ErrorKind.INTERNAL_INVARIANT.value,
                str(error) or type(error).__name__,
                False,
            )

    def _quarantine_untracked_cache(
        self, cache_key: str, filesystem_cache_committed: bool
    ) -> None:
        if not filesystem_cache_committed:
            return
        try:
            if self.repository.find_cached_artifacts(cache_key):
                return
            quarantined = self.store.quarantine_cache(cache_key)
            if quarantined is not None:
                LOGGER.warning(
                    "quarantined untracked cache after rejected commit: %s",
                    quarantined,
                )
        except Exception:
            LOGGER.exception(
                "could not quarantine untracked cache %s", cache_key
            )

    def _report_progress(
        self, claim: ClaimedStep, progress: int, message: str
    ) -> None:
        self._maybe_exit_worker(claim, progress)
        self.repository.heartbeat(claim, progress, message, self.clock.now())

    def _maybe_exit_worker(self, claim: ClaimedStep, progress: int) -> None:
        if not self.settings.enable_fault_injection or claim.step_key != "analyze_frames":
            return
        if claim.params.get("fault_plan") not in {
            "worker_exit_at_40",
            "worker_exit_during_analysis",
        }:
            return
        if progress < 40:
            return
        marker = self.settings.require_runtime_path(
            self.settings.temp_dir / "faults" / f"{claim.task_id}-worker-exit.used"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="ascii", errors="strict") as handle:
                handle.write(claim.attempt_id)
        except FileExistsError:
            return
        LOGGER.critical("exiting worker for injected crash at %s%%", progress)
        os._exit(75)

    def _fail_claim(
        self,
        claim: ClaimedStep,
        error_type: str,
        message: str,
        retryable: bool,
    ) -> None:
        try:
            self.repository.fail_step(
                claim,
                error_type,
                message,
                retryable,
                self.clock.now(),
            )
        except StaleLeaseError:
            LOGGER.warning("failure arrived after lease expiry for %s", claim.step_id)

    def _absolute_inputs(
        self, inputs: tuple[ArtifactInput, ...]
    ) -> tuple[ArtifactInput, ...]:
        return tuple(
            replace(
                artifact,
                path=self.settings.require_runtime_path(
                    self.settings.runtime_root / artifact.path
                ),
            )
            for artifact in inputs
        )

    def _to_committed(self, stored: StoredArtifact) -> CommittedArtifact:
        relative = stored.path.relative_to(self.settings.runtime_root).as_posix()
        artifact = replace(stored.artifact, relative_path=relative)
        return CommittedArtifact(
            artifact=artifact,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            cache_key=stored.cache_key,
        )

    def _stored_from_snapshot(self, snapshot: ArtifactSnapshot) -> StoredArtifact:
        path = self.settings.require_runtime_path(
            self.settings.runtime_root / snapshot.relative_path
        )
        return StoredArtifact(
            artifact=ProducedArtifact(
                kind=snapshot.kind,
                relative_path=snapshot.relative_path,
                media_type=snapshot.media_type,
                metadata=snapshot.metadata,
            ),
            path=path,
            sha256=snapshot.sha256,
            size_bytes=snapshot.size_bytes,
            cache_key=snapshot.cache_key,
        )

    def _recovery_interval(self) -> timedelta:
        return timedelta(seconds=self.settings.recovery_interval_seconds)


class _ResourceBusy(PipelineError):
    kind = ErrorKind.RETRYABLE_STEP
    retryable = True


def cleanup_abandoned_staging(settings: Settings, attempt_id: str) -> None:
    path = settings.require_runtime_path(settings.staging_dir / attempt_id)
    if path.exists():
        shutil.rmtree(path)
