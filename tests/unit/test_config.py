from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.domain.errors import PathBoundaryError


def test_settings_create_runtime_layout(tmp_path: Path) -> None:
    settings = Settings(runtime_root=tmp_path / "runtime").validated()

    settings.ensure_directories()

    assert settings.database_path.parent == settings.runtime_root
    assert settings.uploads_dir.is_dir()
    assert settings.staging_dir.is_dir()
    assert settings.artifacts_dir.is_dir()
    assert settings.logs_dir.is_dir()
    assert settings.temp_dir.is_dir()


def test_runtime_path_rejects_escape(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(PathBoundaryError):
        settings.require_runtime_path(tmp_path / "outside.txt")


def test_heartbeat_must_be_shorter_than_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        Settings(
            runtime_root=tmp_path,
            heartbeat_seconds=15,
            lease_seconds=15,
        ).validated()

