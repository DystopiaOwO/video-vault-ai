import json
from pathlib import Path

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import build_project_plan, can_project_render, create_project, project_dir, set_review_status
from video_vault.render_settings import default_render_settings, load_render_settings, save_render_settings


def _approved_project(tmp_path: Path) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "20260718_081500_coffee.mp4"
    source.write_bytes(b"video")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 1, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "settings", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    return cfg, db, project_id


def test_default_settings_and_save_invalidate_approval(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    settings = load_render_settings(cfg, project_id)
    assert settings == default_render_settings()
    assert can_project_render(cfg, db, project_id)[0] is True

    path = save_render_settings(cfg, db, project_id, {"profile_id": "accurate_preview_1080p", "audio": {"bgm_gain_db": -12}})
    assert path.name == "render_settings.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["profile_id"] == "accurate_preview_1080p"
    assert saved["audio"]["bgm_gain_db"] == -12.0
    review = json.loads(project_dir(cfg, project_id).joinpath("review_status.json").read_text(encoding="utf-8"))
    assert review["approved_by_user"] is False
    assert "approved_manifest_hash" not in review
    assert can_project_render(cfg, db, project_id)[0] is False


def test_approval_captures_manifest_snapshot(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    review_path = project_dir(cfg, project_id) / "review_status.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    manifest = json.loads((project_dir(cfg, project_id) / "render_manifest.json").read_text(encoding="utf-8"))
    assert review["approved_by_user"] is True
    assert review["approved_manifest_hash"] == manifest["manifest_hash"]
    assert review["approved_plan_id"] == manifest["plan_id"]
    assert review["approved_at"]
