from video_vault.analyzer import MockProvider
from video_vault.analyzer.vision_pipeline import analyze_video_frames
from video_vault.database import add_frame, frames, init_db, segments, upsert_video
import pytest


def test_mock_analyze_returns_segments():
    result = MockProvider().analyze({"filename": "coffee.mp4", "category": "coffee", "duration_seconds": 90})
    assert result["segments"]
    assert result["segments"][0]["segment_type"] == "shorts"
    assert result["segments"][0]["suggested_use"] == "短影音"
    assert "初步內容感知" in result["summary"]


def test_mock_frame_pipeline_writes_cache_and_segments(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    frame = tmp_path / "frame_00000.jpg"
    frame.write_bytes(b"fake image")
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee"})
    add_frame(db, video_id, frame, 0)
    cfg = {"library_root": str(tmp_path), "frame_interval_seconds": 5, "ai": {"provider": "mock"}}
    result = analyze_video_frames(db, {"id": video_id, "filename": "a.mp4", "category": "coffee"}, cfg)
    assert result["frames"][0]["tags"]
    assert "咖啡" in result["frames"][0]["summary"]
    assert result["segments"][0]["suggested_use"] == "短影音"
    assert frames(db, video_id)[0]["vision_summary"]
    assert segments(db, video_id)
    assert list((tmp_path / "05_index" / "raw_ai_outputs").glob("*.json"))


def test_unsupported_provider_fails_closed(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    frame = tmp_path / "frame_00000.jpg"
    frame.write_bytes(b"fake image")
    video_id = upsert_video(
        db,
        {"original_path": "unsupported.mp4", "current_path": "unsupported.mp4", "filename": "unsupported.mp4"},
    )
    add_frame(db, video_id, frame, 0)
    cfg = {"library_root": str(tmp_path), "frame_interval_seconds": 5, "ai": {"provider": "unsupported"}}

    with pytest.raises(ValueError, match="unsupported AI provider"):
        analyze_video_frames(db, {"id": video_id, "filename": "unsupported.mp4"}, cfg)
