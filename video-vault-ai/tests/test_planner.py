from video_vault.database import add_analysis, init_db, set_video_status, upsert_video
from video_vault.planner import draft_plan, load_plan, revise_plan, set_plan_status, write_plan_files
from video_vault.renderer import render_approved


def _video(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee", "duration_seconds": 10})
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {"segments": [{"start_seconds": 1, "end_seconds": 4, "segment_type": "shorts", "title": "x", "reason": "nice", "tags": ["coffee"], "score": 1, "suggested_use": "Shorts"}]},
        tmp_path / "raw.json",
    )
    return db, {"id": video_id, "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee", "duration_seconds": 10}


def test_draft_plan_needs_review(tmp_path):
    db, video = _video(tmp_path)
    cfg = {"library_root": str(tmp_path)}
    plan = draft_plan(cfg, db, video)
    plan_path, script_path, status_path = write_plan_files(cfg, plan)
    assert load_plan(cfg, video["id"])["status"] == "needs_review"
    assert "剪輯計畫審核稿" in script_path.read_text(encoding="utf-8")
    assert "needs_review" in status_path.read_text(encoding="utf-8")
    assert plan_path.exists()


def test_render_requires_approval(tmp_path):
    db, video = _video(tmp_path)
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"}
    write_plan_files(cfg, draft_plan(cfg, db, video))
    assert render_approved(cfg, video["id"], dry_run=True) is None
    set_plan_status(cfg, video["id"], "approved")
    assert render_approved(cfg, video["id"], dry_run=True).name == "video_1_approved_render.mp4"


def test_revise_keeps_plan_and_returns_to_review(tmp_path):
    db, video = _video(tmp_path)
    cfg = {"library_root": str(tmp_path)}
    write_plan_files(cfg, draft_plan(cfg, db, video))
    set_plan_status(cfg, video["id"], "approved")
    folder = tmp_path / "05_index" / "video_1"
    (folder / "revision_prompt.txt").write_text("慢一點", encoding="utf-8")
    revise_plan(cfg, video["id"])
    assert load_plan(cfg, video["id"])["status"] == "needs_review"
    assert (folder / "edit_plan.json").exists()
