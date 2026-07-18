from video_vault.database import init_db, upsert_video, videos, write_json_index


def test_index_sqlite_and_json(tmp_path):
    db = tmp_path / "index.sqlite3"
    init_db(db)
    upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "unknown"})
    assert videos(db)[0]["filename"] == "a.mp4"
    out = write_json_index(db, tmp_path / "index.json")
    assert "a.mp4" in out.read_text()
