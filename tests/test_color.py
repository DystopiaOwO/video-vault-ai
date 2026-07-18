from video_vault.color import color_decision, color_filter, video_decode_args, video_encode_args
from video_vault.database import add_frame, add_analysis, init_db, update_frame_analysis, upsert_video


def test_dji_lut_filter_uses_configured_cube(tmp_path):
    lut = tmp_path / "dji.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")

    vf = color_filter("dji_lut", {"color": {"dji_lut_path": str(lut)}})

    assert "lut3d=file=" in vf
    assert "dji.cube" in vf


def test_nvenc_uses_cuda_decode_args():
    assert video_decode_args({"color": {"video_encoder": "h264_nvenc"}}) == ["-hwaccel", "cuda"]


def test_nvenc_uses_fast_preset():
    args = video_encode_args({"color": {"video_encoder": "h264_nvenc"}})
    assert "-preset" in args
    assert "p1" in args


def test_color_decision_dark_clip_gets_lift(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr("video_vault.color._brightness_stats", lambda source, cfg: {"average": 60, "highlight_ratio": 0, "sampled_frames": 3})

    decision = color_decision(source, {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"})

    assert decision["brightness"] > 0
    assert decision["gamma"] > 1


def test_color_decision_uses_perception_text(tmp_path, monkeypatch):
    source = tmp_path / "coffee.mp4"
    source.write_bytes(b"x")
    db = tmp_path / "05_index" / "video_vault.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "coffee"})
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"x")
    add_frame(db, video_id, frame, 0)
    update_frame_analysis(db, 1, {"summary": "咖啡餐點畫面有點過曝", "tags": ["coffee", "food"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    monkeypatch.setattr("video_vault.color._brightness_stats_at", lambda source, cfg, timestamp: {"average": 128, "highlight_ratio": 0, "sampled_frames": 1})

    decision = color_decision(source, {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"})

    assert decision["saturation"] > 0.98
    assert decision["brightness"] < -0.015
    assert "咖啡餐點" in decision["perception_basis"]


def test_color_decision_uses_best_segment_as_reference(tmp_path, monkeypatch):
    source = tmp_path / "travel.mp4"
    source.write_bytes(b"x")
    db = tmp_path / "05_index" / "video_vault.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel"})
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {
            "segments": [
                {"start_seconds": 0, "end_seconds": 5, "segment_type": "b_roll", "title": "路上", "reason": "", "tags": ["travel"], "score": 0.4, "suggested_use": "補畫面"},
                {"start_seconds": 20, "end_seconds": 30, "segment_type": "shorts", "title": "最精彩畫面", "reason": "", "tags": ["travel"], "score": 0.95, "suggested_use": "短影音"},
            ]
        },
        tmp_path / "raw.json",
    )
    seen = {}

    def fake_stats(source, cfg, timestamp):
        seen["timestamp"] = timestamp
        return {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1}

    monkeypatch.setattr("video_vault.color._brightness_stats_at", fake_stats)

    decision = color_decision(source, {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"})

    assert seen["timestamp"] == 25
    assert decision["brightness"] < -0.05
    assert decision["color_reference"]["label"] == "最精彩畫面"
