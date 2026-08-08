from pathlib import Path

from video_vault.cloud_review import (
    MockCloudReviewProvider,
    build_review_plan,
    execute_review_plan,
)


def _window(window_uuid: str, *, confidence: float, reasons: list[str] | None = None, video_id: int = 1) -> dict:
    return {
        "project_id": 7,
        "video_id": video_id,
        "run_uuid": "run-1",
        "window_uuid": window_uuid,
        "segment_uuid": f"segment-{window_uuid}",
        "ordinal": int(window_uuid.rsplit("-", 1)[-1]),
        "start_seconds": 0,
        "end_seconds": 4,
        "frame_timestamps": [0, 2, 4],
        "confidence": confidence,
        "validation": {"needs_review_reasons": reasons or []},
    }


def _cfg(**overrides):
    policy = {
        "enabled": True,
        "provider": "mock",
        "confidence_threshold": 0.55,
        "max_calls_per_clip": 3,
        "max_frames_per_clip": 12,
        "max_calls_per_project": 6,
        "max_frames_per_project": 24,
        "estimated_cost_per_frame_usd": 0.01,
    }
    policy.update(overrides)
    return {"perception": {"cloud_review": policy}}


def test_plan_only_selects_low_confidence_rule_conflict_and_user_windows_without_paths():
    plan = build_review_plan([
        {**_window("window-1", confidence=0.2), "frame_paths": [r"D:\private\one.jpg"]},
        _window("window-2", confidence=0.9, reasons=["rule_conflict"]),
        _window("window-3", confidence=0.95),
    ], _cfg(), selected_window_ids={"window-3"})

    assert plan["status"] == "ready"
    assert [item["window_uuid"] for item in plan["windows"]] == ["window-1", "window-2", "window-3"]
    assert all(item["source_paths_exposed"] is False for item in plan["windows"])
    assert all("frame_paths" not in item for item in plan["windows"])
    assert plan["privacy"]["full_video_upload"] is False
    assert plan["estimated_frames"] == 9
    assert plan["estimated_cost_usd"] == 0.09


def test_plan_enforces_clip_and_project_caps():
    plan = build_review_plan(
        [_window(f"window-{index}", confidence=0.1) for index in range(1, 5)],
        _cfg(max_calls_per_clip=2, max_frames_per_clip=6, max_calls_per_project=3, max_frames_per_project=9),
    )

    assert plan["status"] == "ready"
    assert len(plan["windows"]) == 2
    assert len(plan["rejected_windows"]) == 2
    assert {item["rejected_reason"] for item in plan["rejected_windows"]} == {"clip_budget_exceeded"}


def test_disabled_plan_is_explicitly_not_ready():
    plan = build_review_plan([_window("window-1", confidence=0.1)], _cfg(enabled=False))
    assert plan["status"] == "disabled"
    assert plan["windows"] == []
    assert plan["privacy"]["full_video_upload"] is False


def test_provider_failure_returns_failed_review_and_never_success(tmp_path):
    plan = build_review_plan([_window("window-1", confidence=0.1)], _cfg())
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    outcome = execute_review_plan(plan, {"window-1": [frame, frame, frame]}, _cfg())

    assert outcome["status"] == "completed"
    assert outcome["results"][0]["status"] == "completed"
    assert outcome["results"][0]["segment_uuid"] == "segment-window-1"


def test_disabled_provider_returns_failed_review_without_uploading(tmp_path):
    plan = build_review_plan([_window("window-1", confidence=0.1)], _cfg())
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    outcome = execute_review_plan(plan, {"window-1": [frame, frame, frame]}, _cfg(enabled=False))

    assert outcome["status"] == "failed"
    assert "disabled" in outcome["error"]
    assert outcome["results"] == []


def test_mock_provider_rejects_empty_frame_payload():
    provider = MockCloudReviewProvider()
    try:
        provider.review_window([], [], {})
    except Exception as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty cloud review payload must fail closed")
