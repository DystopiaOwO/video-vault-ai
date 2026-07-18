import json

from video_vault.media_probe import probe_media


def test_probe_normalizes_audio_video_and_invalidates_cache(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    calls = []

    payload = {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "duration": "2.5", "avg_frame_rate": "30/1", "r_frame_rate": "60/1", "time_base": "1/90000", "pix_fmt": "yuv420p", "color_space": "bt709", "tags": {"rotate": "90"}},
            {"codec_type": "audio", "sample_rate": "44100", "channels": 2},
        ],
        "format": {"duration": "2.5"},
    }

    def fake_run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()

    monkeypatch.setattr("video_vault.media_probe.subprocess.run", fake_run)
    cache = tmp_path / "probe-cache"
    first = probe_media(source, {"ffprobe_path": "ffprobe"}, cache_dir=cache)
    second = probe_media(source, {"ffprobe_path": "ffprobe"}, cache_dir=cache)
    assert first.duration_ms == 2500
    assert (first.width, first.height, first.rotation) == (1920, 1080, 90)
    assert first.is_vfr is True
    assert (first.audio_sample_rate, first.audio_channels) == (44100, 2)
    assert second == first
    assert len(calls) == 1
    source.write_bytes(b"changed")
    probe_media(source, {"ffprobe_path": "ffprobe"}, cache_dir=cache)
    assert len(calls) == 2
