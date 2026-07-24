from __future__ import annotations

from pathlib import Path

from video_vault.database import (
    add_frame,
    connect,
    init_db,
    replace_segments,
    update_project_media_summary,
    upsert_video,
)
from video_vault.project import build_project_plan, create_project


def test_unrecognized_user_note_is_recorded_without_false_applied_guidance(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "clip.mp4"
    frame_path = tmp_path / "frame.jpg"
    source.write_bytes(b"source")
    frame_path.write_bytes(b"frame")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 5,
            "status": "perceived",
        },
    )
    add_frame(db, video_id, frame_path, 0)
    with connect(db) as con:
        con.execute(
            """update frames
            set vision_summary='早餐桌上的咖啡', tags='food,coffee',
                score_visual_quality=0.8, score_usefulness=0.9
            where video_id=?""",
            (video_id,),
        )
    replace_segments(
        db,
        video_id,
        [
            {
                "start_seconds": 0,
                "end_seconds": 5,
                "segment_type": "key_action",
                "title": "coffee",
                "reason": "food",
                "tags": ["food", "coffee"],
                "score": 0.9,
                "suggested_use": "main",
            }
        ],
    )
    project_id = create_project(db, "旅行", [video_id])
    assert update_project_media_summary(db, project_id, video_id, "這段是我很喜歡的畫面")

    plan = build_project_plan({"library_root": str(tmp_path)}, db, project_id)

    usage = plan["story_context_usage"][0]
    assert usage["activity"] == "飲食"
    assert usage["activity_source"] == "ai_tags"
    assert usage["guidance_applied"] is False
    assert plan["feedback_applied"] == []
    assert any(group["activity"] == "飲食" for group in plan["groups"])
