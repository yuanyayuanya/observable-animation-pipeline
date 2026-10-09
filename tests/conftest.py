from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(runtime_root=tmp_path / "runtime").validated()
    value.ensure_directories()
    return value

