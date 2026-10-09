from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class FaultPlan:
    step: str
    mode: str
    at_progress: int
    identity: str


DOCUMENTED_FAULT_PLANS: Mapping[str, FaultPlan] = {
    "analyze_fail_once_at_50": FaultPlan(
        step="analyze_frames",
        mode="temporary_failure_once",
        at_progress=50,
        identity="analyze_fail_once_at_50",
    ),
    "analyze_timeout_once": FaultPlan(
        step="analyze_frames",
        mode="timeout_once",
        at_progress=50,
        identity="analyze_timeout_once",
    ),
    "contact_sheet_permanent_fail": FaultPlan(
        step="build_contact_sheet",
        mode="permanent_fail",
        at_progress=0,
        identity="contact_sheet_permanent_fail",
    ),
}


def normalize_fault_plan(raw: Any, step_key: str) -> FaultPlan | None:
    """Normalize API strings and flexible mapping forms for one pipeline step."""
    if not raw or raw == "none":
        return None
    if isinstance(raw, str):
        documented = DOCUMENTED_FAULT_PLANS.get(raw)
        if documented is not None:
            return documented if documented.step == step_key else None
        return FaultPlan(step_key, raw, 50, raw)
    if not isinstance(raw, Mapping):
        return None

    nested = raw.get(step_key)
    if nested is not None:
        if isinstance(nested, str):
            documented = DOCUMENTED_FAULT_PLANS.get(nested)
            if documented is not None:
                return documented if documented.step == step_key else None
            return FaultPlan(step_key, nested, 50, nested)
        if isinstance(nested, Mapping):
            return _from_mapping(nested, step_key)

    configured_step = str(raw.get("step", step_key))
    if configured_step != step_key or "mode" not in raw:
        return None
    return _from_mapping(raw, step_key)


def _from_mapping(raw: Mapping[str, Any], step_key: str) -> FaultPlan:
    mode = str(raw.get("mode", ""))
    progress = max(0, min(100, int(raw.get("at_progress", 50))))
    identity = str(raw.get("id", mode))
    return FaultPlan(step_key, mode, progress, identity)
