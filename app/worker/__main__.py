from __future__ import annotations

import logging

from app.bootstrap import bootstrap_settings
from app.db.engine import create_schema, create_session_factory, create_sqlite_engine
from app.db.repository import PipelineRepository
from app.worker.runner import Worker


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = bootstrap_settings()
    engine = create_sqlite_engine(settings.database_path)
    create_schema(engine)
    repository = PipelineRepository(
        create_session_factory(engine), lease_seconds=settings.lease_seconds
    )
    Worker(repository, settings).run_forever()


if __name__ == "__main__":
    main()

