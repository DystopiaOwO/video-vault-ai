from pathlib import Path

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.opencut import export_opencut_handoff
from video_vault.project import create_project


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
