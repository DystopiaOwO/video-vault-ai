import json
from pathlib import Path

import pytest

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_detail, project_segments, save_segment_review, set_review_status, update_segment_timing
from video_vault.render_manifest import build_render_manifest
from video_vault.storyboard import generate_storyboard, load_storyboard, storyboard_thumbnail_path, update_storyboard, validate_storyboard


def _project(tmp_path: Path, count: int = 2) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_ids = []
    for index in range(count):
        source = tmp_path / f"travel-{index}.mp4"
        source.write_bytes(b"source")
        video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 20})
        add_analysis(db, video_id, "mock", "rules", {"segments": [
            {"start_seconds": 0, "end_seconds": 8, "segment_type": "key_action", "title": f"first-{index}", "reason": "arrive", "tags": ["travel"], "score": 0.9, "suggested_use": "main"},
            {"start_seconds": 10, "end_seconds": 16, "segment_type": "detail", "title": f"second-{index}", "reason": "meal", "tags": ["food"], "score": 0.8, "suggested_use": "B-roll"},
        ]}, tmp_path / f"raw-{index}.json")
        video_ids.append(video_id)
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "Storyboard test", video_ids, category="travel", content_type="travel_diary")
    build_project_plan(cfg, db, project_id)
    return cfg, db, project_id


def test_storyboard_generation_preserves_locked_segments(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id].update({"locked": True, "order": 99, "notes": "人工保留"})
    update_storyboard(cfg, db, project_id, state)

    regenerated = generate_storyboard(cfg, db, project_id, force=True)

    assert regenerated["segments"][segment_id]["locked"] is True
    assert regenerated["segments"][segment_id]["order"] == 99
    assert regenerated["segments"][segment_id]["notes"] == "人工保留"


def test_storyboard_group_order_and_segment_order_drive_project_segments(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    group_id = state["groups"][0]["group_id"]
    ids = list(state["segments"])
    state["segments"][ids[0]]["group_id"] = group_id
    state["segments"][ids[0]]["order"] = 2
    state["segments"][ids[1]]["group_id"] = group_id
    state["segments"][ids[1]]["order"] = 1
    update_storyboard(cfg, db, project_id, state)

    rows = project_segments(cfg, project_id, json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "project_plan.json").read_text(encoding="utf-8")))
    assert [row["segment_id"] for row in rows[:2]] == [ids[1], ids[0]]


def test_excluded_storyboard_segment_is_not_in_manifest(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    excluded = next(iter(state["segments"]))
    state["segments"][excluded]["included"] = False
    update_storyboard(cfg, db, project_id, state)

    manifest = build_render_manifest(cfg, db, project_id)
    assert excluded not in {item["segment_id"] for item in manifest["segments"]}


def test_storyboard_change_invalidates_approval(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    generate_storyboard(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    assert project_detail(cfg, db, project_id)["can_render"] is True
    path = tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json"
    state = load_storyboard(cfg, project_id)
    state["segments"][next(iter(state["segments"]))]["included"] = False
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    assert project_detail(cfg, db, project_id)["can_render"] is False


def test_storyboard_validation_requires_included_segment_and_unique_order(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    for item in state["segments"].values():
        item["included"] = False
    result = validate_storyboard(state, project_segments(cfg, project_id, json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "project_plan.json").read_text(encoding="utf-8")), apply_storyboard=False))
    assert result["valid"] is False
    assert any("included" in error for error in result["errors"])


def test_storyboard_media_endpoint_blocks_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError):
        storyboard_thumbnail_path({"library_root": str(tmp_path)}, 1, "../secret.jpg")


@pytest.mark.parametrize("field", ["notes", "thumbnail_time_ratio", "locked"])
def test_storyboard_ui_metadata_does_not_invalidate_manifest_hash(tmp_path: Path, field: str):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    from video_vault.render_manifest import build_render_manifest

    before = build_render_manifest(cfg, db, project_id)["manifest_hash"]
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id][field] = "note" if field == "notes" else (0.75 if field == "thumbnail_time_ratio" else True)
    (tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    after = build_render_manifest(cfg, db, project_id)["manifest_hash"]
    assert after == before


def test_storyboard_render_state_change_invalidates_manifest_hash(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    from video_vault.render_manifest import build_render_manifest

    before = build_render_manifest(cfg, db, project_id)["manifest_hash"]
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id]["included"] = False
    other_id = next(key for key in state["segments"] if key != segment_id)
    state["segments"][other_id]["order"] = 1
    (tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    assert build_render_manifest(cfg, db, project_id)["manifest_hash"] != before


def test_force_storyboard_recomputes_unlocked_suggestion_but_preserves_manual_state(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    ids = list(state["segments"])
    state["segments"][ids[0]]["manual_group"] = True
    state["segments"][ids[0]]["manual_order"] = True
    state["segments"][ids[0]]["notes"] = "人工說明"
    update_storyboard(cfg, db, project_id, state)
    regenerated = generate_storyboard(cfg, db, project_id, force=True)
    assert regenerated["segments"][ids[0]]["manual_group"] is True
    assert regenerated["segments"][ids[0]]["manual_order"] is True
    assert regenerated["segments"][ids[0]]["notes"] == "人工說明"


def _approved_storyboard(tmp_path: Path) -> tuple[dict, Path, int, dict]:
    cfg, db, project_id = _project(tmp_path, count=1)
    state = generate_storyboard(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    assert project_detail(cfg, db, project_id)["can_render"] is True
    return cfg, db, project_id, state


def test_note_save_preserves_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id]["notes"] = "補充地點說明"
    result = update_storyboard(cfg, db, project_id, state, return_result=True)
    assert result["render_changed"] is False
    assert result["approval_invalidated"] is False
    assert project_detail(cfg, db, project_id)["can_render"] is True


def test_lock_save_preserves_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id]["locked"] = True
    update_storyboard(cfg, db, project_id, state)
    assert project_detail(cfg, db, project_id)["can_render"] is True


def test_thumbnail_ratio_save_preserves_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    segment_id = next(iter(state["segments"]))
    state["segments"][segment_id]["thumbnail_time_ratio"] = 0.75
    update_storyboard(cfg, db, project_id, state)
    assert project_detail(cfg, db, project_id)["can_render"] is True


def test_group_title_save_preserves_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    state["groups"][0]["title"] = "抵達車站"
    update_storyboard(cfg, db, project_id, state)
    assert project_detail(cfg, db, project_id)["can_render"] is True


def test_order_save_invalidates_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    ids = list(state["segments"])
    state["segments"][ids[0]]["order"], state["segments"][ids[1]]["order"] = 2, 1
    result = update_storyboard(cfg, db, project_id, state, return_result=True)
    assert result["render_changed"] is True
    assert project_detail(cfg, db, project_id)["can_render"] is False


def test_included_save_invalidates_approved_project(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    state["segments"][next(iter(state["segments"]))]["included"] = False
    result = update_storyboard(cfg, db, project_id, state, return_result=True)
    assert result["approval_invalidated"] is True
    assert project_detail(cfg, db, project_id)["can_render"] is False


def test_timing_patch_only_changes_requested_segment(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    rows = project_detail(cfg, db, project_id)["segments"]
    target = dict(rows[0])
    target.update({"include": False, "manual_order": 7, "scene_role": "arrival", "story_position": "opening", "audio_role": "mute", "user_notes": "保留人工筆記", "start_seconds": 1, "end_seconds": 6, "speed": 1})
    save_segment_review(cfg, db, project_id, [target])
    update_segment_timing(cfg, db, project_id, target["segment_id"], 2.2, 5.8, 1.25)
    review = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "feedback" / "segment_review.json").read_text(encoding="utf-8"))[0]
    assert review["start_seconds"] == 2.2
    assert review["end_seconds"] == 5.8
    assert review["speed"] == 1.25
    assert review["include"] is False
    assert review["manual_order"] == 1
    assert review["scene_role"] == "arrival"
    assert review["story_position"] == "opening"
    assert review["audio_role"] == "mute"
    assert review["user_notes"] == "保留人工筆記"


def test_timing_patch_preserves_segment_review_fields(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    row = dict(project_detail(cfg, db, project_id)["segments"][0])
    row.update({"include": True, "scene_role": "detail", "user_notes": "手動備註"})
    save_segment_review(cfg, db, project_id, [row])
    before = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "feedback" / "segment_review.json").read_text(encoding="utf-8"))[0]
    update_segment_timing(cfg, db, project_id, row["segment_id"], 3, 7, 0.5)
    after = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "feedback" / "segment_review.json").read_text(encoding="utf-8"))[0]
    for key in ("segment_id", "include", "scene_role", "user_notes", "manual_order"):
        assert after[key] == before[key]


def test_timing_patch_does_not_copy_storyboard_order(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    storyboard = generate_storyboard(cfg, db, project_id)
    segment_id = next(iter(storyboard["segments"]))
    storyboard["segments"][segment_id]["order"] = 2
    update_storyboard(cfg, db, project_id, storyboard)
    update_segment_timing(cfg, db, project_id, segment_id, 1, 4, 1.5)
    review = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "feedback" / "segment_review.json").read_text(encoding="utf-8"))
    assert "group_id" not in review[0]
    assert review[0].get("manual_order") != 99


def test_timing_patch_invalidates_approval(tmp_path: Path):
    cfg, db, project_id, state = _approved_storyboard(tmp_path)
    segment_id = next(iter(state["segments"]))
    update_segment_timing(cfg, db, project_id, segment_id, 1, 4, 1.25)
    assert project_detail(cfg, db, project_id)["can_render"] is False


def test_timing_patch_rejects_source_overflow(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    segment_id = project_detail(cfg, db, project_id)["segments"][0]["segment_id"]
    with pytest.raises(ValueError, match="超過來源長度"):
        update_segment_timing(cfg, db, project_id, segment_id, 1, 21, 1)
