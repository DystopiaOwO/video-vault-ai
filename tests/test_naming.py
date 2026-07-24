from pathlib import Path

from video_vault.database import add_frame, frames, init_db, upsert_video, videos
from video_vault.naming import detect_category, rename_after_perception


def test_detect_category_from_frame_tags(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "unknown"})
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"x")
    add_frame(db, video_id, frame, 0)
    with __import__("sqlite3").connect(db) as con:
        con.execute("update frames set tags='coffee,hands' where video_id=?", (video_id,))
    assert detect_category(db, video_id) == "coffee"


def test_rename_after_perception_updates_db(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "unknown"})
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"x")
    add_frame(db, video_id, frame, 0)
    with __import__("sqlite3").connect(db) as con:
        con.execute("update frames set tags='coffee' where video_id=?", (video_id,))
    monkeypatch.setattr("video_vault.naming.recording_time", lambda path, cfg: __import__("datetime").datetime(2026, 6, 20, 9, 8, 7))
    out = rename_after_perception({"ffprobe_path": "ffprobe"}, db, dict(videos(db)[0]))
    assert Path(out["current_path"]).name == "20260620_090807_coffee_clip.mp4"
    assert videos(db)[0]["filename"] == "20260620_090807_coffee_clip.mp4"
