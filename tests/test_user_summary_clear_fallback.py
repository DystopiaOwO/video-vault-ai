from __future__ import annotations

from pathlib import Path

from video_vault.database import (
    add_frame,
    frames,
    init_db,
    project_videos,
    update_frame_analysis,
    update_project_media_summary,
    upsert_video,
)
from video_vault.project import create_project, project_detail


def test_clearing_user_context_restores_ai_effective_summary_without_touching_frame(tmp_path: Path):
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
            "category": "travel",
            "duration_seconds": 5,
            "status": "uploaded",
        },
    )
    add_frame(db, video_id, frame_path, 0)
    frame_id = int(frames(db, video_id)[0]["id"])
    update_frame_analysis(
        db,
        frame_id,
        {
            "summary": "AI 看見車站入口",
            "tags": ["travel", "station"],
            "visual_quality_score": 0.8,
            "usefulness_score": 0.9,
        },
    )
    project_id = create_project(db, "旅行", [video_id])
    cfg = {"library_root": str(tmp_path)}

    assert update_project_media_summary(db, project_id, video_id, "這段是抵達旅館")
    with_user = project_detail(cfg, db, project_id)["clips"][0]
    assert with_user["effective_summary"] == "這段是抵達旅館"
    assert with_user["effective_summary_source"] == "user"

    assert update_project_media_summary(db, project_id, video_id, "")
    cleared = project_detail(cfg, db, project_id)["clips"][0]
    assert cleared["user_summary"] == ""
    assert cleared["effective_summary"] == "AI 看見車站入口"
    assert cleared["effective_summary_source"] == "ai"
    assert frames(db, video_id)[0]["vision_summary"] == "AI 看見車站入口"
    assert project_videos(db, project_id)[0]["user_summary_migration_state"] == "native"
