from __future__ import annotations

import hashlib
import json
import os
import shutil
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.domain.contracts import ArtifactInput, ProducedArtifact
from app.domain.errors import ArtifactValidationError, PathBoundaryError


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact: ProducedArtifact
    path: Path
    sha256: str
    size_bytes: int
    cache_key: str


class ArtifactStore:
    """Filesystem-backed, content-addressed storage under one runtime root."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.expanduser().resolve(strict=False)
        self.staging_root = self.runtime_root / "staging"
        self.artifacts_root = self.runtime_root / "artifacts"
        self.quarantine_root = self.runtime_root / "quarantine"
        self.temp_root = self.runtime_root / "temp"
        for path in (
            self.runtime_root,
            self.staging_root,
            self.artifacts_root,
            self.quarantine_root,
            self.temp_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def require_runtime_path(self, path: Path) -> Path:
        resolved = path.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(self.runtime_root)
        except ValueError as error:
            raise PathBoundaryError(f"path is outside runtime root: {resolved}") from error
        return resolved

    def prepare_staging(self, attempt_id: str) -> Path:
        path = self._child_for_identifier(self.staging_root, attempt_id, "attempt_id")
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def cache_path(self, cache_key: str) -> Path:
        if len(cache_key) != 64 or any(char not in "0123456789abcdef" for char in cache_key):
            raise ArtifactValidationError("cache key must be a lowercase SHA-256 digest")
        return self.require_runtime_path(self.artifacts_root / cache_key)

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ArtifactValidationError(f"cannot read artifact: {path}") from error
        return digest.hexdigest()

    @classmethod
    def build_cache_key(
        cls,
        step_key: str,
        step_version: str,
        params: Mapping[str, Any],
        input_artifacts: Sequence[ArtifactInput],
    ) -> str:
        payload = {
            "input_sha256": sorted(artifact.sha256.lower() for artifact in input_artifacts),
            "params": cls._normalize_json(params),
            "step_key": step_key,
            "step_version": step_version,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def commit(
        self,
        cache_key: str,
        staging_dir: Path,
        artifacts: Sequence[ProducedArtifact],
    ) -> list[StoredArtifact]:
        staging = self.require_runtime_path(staging_dir)
        try:
            staging.relative_to(self.staging_root)
        except ValueError as error:
            raise PathBoundaryError("staging directory is outside the staging root") from error
        if not staging.is_dir():
            raise ArtifactValidationError(f"staging directory does not exist: {staging}")
        if not artifacts:
            raise ArtifactValidationError("a step must produce at least one artifact")

        validated = [self._validate_staged_artifact(staging, artifact) for artifact in artifacts]
        destination = self.cache_path(cache_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self._volume_id(staging) != self._volume_id(destination.parent):
            raise ArtifactValidationError("staging and artifact directories must be on the same volume")
        if destination.exists():
            raise ArtifactValidationError(f"artifact cache destination already exists: {destination}")

        try:
            os.replace(staging, destination)
        except OSError as error:
            raise ArtifactValidationError("atomic artifact commit failed") from error

        return [
            StoredArtifact(
                artifact=artifact,
                path=destination / relative_path,
                sha256=sha256,
                size_bytes=size_bytes,
                cache_key=cache_key,
            )
            for artifact, relative_path, sha256, size_bytes in validated
        ]

    def is_cache_valid(self, records: Sequence[StoredArtifact]) -> bool:
        if not records:
            return False
        for record in records:
            try:
                path = self.require_runtime_path(record.path)
                expected_root = self.cache_path(record.cache_key)
                path.relative_to(expected_root)
            except (PathBoundaryError, ArtifactValidationError, ValueError):
                return False
            if not path.is_file() or path.stat().st_size != record.size_bytes:
                return False
            if self.sha256_file(path) != record.sha256:
                return False
        return True

    def quarantine_cache(self, cache_key: str) -> Path | None:
        source = self.cache_path(cache_key)
        if not source.exists():
            return None
        destination = self.require_runtime_path(
            self.quarantine_root / f"{cache_key}-{uuid4().hex}"
        )
        os.replace(source, destination)
        return destination

    def _validate_staged_artifact(
        self,
        staging: Path,
        artifact: ProducedArtifact,
    ) -> tuple[ProducedArtifact, Path, str, int]:
        relative = Path(artifact.relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise PathBoundaryError(f"invalid artifact relative path: {artifact.relative_path}")
        source = self.require_runtime_path(staging / relative)
        try:
            source.relative_to(staging)
        except ValueError as error:
            raise PathBoundaryError(f"artifact escapes staging: {artifact.relative_path}") from error
        if source.is_symlink() or not source.is_file():
            raise ArtifactValidationError(f"artifact is missing or not a regular file: {relative}")
        size_bytes = source.stat().st_size
        if size_bytes <= 0:
            raise ArtifactValidationError(f"artifact is empty: {relative}")
        return artifact, relative, self.sha256_file(source), size_bytes

    def _child_for_identifier(self, root: Path, value: str, label: str) -> Path:
        if not value or value in {".", ".."} or Path(value).name != value:
            raise PathBoundaryError(f"invalid {label}: {value!r}")
        return self.require_runtime_path(root / value)

    @staticmethod
    def _volume_id(path: Path) -> tuple[str, int]:
        resolved = path.resolve(strict=True)
        return resolved.drive.lower(), resolved.stat().st_dev

    @classmethod
    def _normalize_json(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if not (float("-inf") < value < float("inf")):
                raise ValueError("cache parameters cannot contain NaN or infinity")
            return value
        if isinstance(value, Path):
            return value.as_posix()
        if isinstance(value, Mapping):
            return {
                str(key): cls._normalize_json(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalize_json(item) for item in value]
        raise TypeError(f"unsupported cache parameter type: {type(value).__name__}")
