from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from app.domain.contracts import ProducedArtifact


@dataclass(frozen=True, slots=True)
class ClaimedStep:
    task_id: str
    step_id: str
    step_key: str
    attempt_id: str
    attempt_no: int
    lease_token: str
    lease_expires_at: datetime
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CommittedArtifact:
    artifact: ProducedArtifact
    sha256: str
    size_bytes: int
    cache_key: str


class WorkRepository(Protocol):
    def claim_next_step(self, worker_id: str, now: datetime) -> ClaimedStep | None:
        """Atomically claim one runnable step and create an attempt."""

    def heartbeat(
        self,
        claim: ClaimedStep,
        progress: int,
        message: str,
        now: datetime,
    ) -> None:
        """Extend the lease if the fencing token is still current."""

    def complete_step(
        self,
        claim: ClaimedStep,
        artifacts: list[CommittedArtifact],
        now: datetime,
    ) -> None:
        """Commit artifacts and unlock the next dependency."""

    def fail_step(
        self,
        claim: ClaimedStep,
        error_type: str,
        error_message: str,
        retryable: bool,
        now: datetime,
    ) -> None:
        """Finish the attempt and either schedule retry or fail the step."""


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware UTC timestamp."""

