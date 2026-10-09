from pathlib import Path

from app.config import Settings
from app.worker.runner import Worker


class EmptyCacheRepository:
    def find_cached_artifacts(self, cache_key: str) -> tuple[()]:
        return ()


def test_untracked_committed_cache_is_quarantined(tmp_path: Path) -> None:
    settings = Settings(runtime_root=tmp_path / "runtime").validated()
    worker = Worker(EmptyCacheRepository(), settings)  # type: ignore[arg-type]
    cache_key = "a" * 64
    cache_path = worker.store.cache_path(cache_key)
    cache_path.mkdir(parents=True)
    (cache_path / "result.json").write_text("{}", encoding="utf-8")

    worker._quarantine_untracked_cache(cache_key, True)

    assert not cache_path.exists()
    quarantined = list(worker.store.quarantine_root.glob(f"{cache_key}-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "result.json").read_text(encoding="utf-8") == "{}"

