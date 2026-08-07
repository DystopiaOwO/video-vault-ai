from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from video_vault.database import add_analysis, connect, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_dir, project_detail
from video_vault.project_lifecycle import CancellationRequested, current_revision
from video_vault.story_calibration import compute_calibration, reset_calibration
from video_vault.story_generation import (
    StoryGenerationError,
    StoryValidationError,
    apply_story_generation_to_storyboard,
    generate_project_story,
    get_story_generation,
    recover_interrupted_story_generations,
    update_story_generation_review,
    validate_story_output,
)
from video_vault.story_input import build_story_input_snapshot, story_input_hash
from video_vault.story_profiles import load_project_story_settings, save_project_story_settings
from video_vault.storyboard import generate_storyboard, load_storyboard


def _fixture(tmp_path: Path, *, second_project: bool = False):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "travel.mp4"
    source.write_bytes(b"immutable-source")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "travel",
            "duration_seconds": 30,
            "status": "uploaded",
        },
    )
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {
            "segments": [
                {
                    "start_seconds": 0,
                    "end_seconds": 8,
                    "segment_type": "scene",
                    "title": "抵達車站",
                    "reason": "arrival",
                    "tags": ["travel"],
                    "score": 0.9,
                    "suggested_use": "main",
                },
                {
                    "start_seconds": 10,
                    "end_seconds": 18,
                    "segment_type": "detail",
                    "title": "咖啡廳",
                    "reason": "cafe",
                    "tags": ["food"],
                    "score": 0.8,
                    "suggested_use": "B-roll",
                },
            ]
        },
        tmp_path / "raw.json",
    )
    project_id = create_project(db, "旅行故事", [video_id], category="travel", content_type="travel_diary")
    cfg = {
        "library_root": str(tmp_path),
        "story": {"provider": "mock"},
    }
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    other_id = create_project(db, "另一個專案", [video_id], category="travel", content_type="travel_diary") if second_project else None
    if other_id:
        build_project_plan(cfg, db, other_id)
        generate_storyboard(cfg, db, other_id)
    return cfg, db, project_id, other_id


def test_story_input_is_deterministic_and_has_no_media_paths(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    first = build_story_input_snapshot(cfg, db, project_id)
    second = build_story_input_snapshot(cfg, db, project_id)
    assert first == second
    assert first["input_hash"] == story_input_hash(first)

    def assert_safe(value):
        if isinstance(value, dict):
            assert not set(value) & {"source_file", "source_path", "frame_path", "image_url", "image_base64", "frame_bytes"}
            for child in value.values():
                assert_safe(child)
        elif isinstance(value, list):
            for child in value:
                assert_safe(child)

    assert_safe(first)


def test_story_input_hash_changes_with_human_story_settings(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    before = build_story_input_snapshot(cfg, db, project_id)
    settings = load_project_story_settings(cfg, db, project_id)
    save_project_story_settings(cfg, db, project_id, {**settings, "desired_pacing": "更慢、保留等待"})
    after = build_story_input_snapshot(cfg, db, project_id)
    assert after["input_hash"] != before["input_hash"]


def test_story_generation_is_cached_and_does_not_auto_apply_storyboard(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    before_storyboard = load_storyboard(cfg, project_id)
    before_revision = current_revision(db, project_id)
    first = generate_project_story(cfg, db, project_id)
    assert first["status"] == "succeeded"
    assert first["cache_hit"] is False
    assert load_storyboard(cfg, project_id) == before_storyboard
    assert current_revision(db, project_id) == before_revision

    second = generate_project_story(cfg, db, project_id)
    assert second["status"] == "succeeded"
    assert second["cache_hit"] is True
    assert second["input_hash"] == first["input_hash"]


def test_cancelled_generation_does_not_replace_last_success(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    successful = generate_project_story(cfg, db, project_id)
    with pytest.raises(CancellationRequested):
        generate_project_story(cfg, db, project_id, force=True, should_cancel=lambda: True)
    detail = project_detail(cfg, db, project_id)
    assert detail["story"]["last_successful_story_generation_uuid"] == successful["story_generation_uuid"]
    with connect(db) as con:
        latest = con.execute(
            "select status from story_generations where project_id=? order by generation desc limit 1",
            (project_id,),
        ).fetchone()
    assert latest["status"] == "cancelled"


def test_restart_recovery_marks_inflight_generation_interrupted(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    successful = generate_project_story(cfg, db, project_id)
    calls = {"count": 0}

    def cancel_after_provider():
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(CancellationRequested):
        generate_project_story(cfg, db, project_id, force=True, should_cancel=cancel_after_provider)
    with connect(db) as con:
        interrupted = con.execute(
            "select story_generation_uuid from story_generations where project_id=? and story_generation_uuid<>? order by generation desc limit 1",
            (project_id, successful["story_generation_uuid"]),
        ).fetchone()
        con.execute(
            "update story_generations set status='publishing' where story_generation_uuid=?",
            (interrupted["story_generation_uuid"],),
        )
        con.execute(
            "update projects set current_story_generation_uuid=? where id=?",
            (interrupted["story_generation_uuid"], project_id),
        )
    assert recover_interrupted_story_generations(db) == 1
    recovered = get_story_generation(db, interrupted["story_generation_uuid"])
    assert recovered["status"] == "interrupted"
    assert project_detail(cfg, db, project_id)["story"]["current_generation"]["story_generation_uuid"] == successful["story_generation_uuid"]
    assert project_detail(cfg, db, project_id)["story"]["last_successful_story_generation_uuid"] == successful["story_generation_uuid"]


def test_story_review_payload_is_validated_against_input_snapshot(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    with pytest.raises(StoryValidationError, match="不存在或跨專案"):
        update_story_generation_review(
            db,
            generation["story_generation_uuid"],
            {"chapters": [{"title": "錯誤", "purpose": "錯誤", "segment_uuids": ["not-owned"], "confidence": 0.5}]},
            project_id=project_id,
        )


def test_story_apply_is_explicit_and_preserves_locked_segment(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    state = load_storyboard(cfg, project_id)
    locked_id = next(iter(state["segments"]))
    state["segments"][locked_id]["locked"] = True
    state["segments"][locked_id]["notes"] = "人工固定"
    from video_vault.storyboard import update_storyboard

    update_storyboard(cfg, db, project_id, state)
    generation = generate_project_story(cfg, db, project_id)
    result = apply_story_generation_to_storyboard(cfg, db, project_id, generation["story_generation_uuid"])
    assert result["storyboard"]["segments"][locked_id]["locked"] is True
    assert result["storyboard"]["segments"][locked_id]["notes"] == "人工固定"


def test_story_review_and_apply_reject_generation_from_another_project(tmp_path: Path):
    cfg, db, project_id, other_id = _fixture(tmp_path, second_project=True)
    generation = generate_project_story(cfg, db, project_id)
    from video_vault.story_generation import update_story_generation_review

    with pytest.raises(StoryGenerationError, match="不屬於指定專案"):
        update_story_generation_review(db, generation["story_generation_uuid"], {}, project_id=other_id)
    with pytest.raises(StoryGenerationError, match="不屬於指定專案"):
        apply_story_generation_to_storyboard(cfg, db, other_id, generation["story_generation_uuid"])


def test_story_output_rejects_unknown_duplicate_and_missing_segment_ids(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    ids = [item["segment_uuid"] for item in snapshot["segments"]]
    output = {
        "schema_version": 1,
        "story_profile": snapshot["story_profile_id"],
        "project_summary": "摘要",
        "overall_confidence": 0.8,
        "chapters": [{"title": "章節", "purpose": "整理", "segment_uuids": [ids[0], "not-owned"], "confidence": 0.8}],
    }
    with pytest.raises(StoryValidationError, match="不存在或跨專案"):
        validate_story_output(output, snapshot)
    output["chapters"][0]["segment_uuids"] = ids[:1]
    with pytest.raises(StoryValidationError, match="遺漏可納入片段"):
        validate_story_output(output, snapshot)


def test_calibration_uses_approved_outputs_only_and_can_reset(tmp_path: Path):
    records = [
        {"approved": False, "story_profile": "travel_diary", "shot_durations": [1, 2], "segment_count": 2},
        {"approved": True, "story_profile": "travel_diary", "shot_durations": [2, 4], "segment_count": 2, "title_card_count": 1},
    ]
    calibration = compute_calibration(records, "travel_diary")
    assert calibration["status"] == "ready"
    assert calibration["sample_count"] == 1
    assert calibration["metrics"]["shot_duration_median"] == 3
    assert compute_calibration(records[:1], "travel_diary")["status"] == "insufficient_data"
    from video_vault.story_calibration import save_calibration, calibration_path

    save_calibration({"library_root": str(tmp_path)}, "travel_diary", calibration)
    assert calibration_path({"library_root": str(tmp_path)}, "travel_diary").is_file()
    reset_calibration({"library_root": str(tmp_path)}, "travel_diary")
    assert not calibration_path({"library_root": str(tmp_path)}, "travel_diary").exists()
