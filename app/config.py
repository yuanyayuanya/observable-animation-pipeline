from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.domain.errors import PathBoundaryError


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    runtime_root: Path
    enable_fault_injection: bool = False
    lease_seconds: int = 15
    heartbeat_seconds: int = 5
    recovery_interval_seconds: int = 5
    worker_poll_seconds: float = 0.5
    max_upload_bytes: int = 50 * 1024 * 1024
    max_video_seconds: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        default_root = Path.cwd() / ".runtime"
        return cls(
            runtime_root=Path(os.getenv("APP_RUNTIME_ROOT", str(default_root))),
            enable_fault_injection=_env_bool("APP_ENABLE_FAULT_INJECTION", False),
            lease_seconds=_env_int("APP_LEASE_SECONDS", 15),
            heartbeat_seconds=_env_int("APP_HEARTBEAT_SECONDS", 5),
            recovery_interval_seconds=_env_int(
                "APP_RECOVERY_INTERVAL_SECONDS", 5
            ),
            worker_poll_seconds=_env_float("APP_WORKER_POLL_SECONDS", 0.5),
        ).validated()

    @property
    def database_path(self) -> Path:
        return self.runtime_root / "app.db"

    @property
    def uploads_dir(self) -> Path:
        return self.runtime_root / "uploads"

    @property
    def staging_dir(self) -> Path:
        return self.runtime_root / "staging"

    @property
    def artifacts_dir(self) -> Path:
        return self.runtime_root / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_root / "logs"

    @property
    def temp_dir(self) -> Path:
        return self.runtime_root / "temp"

    def validated(self) -> "Settings":
        root = self.runtime_root.expanduser().resolve(strict=False)
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than lease_seconds")
        object.__setattr__(self, "runtime_root", root)
        return self

    def ensure_directories(self) -> None:
        for path in (
            self.runtime_root,
            self.uploads_dir,
            self.staging_dir,
            self.artifacts_dir,
            self.logs_dir,
            self.temp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def require_runtime_path(self, path: Path) -> Path:
        resolved = path.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(self.runtime_root)
        except ValueError as error:
            raise PathBoundaryError(
                f"path is outside runtime root: {resolved}"
            ) from error
        return resolved

