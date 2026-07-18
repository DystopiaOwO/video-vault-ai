from video_vault.color import color_decision, color_filter


def test_dji_lut_filter_uses_configured_cube(tmp_path):
    lut = tmp_path / "dji.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")

    vf = color_filter("dji_lut", {"color": {"dji_lut_path": str(lut)}})

    assert "lut3d=file=" in vf
    assert "dji.cube" in vf


def test_color_decision_dark_clip_gets_lift(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr("video_vault.color._brightness_stats", lambda source, cfg: {"average": 60, "highlight_ratio": 0, "sampled_frames": 3})

    decision = color_decision(source, {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"})

    assert decision["brightness"] > 0
    assert decision["gamma"] > 1
