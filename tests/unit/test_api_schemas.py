from datetime import UTC, datetime

from app.api.schemas import TaskDetail


def test_task_detail_serializes_nested_pipeline_state() -> None:
    now = datetime.now(UTC)
    detail = TaskDetail(
        id="tsk_demo",
        status="RUNNING",
        progress=25,
        filename="demo.mp4",
        created_at=now,
        updated_at=now,
        input_sha256="a" * 64,
        params={"frame_count": 8},
        steps=[
            {
                "id": "stp_demo",
                "key": "extract_frames",
                "ordinal": 2,
                "status": "RUNNING",
                "progress": 50,
                "attempt_count": 1,
                "max_attempts": 3,
            }
        ],
    )

    payload = detail.model_dump(mode="json")

    assert payload["steps"][0]["key"] == "extract_frames"
    assert payload["steps"][0]["progress"] == 50
    assert payload["artifacts"] == []

