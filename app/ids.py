from __future__ import annotations

from uuid import uuid4


_ALLOWED_PREFIXES = {"tsk", "stp", "att", "art", "evt", "wrk"}


def new_id(prefix: str) -> str:
    if prefix not in _ALLOWED_PREFIXES:
        raise ValueError(f"unsupported id prefix: {prefix}")
    return f"{prefix}_{uuid4().hex}"

