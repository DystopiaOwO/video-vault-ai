from pathlib import Path

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.hyperframes import export_hyperframes_project
from video_vault.project import create_project


def test_hyperframes_export_writes_html_timeline(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_travel.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "scene", "title": "station", "reason": "ok", "tags": ["travel"], "score": 1, "suggested_use": "B-roll"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "trip", [video_id], category="travel", content_type="travel_diary")

    out = export_hyperframes_project({"library_root": str(tmp_path)}, db, project_id, render_clips=False)

    html = Path(out, "index.html").read_text(encoding="utf-8")
    assert 'data-composition-id="story"' in html
    assert "<video" in html
    assert Path(out, "timeline.json").exists()
