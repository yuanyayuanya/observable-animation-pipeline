from __future__ import annotations

from app.config import Settings


def bootstrap_settings() -> Settings:
    settings = Settings.from_env()
    settings.ensure_directories()
    return settings

