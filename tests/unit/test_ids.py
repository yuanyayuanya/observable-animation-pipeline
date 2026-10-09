from __future__ import annotations

import pytest

from app.ids import new_id


def test_new_id_has_prefix_and_unique_value() -> None:
    first = new_id("tsk")
    second = new_id("tsk")

    assert first.startswith("tsk_")
    assert first != second


def test_new_id_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="unsupported id prefix"):
        new_id("unknown")

