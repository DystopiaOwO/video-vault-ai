from pathlib import Path

import json
import pytest

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.opencut import export_opencut_handoff
from video_vault.project import build_project_plan, can_project_render, create_project, project_detail, save_segment_review, set_review_status
from video_vault.storyboard import generate_storyboard


def test_opencut_handoff_writes_manifest(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "coffee", [video_id], category="coffee")

    out = export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id)

    assert Path(out, "README.md").exists()
    assert Path(out, "recommended_segments.csv").exists()
    assert Path(out, "opencut_handoff.json").exists()


def test_needs_review_project_cannot_render_opencut_graded_clips(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "coffee", [video_id], category="coffee")

    with pytest.raises(PermissionError):
        export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id, render_clips=True)

    out = export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id, render_clips=False)
    assert Path(out, "opencut_handoff.json").exists()
    assert not list(Path(out, "graded_clips").glob("*.mp4"))


def test_opencut_handoff_uses_segment_review(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {"segments": [
            {"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "first", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"},
            {"start_seconds": 6, "end_seconds": 8, "segment_type": "key_action", "title": "second", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"},
        ]},
        tmp_path / "raw.json",
    )
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "coffee", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    rows = project_detail(cfg, db, project_id)["segments"]
    rows[1]["start_seconds"] = 6.5
    rows[1]["end_seconds"] = 7.5
    rows[0]["include"] = False
    save_segment_review(cfg, db, project_id, [rows[1], rows[0]])

    out = export_opencut_handoff(cfg, db, project_id)
    data = json.loads(Path(out, "opencut_handoff.json").read_text(encoding="utf-8"))

    assert [seg["title"] for seg in data["segments"]] == ["second"]
    assert data["segments"][0]["start_seconds"] == 6.5
    assert data["segments"][0]["end_seconds"] == 7.5


def test_opencut_preview_keeps_approved_project_approved(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "coffee", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")

    export_opencut_handoff(cfg, db, project_id, render_clips=False)

    assert can_project_render(cfg, db, project_id)[0] is True
