from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


ProgressReporter = Callable[[int, str], None]


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    kind: str
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    kind: str
    relative_path: str
    media_type: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepContext:
    task_id: str
    step_id: str
    attempt_id: str
    step_key: str
    step_version: str
    params: Mapping[str, Any]
    runtime_root: Path
    staging_dir: Path
    input_artifacts: Sequence[ArtifactInput]
    report_progress: ProgressReporter


@dataclass(frozen=True, slots=True)
class StepResult:
    artifacts: Sequence[ProducedArtifact]
    metrics: Mapping[str, float | int | str] = field(default_factory=dict)


class StepExecutor(Protocol):
    key: str
    version: str
    max_attempts: int

    def execute(self, context: StepContext) -> StepResult:
        """Execute one idempotent step into the attempt staging directory."""

