from __future__ import annotations

from pathlib import Path

from video_vault.database import (
    add_frame,
    connect,
    frames,
    init_db,
    project_videos,
    replace_segments,
    update_frame_analysis,
    update_project_media_summary,
    update_video_summary,
    upsert_video,
    videos,
)
from video_vault.project import build_project_plan, create_project, project_detail
from video_vault.ui import update_clip_summary


def _cfg(root: Path) -> dict:
    return {
        "library_root": str(root),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "ai": {"provider": "mock", "model": "mock-v1"},
    }


def _video(db: Path, root: Path, name: str = "clip.mp4") -> tuple[int, Path]:
    source = root / name
    source.write_bytes(b"source")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "travel",
            "duration_seconds": 10,
            "status": "uploaded",
        },
    )
    return video_id, source


def _frame(db: Path, root: Path, video_id: int, index: int, summary: str, tags: str = "food,coffee") -> int:
    path = root / f"frame-{video_id}-{index}.jpg"
    path.write_bytes(f"frame-{index}".encode())
    add_frame(db, video_id, path, index * 5)
    with connect(db) as con:
        row = con.execute(
            "select id from frames where video_id=? and frame_path=?",
            (video_id, str(path)),
        ).fetchone()
        assert row is not None
        frame_id = int(row["id"])
        con.execute(
            """update frames set vision_summary=?, tags=?,
                score_visual_quality=0.8, score_usefulness=0.9 where id=?""",
            (summary, tags, frame_id),
        )
    return frame_id


def _food_segment() -> dict:
    return {
        "start_seconds": 0,
        "end_seconds": 5,
        "segment_type": "key_action",
        "title": "breakfast coffee",
        "reason": "food scene",
        "tags": ["food", "coffee"],
        "score": 0.9,
        "suggested_use": "main",
    }


def test_user_summary_can_be_saved_before_perception_without_creating_frames(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)
    project_id = create_project(db, "A", [video_id])

    assert update_project_media_summary(db, project_id, video_id, "先保留：抵達飯店") is True

    assert frames(db, video_id) == []
    row = dict(project_videos(db, project_id)[0])
    assert row["user_summary"] == "先保留：抵達飯店"
    assert row["user_summary_updated_at"]
    assert row["user_summary_migration_state"] == "native"


def test_saving_and_clearing_user_summary_never_changes_ai_frame_rows(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)
    _frame(db, tmp_path, video_id, 0, "早餐桌上的咖啡")
    _frame(db, tmp_path, video_id, 1, "窗邊的早餐與街景")
    project_id = create_project(db, "A", [video_id])
    before = [dict(row) for row in frames(db, video_id)]

    assert update_project_media_summary(db, project_id, video_id, "這段是抵達飯店")
    assert [dict(row) for row in frames(db, video_id)] == before

    assert update_project_media_summary(db, project_id, video_id, "")
    assert [dict(row) for row in frames(db, video_id)] == before
    row = dict(project_videos(db, project_id)[0])
    assert row["user_summary"] == ""


def test_reanalysis_changes_ai_observations_but_preserves_user_summary(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)
    frame_id = _frame(db, tmp_path, video_id, 0, "舊 AI 觀察")
    project_id = create_project(db, "A", [video_id])
    assert update_project_media_summary(db, project_id, video_id, "使用者脈絡不應被覆蓋")

    update_frame_analysis(
        db,
        frame_id,
        {
            "summary": "新的 AI 觀察",
            "tags": ["travel"],
            "visual_quality_score": 0.9,
            "usefulness_score": 0.8,
        },
    )

    assert frames(db, video_id)[0]["vision_summary"] == "新的 AI 觀察"
    assert project_videos(db, project_id)[0]["user_summary"] == "使用者脈絡不應被覆蓋"


def test_story_plan_uses_user_context_with_explicit_provenance(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)
    _frame(db, tmp_path, video_id, 0, "早餐桌上的咖啡", "food,coffee")
    replace_segments(db, video_id, [_food_segment()])
    project_id = create_project(db, "A", [video_id])
    cfg = _cfg(tmp_path)

    assert update_clip_summary(
        cfg,
        db,
        project_id,
        video_id,
        "這段是抵達飯店，不要放在早餐",
    )

    plan = project_detail(cfg, db, project_id)["plan"]
    assert plan["story_context_usage"][0]["effective_summary_source"] == "user"
    assert plan["story_context_usage"][0]["activity"] == "抵達／住宿"
    assert "飲食" in plan["story_context_usage"][0]["avoided_activities"]
    assert any(group["activity"] == "抵達／住宿" for group in plan["groups"])
    assert not any(group["activity"] == "飲食" for group in plan["groups"])
    segment = next(seg for group in plan["groups"] for seg in group["segments"])
    assert segment["story_context"]["activity_source"] == "user_summary"
    script = project_detail(cfg, db, project_id)["script"]
    assert "## 使用者故事脈絡" in script
    assert "來源：user_summary" in script


def test_clip_contract_exposes_ai_user_and_effective_summaries_separately(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)
    _frame(db, tmp_path, video_id, 0, "AI 第一個畫面")
    _frame(db, tmp_path, video_id, 1, "AI 第二個畫面")
    project_id = create_project(db, "A", [video_id])
    cfg = _cfg(tmp_path)
    assert update_project_media_summary(db, project_id, video_id, "人工故事脈絡")

    clip = project_detail(cfg, db, project_id)["clips"][0]
    assert clip["ai_visual_summary"] == "AI 第一個畫面 / AI 第二個畫面"
    assert clip["visual_summary"] == clip["ai_visual_summary"]
    assert clip["user_summary"] == "人工故事脈絡"
    assert clip["effective_summary"] == "人工故事脈絡"
    assert clip["effective_summary_source"] == "user"


def test_legacy_unowned_summary_is_stored_on_video_not_frames(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id, _ = _video(db, tmp_path)

    assert update_video_summary(db, video_id, "尚未加入專案的使用者備註")

    assert frames(db, video_id) == []
    row = dict(videos(db)[0])
    assert row["user_summary"] == "尚未加入專案的使用者備註"
    project_id = create_project(db, "A", [video_id])
    assert project_videos(db, project_id)[0]["user_summary"] == "尚未加入專案的使用者備註"


def test_legacy_summary_migration_is_conservative(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    clear_id, _ = _video(db, tmp_path, "clear.mp4")
    ambiguous_id, _ = _video(db, tmp_path, "ambiguous.mp4")
    _frame(db, tmp_path, clear_id, 0, "AI 原始觀察")
    _frame(db, tmp_path, ambiguous_id, 0, "完全相同的舊描述")
    _frame(db, tmp_path, ambiguous_id, 1, "完全相同的舊描述")
    clear_project = create_project(db, "clear", [clear_id])
    ambiguous_project = create_project(db, "ambiguous", [ambiguous_id])
    with connect(db) as con:
        con.execute(
            """update project_videos
            set summary_override='人工修改內容', user_summary='', summary_migration_state='none'
            where project_id=? and video_id=?""",
            (clear_project, clear_id),
        )
        con.execute(
            """update project_videos
            set summary_override='完全相同的舊描述', user_summary='', summary_migration_state='none'
            where project_id=? and video_id=?""",
            (ambiguous_project, ambiguous_id),
        )

    init_db(db)

    clear = dict(project_videos(db, clear_project)[0])
    ambiguous = dict(project_videos(db, ambiguous_project)[0])
    assert clear["user_summary"] == "人工修改內容"
    assert clear["user_summary_migration_state"] == "migrated"
    assert ambiguous["user_summary"] == ""
    assert ambiguous["user_summary_migration_state"] == "review"
