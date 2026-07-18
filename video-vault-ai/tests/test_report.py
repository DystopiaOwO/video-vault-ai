from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.report import write_report


def test_report_contains_markdown(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee"})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 1, "end_seconds": 2, "segment_type": "shorts", "title": "x", "reason": "y", "tags": ["coffee"], "score": 1, "suggested_use": "Shorts"}]}, tmp_path / "raw.json")
    out = write_report({"id": video_id, "filename": "a.mp4", "category": "coffee", "duration_seconds": 2, "width": 0, "height": 0, "fps": 0}, db, tmp_path)
    assert "# 影片內容分析報告" in out.read_text(encoding="utf-8")
