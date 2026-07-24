from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from video_vault.database import (
    init_db,
    project,
    project_videos,
    replace_segments,
    segments,
    set_project_videos,
    upsert_video,
)
from video_vault.project import build_project_plan, create_project, project_segments
from video_vault.segment_state_migration import migrate_segment_state_for_video


def _segment(
    start: float,
    end: float,
    *,
    title: str = "shot",
    tags: tuple[str, ...] = ("coffee",),
    segment_type: str = "key_action",
) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "segment_type": segment_type,
        "title": title,
        "reason": "useful",
        "tags": list(tags),
        "score": 0.9,
        "suggested_use": "main",
    }


def _video(db: Path, path: Path, *, category: str = "coffee", duration: float = 30) -> int:
    path.write_bytes(path.name.encode("utf-8"))
    return upsert_video(
        db,
        {
            "original_path": str(path),
            "current_path": str(path),
            "filename": path.name,
            "category": category,
            "duration_seconds": duration,
        },
    )


def test_small_timing_drift_and_metadata_changes_preserve_segment_uuid(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "clip.mp4")

    replace_segments(db, video_id, [_segment(12.0, 18.0, title="pour", tags=("coffee", "hands"))])
    first = dict(segments(db, video_id)[0])
    UUID(first["segment_uuid"])

    report = replace_segments(
        db,
        video_id,
        [_segment(12.04, 18.04, title="changed title", tags=("steam", "detail"))],
    )
    second = dict(segments(db, video_id)[0])

    assert second["segment_uuid"] == first["segment_uuid"]
    assert second["revision"] == 2
    assert report["matched"][0]["kind"] == "one_to_one"
    assert report["requires_review"] is False


def test_split_preserves_primary_uuid_and_creates_stable_child(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "clip.mp4")
    replace_segments(db, video_id, [_segment(0, 10)])
    parent_uuid = str(segments(db, video_id)[0]["segment_uuid"])

    report = replace_segments(db, video_id, [_segment(0, 5), _segment(5, 10)])
    first_ids = [str(row["segment_uuid"]) for row in segments(db, video_id)]

    assert report["requires_review"] is True
    assert report["splits"]
    assert parent_uuid in first_ids
    assert len(set(first_ids)) == 2
    assert all(item["requires_review"] for item in report["splits"])

    replace_segments(db, video_id, [_segment(0, 5), _segment(5, 10)])
    second_ids = [str(row["segment_uuid"]) for row in segments(db, video_id)]
    assert second_ids == first_ids


def test_merge_and_removed_segments_are_reported_explicitly(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "clip.mp4")
    replace_segments(db, video_id, [_segment(0, 5), _segment(5, 10), _segment(20, 25)])
    previous_ids = {str(row["segment_uuid"]) for row in segments(db, video_id)}

    report = replace_segments(db, video_id, [_segment(0, 10), _segment(28, 30)])
    current_ids = {str(row["segment_uuid"]) for row in segments(db, video_id)}

    assert report["merges"]
    assert report["removed"]
    assert report["new"]
    assert report["requires_review"] is True
    assert previous_ids - current_ids


def test_project_media_uuid_survives_insert_delete_and_reorder(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    first = _video(db, tmp_path / "first.mp4")
    second = _video(db, tmp_path / "second.mp4")
    inserted = _video(db, tmp_path / "inserted.mp4")
    project_id = create_project(db, "identity", [first, second])
    original = {
        int(row["id"]): str(row["project_media_uuid"])
        for row in project_videos(db, project_id)
    }

    set_project_videos(db, project_id, [inserted, first, second])
    after_insert = list(project_videos(db, project_id))
    assert [int(row["id"]) for row in after_insert] == [inserted, first, second]
    assert str(after_insert[1]["project_media_uuid"]) == original[first]
    assert str(after_insert[2]["project_media_uuid"]) == original[second]

    set_project_videos(db, project_id, [second, first])
    after_delete_reorder = list(project_videos(db, project_id))
    assert [int(row["id"]) for row in after_delete_reorder] == [second, first]
    assert str(after_delete_reorder[0]["project_media_uuid"]) == original[second]
    assert str(after_delete_reorder[1]["project_media_uuid"]) == original[first]


def test_project_plan_and_contract_use_stable_ids(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "20260724_081500_coffee.mp4")
    replace_segments(db, video_id, [_segment(0, 5)])
    segment_uuid = str(segments(db, video_id)[0]["segment_uuid"])
    project_id = create_project(db, "identity", [video_id])
    cfg = {"library_root": str(tmp_path)}

    plan = build_project_plan(cfg, db, project_id)
    contract = project_segments(cfg, project_id, plan, apply_storyboard=False, db=db)

    assert plan["clips"][0]["project_media_id"]
    UUID(plan["clips"][0]["project_media_id"])
    assert plan["groups"][0]["segments"][0]["segment_id"] == segment_uuid
    assert contract[0]["segment_id"] == segment_uuid
    assert contract[0]["legacy_segment_id"] == "clip_001_00000000"
    assert (tmp_path / "08_projects" / f"project_{project_id}" / "clips" / "clip_001" / "clip.json").is_file()
    stable_dir = tmp_path / "08_projects" / f"project_{project_id}" / "clips" / f"media_{plan['clips'][0]['project_media_id']}"
    assert (stable_dir / "clip.json").is_file()


def test_reanalysis_migrates_user_state_and_preserves_unmatched_orphans(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    first_video = _video(db, tmp_path / "20260724_081500_coffee.mp4")
    other_video = _video(db, tmp_path / "20260724_121500_food.mp4", category="food")
    replace_segments(db, first_video, [_segment(0, 5)])
    replace_segments(db, other_video, [_segment(0, 4, tags=("food",))])
    stable_id = str(segments(db, first_video)[0]["segment_uuid"])
    other_id = str(segments(db, other_video)[0]["segment_uuid"])
    project_id = create_project(db, "identity", [first_video, other_video])
    cfg = {"library_root": str(tmp_path)}
    plan = build_project_plan(cfg, db, project_id)
    root = tmp_path / "08_projects" / f"project_{project_id}"

    # Simulate a project created before stable IDs were added.
    for group in plan["groups"]:
        for item in group["segments"]:
            item.pop("segment_id", None)
            item.pop("segment_revision", None)
    (root / "project_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    legacy_id = "clip_001_00000000"
    ghost_id = "clip_999_00000000"
    (root / "feedback" / "segment_review.json").write_text(
        json.dumps([{"segment_id": legacy_id, "include": False, "user_notes": "keep me"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "storyboard.json").write_text(
        json.dumps({"schema_version": 1, "groups": [], "segments": {legacy_id: {"included": False}, other_id: {"included": True}}}),
        encoding="utf-8",
    )
    (root / "audio_settings.json").write_text(
        json.dumps({"segments": {legacy_id: {"role": "mute"}, other_id: {"role": "keep"}, ghost_id: {"role": "lower"}}}),
        encoding="utf-8",
    )
    (root / "color_consistency.json").write_text(
        json.dumps({"segments": {legacy_id: {"enabled": False}, other_id: {"enabled": True}}}),
        encoding="utf-8",
    )

    identity_report = replace_segments(db, first_video, [_segment(0.04, 5.04)])
    reports = migrate_segment_state_for_video(cfg, db, first_video, identity_report)

    assert str(segments(db, first_video)[0]["segment_uuid"]) == stable_id
    review = json.loads((root / "feedback" / "segment_review.json").read_text(encoding="utf-8"))
    storyboard = json.loads((root / "storyboard.json").read_text(encoding="utf-8"))
    audio = json.loads((root / "audio_settings.json").read_text(encoding="utf-8"))
    color = json.loads((root / "color_consistency.json").read_text(encoding="utf-8"))

    assert review[0]["segment_id"] == stable_id
    assert stable_id in storyboard["segments"]
    assert stable_id in audio["segments"]
    assert stable_id in color["segments"]
    assert other_id in storyboard["segments"]
    assert other_id in audio["segments"]
    assert other_id in color["segments"]
    assert ghost_id in audio["segments"]
    assert any(item["segment_id"] == ghost_id for item in reports[0]["orphaned"])
    assert reports[0]["requires_review"] is True
    assert dict(project(db, project_id))["status"] == "needs_review"
    assert (root / "validation" / "segment_identity_migration_latest.json").is_file()
