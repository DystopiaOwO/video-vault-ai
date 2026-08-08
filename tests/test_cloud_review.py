from pathlib import Path

import video_vault.cloud_review as cloud_review

from video_vault.cloud_review import (
    MockCloudReviewProvider,
    OpenAICloudReviewProvider,
    build_review_plan,
    execute_review_plan,
)
from video_vault.database import init_db, project
from video_vault.project import create_project
import video_vault.ui as ui


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
        "max_estimated_cost_usd_per_clip": 0.12,
        "max_estimated_cost_usd_per_project": 0.24,
        "timeout_seconds": 60,
    }
    policy.update(overrides)
    return {"perception": {"cloud_review": policy}}


def test_plan_with_explicit_selection_is_exact_even_for_low_confidence_and_rule_conflict():
    plan = build_review_plan([
        {**_window("window-1", confidence=0.2), "frame_paths": [r"D:\private\one.jpg"]},
        _window("window-2", confidence=0.9, reasons=["rule_conflict"]),
        _window("window-3", confidence=0.95),
    ], _cfg(), selected_window_ids={"window-3"})

    assert plan["status"] == "ready"
    assert [item["window_uuid"] for item in plan["windows"]] == ["window-3"]
    assert all(item["source_paths_exposed"] is False for item in plan["windows"])
    assert all("frame_paths" not in item for item in plan["windows"])
    assert plan["privacy"]["full_video_upload"] is False
    assert plan["estimated_frames"] == 3
    assert plan["estimated_cost_usd"] == 0.03


def test_plan_without_selection_keeps_only_low_confidence_and_rule_conflict_windows():
    plan = build_review_plan([
        _window("window-1", confidence=0.2),
        _window("window-2", confidence=0.9, reasons=["rule_conflict"]),
        _window("window-3", confidence=0.95),
    ], _cfg())

    assert [item["window_uuid"] for item in plan["windows"]] == ["window-1", "window-2"]


def test_empty_selection_sends_no_windows_and_subset_does_not_auto_add_candidates():
    windows = [_window("window-1", confidence=0.2), _window("window-2", confidence=0.9, reasons=["rule_conflict"])]
    assert build_review_plan(windows, _cfg(), selected_window_ids=set())["windows"] == []
    subset = build_review_plan(windows, _cfg(), selected_window_ids={"window-2"})
    assert [item["window_uuid"] for item in subset["windows"]] == ["window-2"]


def test_plan_enforces_clip_and_project_caps():
    plan = build_review_plan(
        [_window(f"window-{index}", confidence=0.1) for index in range(1, 5)],
        _cfg(max_calls_per_clip=2, max_frames_per_clip=6, max_calls_per_project=3, max_frames_per_project=9),
    )

    assert plan["status"] == "ready"
    assert len(plan["windows"]) == 2
    assert len(plan["rejected_windows"]) == 2
    assert {item["rejected_reason"] for item in plan["rejected_windows"]} == {"clip_budget_exceeded"}


def test_plan_enforces_cost_ceiling_and_cumulative_usage():
    cost_limited = build_review_plan(
        [_window("window-1", confidence=0.1)],
        _cfg(max_estimated_cost_usd_per_clip=0.02, max_estimated_cost_usd_per_project=0.02),
    )
    assert cost_limited["status"] == "budget_exceeded"
    assert cost_limited["rejected_windows"][0]["rejected_reason"] == "clip_cost_budget_exceeded"

    already_spent = build_review_plan(
        [_window("window-1", confidence=0.1)],
        _cfg(max_calls_per_clip=2, max_frames_per_clip=6, max_calls_per_project=3, max_frames_per_project=9),
        usage={
            "calls": 1,
            "frames": 3,
            "estimated_cost_usd": 0.03,
            "by_clip": {"1": {"calls": 1, "frames": 3, "estimated_cost_usd": 0.03}},
        },
    )
    assert already_spent["status"] == "ready"
    assert already_spent["estimated_calls"] == 1
    exhausted = build_review_plan(
        [_window("window-1", confidence=0.1)],
        _cfg(max_calls_per_clip=1, max_frames_per_clip=3, max_calls_per_project=1, max_frames_per_project=3),
        usage={"calls": 1, "frames": 3, "estimated_cost_usd": 0.03, "by_clip": {"1": {"calls": 1, "frames": 3, "estimated_cost_usd": 0.03}}},
    )
    assert exhausted["status"] == "budget_exceeded"


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
    assert outcome["attempted_window_uuids"] == []


def test_unavailable_provider_fails_closed_and_preserves_local_result(tmp_path):
    plan = build_review_plan([_window("window-1", confidence=0.1)], _cfg())
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    outcome = execute_review_plan(plan, {"window-1": [frame, frame, frame]}, _cfg(provider="unavailable"))

    assert outcome["status"] == "failed"
    assert outcome["local_result_preserved"] is True
    assert outcome["project_needs_review"] is True


def test_timeout_or_quota_provider_failure_never_becomes_success(monkeypatch, tmp_path):
    class FailingProvider:
        name = "mock"
        model = "test"

        def review_window(self, *_args, **_kwargs):
            raise TimeoutError("timeout or quota exceeded")

    monkeypatch.setattr(cloud_review, "cloud_review_provider", lambda _cfg: FailingProvider())
    plan = build_review_plan([_window("window-1", confidence=0.1)], _cfg())
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    outcome = execute_review_plan(plan, {"window-1": [frame, frame, frame]}, _cfg())

    assert outcome["status"] == "failed"
    assert outcome["completed_count"] == 0
    assert outcome["local_result_preserved"] is True
    assert outcome["project_needs_review"] is True


def test_ui_unavailable_review_preserves_local_result_and_marks_project_needs_review(monkeypatch, tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project(db, "cloud review", [])
    staging = tmp_path / "staging"
    staging.mkdir()
    frame = staging / "frame.jpg"
    frame.write_bytes(b"frame")
    candidate = {**_window("window-1", confidence=0.1), "frame_paths": [str(frame)]}
    states = {1: {"current_cloud_review": {}, "current_analysis_run_uuid": "run-1"}}
    saved: list[dict] = []
    monkeypatch.setattr(ui, "_cloud_review_candidates", lambda *_args: [candidate])
    monkeypatch.setattr(ui, "perception_states_for_project", lambda *_args: states)
    monkeypatch.setattr(ui, "analysis_run", lambda *_args: {"staging_path": str(staging)})
    monkeypatch.setattr(ui, "set_run_cloud_review", lambda _db, _run, review: saved.append(review))

    cfg = {"library_root": str(tmp_path), **_cfg()}
    result = ui._cloud_review_execute(cfg, db, project_id, {"base_revision": 1, "window_uuids": ["window-1"]})

    assert result["ok"] is False
    assert result["review_status"] == "failed"
    assert result["local_result_preserved"] is True
    assert project(db, project_id)["status"] == "needs_review"
    assert saved and saved[0]["status"] == "failed"


def test_openai_cloud_review_uses_perception_timeout_at_request_layer():
    provider = OpenAICloudReviewProvider({
        "perception": {"cloud_review": {"enabled": True, "provider": "openai", "timeout_seconds": 7}},
        "ai": {"cloud": {"api_key_env": "MISSING_TEST_KEY"}},
    })
    assert provider._provider.timeout_seconds == 7


def test_mock_provider_rejects_empty_frame_payload():
    provider = MockCloudReviewProvider()
    try:
        provider.review_window([], [], {})
    except Exception as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty cloud review payload must fail closed")
