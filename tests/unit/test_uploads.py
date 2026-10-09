from io import BytesIO

import pytest

from app.config import Settings
from app.domain.errors import PermanentInputError
from app.uploads import store_upload


def test_store_upload_streams_and_hashes_under_runtime(tmp_path) -> None:
    settings = Settings(runtime_root=tmp_path, max_upload_bytes=16).validated()
    settings.ensure_directories()

    receipt = store_upload(BytesIO(b"video"), "../demo.mp4", "tsk_demo", settings)

    assert receipt.original_filename == "demo.mp4"
    assert receipt.relative_path == "uploads/tsk_demo/source.mp4"
    assert (tmp_path / receipt.relative_path).read_bytes() == b"video"


def test_store_upload_removes_partial_file_when_limit_is_exceeded(tmp_path) -> None:
    settings = Settings(runtime_root=tmp_path, max_upload_bytes=4).validated()
    settings.ensure_directories()

    with pytest.raises(PermanentInputError, match="exceeds"):
        store_upload(BytesIO(b"12345"), "demo.mp4", "tsk_demo", settings)

    assert not (tmp_path / "uploads" / "tsk_demo" / ".source.uploading").exists()
    assert not (tmp_path / "uploads" / "tsk_demo" / "source.mp4").exists()

