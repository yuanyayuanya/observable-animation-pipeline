from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import Settings
from app.domain.errors import PermanentInputError


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    original_filename: str
    relative_path: str
    sha256: str
    size_bytes: int


def store_upload(
    stream: BinaryIO,
    filename: str | None,
    task_id: str,
    settings: Settings,
) -> UploadReceipt:
    """Stream an upload into the runtime tree without buffering it in memory."""
    original = Path(filename or "upload.mp4").name
    suffix = Path(original).suffix.lower()
    if suffix not in {".mp4", ".mov"}:
        raise PermanentInputError("only .mp4 and .mov uploads are supported")

    task_dir = settings.require_runtime_path(settings.uploads_dir / task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.require_runtime_path(task_dir / f"source{suffix}")
    staging = settings.require_runtime_path(task_dir / ".source.uploading")
    digest = hashlib.sha256()
    size = 0

    try:
        with staging.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise PermanentInputError(
                        f"upload exceeds {settings.max_upload_bytes} bytes"
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise PermanentInputError("uploaded video is empty")
        os.replace(staging, destination)
    except Exception:
        staging.unlink(missing_ok=True)
        raise

    return UploadReceipt(
        original_filename=original,
        relative_path=destination.relative_to(settings.runtime_root).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )

