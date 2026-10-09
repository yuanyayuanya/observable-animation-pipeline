from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping

from app.domain.contracts import StepExecutor
from app.pipeline.steps import (
    AnalyzeFramesStep,
    BuildContactSheetStep,
    ExtractFramesStep,
    PackageResultStep,
    ValidateInputStep,
)


@dataclass(frozen=True, slots=True)
class StepDefinition:
    key: str
    version: str
    weight: int
    max_retries: int
    executor: StepExecutor

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


class StepRegistry:
    def __init__(self, definitions: tuple[StepDefinition, ...]) -> None:
        if not definitions:
            raise ValueError("step registry cannot be empty")
        keys = [definition.key for definition in definitions]
        if len(set(keys)) != len(keys):
            raise ValueError("step keys must be unique")
        if sum(definition.weight for definition in definitions) != 100:
            raise ValueError("step weights must add up to 100")
        for definition in definitions:
            if definition.key != definition.executor.key:
                raise ValueError(f"executor key mismatch for {definition.key}")
            if definition.version != definition.executor.version:
                raise ValueError(f"executor version mismatch for {definition.key}")
            if definition.max_attempts != definition.executor.max_attempts:
                raise ValueError(f"executor attempt policy mismatch for {definition.key}")
        self._definitions = definitions
        self._by_key: Mapping[str, StepDefinition] = MappingProxyType(
            {definition.key: definition for definition in definitions}
        )

    def __iter__(self) -> Iterator[StepDefinition]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(definition.key for definition in self._definitions)

    def get(self, key: str) -> StepDefinition:
        try:
            return self._by_key[key]
        except KeyError as error:
            raise KeyError(f"unknown pipeline step: {key}") from error


DEFAULT_STEP_REGISTRY = StepRegistry(
    (
        StepDefinition("validate_input", "1", 5, 0, ValidateInputStep()),
        StepDefinition("extract_frames", "1", 20, 1, ExtractFramesStep()),
        StepDefinition("analyze_frames", "1", 45, 2, AnalyzeFramesStep()),
        StepDefinition("build_contact_sheet", "1", 20, 1, BuildContactSheetStep()),
        StepDefinition("package_result", "1", 10, 1, PackageResultStep()),
    )
)
