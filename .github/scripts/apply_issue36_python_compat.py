from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        if new in text:
            return
        raise SystemExit(f"missing patch target: {label}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


media = Path("tests/test_media_ownership.py")
replace_once(
    media,
    '''    cfg = {"library_root": str(tmp_path)}
    assert project_detail(cfg, db, project_a)["clips"][0]["visual_summary"] == "A-only summary"
    assert project_detail(cfg, db, project_b)["clips"][0]["visual_summary"] == "global summary"

    assert update_video_summary(db, video_id, "A second edit", project_id=project_a) is True
    assert dict(project_videos(db, project_a)[0])["project_summary"] == "A second edit"
    assert dict(project_videos(db, project_b)[0])["project_summary"] == "global summary"
    assert project_detail(cfg, db, project_a)["clips"][0]["visual_summary"] == "A second edit"
    assert project_detail(cfg, db, project_b)["clips"][0]["visual_summary"] == "global summary"
''',
    '''    cfg = {"library_root": str(tmp_path)}
    clip_a = project_detail(cfg, db, project_a)["clips"][0]
    clip_b = project_detail(cfg, db, project_b)["clips"][0]
    assert clip_a["visual_summary"] == "global summary"
    assert clip_b["visual_summary"] == "global summary"
    assert clip_a["user_summary"] == "A-only summary"
    assert clip_b["user_summary"] == ""
    assert clip_a["effective_summary"] == "A-only summary"
    assert clip_b["effective_summary"] == "global summary"

    assert update_video_summary(db, video_id, "A second edit", project_id=project_a) is True
    assert dict(project_videos(db, project_a)[0])["project_summary"] == "A second edit"
    assert dict(project_videos(db, project_b)[0])["project_summary"] == "global summary"
    clip_a = project_detail(cfg, db, project_a)["clips"][0]
    clip_b = project_detail(cfg, db, project_b)["clips"][0]
    assert clip_a["visual_summary"] == "global summary"
    assert clip_b["visual_summary"] == "global summary"
    assert clip_a["user_summary"] == "A second edit"
    assert clip_b["user_summary"] == ""
    assert clip_a["effective_summary"] == "A second edit"
    assert clip_b["effective_summary"] == "global summary"
''',
    "project-local summary assertions",
)

project = Path("tests/test_project.py")
replace_once(
    project,
    '''def test_manual_clip_summary_updates_project_detail(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee"})
    frame1 = tmp_path / "frame.jpg"
    frame2 = tmp_path / "frame2.jpg"
    add_frame(db, video_id, frame1, 0)
    add_frame(db, video_id, frame2, 5)
    update_frame_analysis(db, int(db_frames(db, video_id)[0]["id"]), {"summary": "舊描述 A", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    update_frame_analysis(db, int(db_frames(db, video_id)[1]["id"]), {"summary": "舊描述 B", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    project_id = create_project(db, "p", [video_id])

    assert update_video_summary(db, video_id, "手動改過的描述")

    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)
    assert detail["clips"][0]["visual_summary"] == "手動改過的描述"
''',
    '''def test_manual_clip_summary_updates_user_context_without_mutating_ai_detail(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee"})
    frame1 = tmp_path / "frame.jpg"
    frame2 = tmp_path / "frame2.jpg"
    add_frame(db, video_id, frame1, 0)
    add_frame(db, video_id, frame2, 5)
    update_frame_analysis(db, int(db_frames(db, video_id)[0]["id"]), {"summary": "舊描述 A", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    update_frame_analysis(db, int(db_frames(db, video_id)[1]["id"]), {"summary": "舊描述 B", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    project_id = create_project(db, "p", [video_id])

    assert update_video_summary(db, video_id, "手動改過的描述")

    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)
    clip = detail["clips"][0]
    assert clip["visual_summary"] == "舊描述 A / 舊描述 B"
    assert clip["ai_visual_summary"] == "舊描述 A / 舊描述 B"
    assert clip["user_summary"] == "手動改過的描述"
    assert clip["effective_summary"] == "手動改過的描述"
    assert clip["effective_summary_source"] == "user"
    assert [row["vision_summary"] for row in db_frames(db, video_id)] == ["舊描述 A", "舊描述 B"]
''',
    "manual summary semantics",
)
