import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.media_probe import probe_media
from video_vault.render_errors import MediaProbeError


def _result(payload, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="probe error")


def test_probe_parses_fractional_frame_rate(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p", "codec_name": "h264"},
            {"codec_type": "audio", "sample_rate": "48000", "channels": 2, "codec_name": "aac"},
        ],
        "format": {"duration": "2.5"},
    }
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    result = probe_media("ffprobe", source)
    assert result.fps_num == 30000
    assert result.fps_den == 1001
    assert result.fps == pytest.approx(30000 / 1001)
    assert result.has_audio and result.sample_rate == 48000


def test_probe_falls_back_from_zero_avg_frame_rate(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [{"codec_type": "video", "avg_frame_rate": "0/0", "r_frame_rate": "60000/1001", "width": 1, "height": 1}],
        "format": {"duration": "1"},
    }
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    result = probe_media("ffprobe", source)
    assert result.fps_num == 60000
    assert result.fps_den == 1001
    assert result.fps == pytest.approx(60000 / 1001)


def test_probe_rejects_invalid_frame_rate_fallback(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {"streams": [{"codec_type": "video", "avg_frame_rate": "N/A", "r_frame_rate": "0/0"}]}
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    with pytest.raises(MediaProbeError, match="no valid frame rate"):
        probe_media("ffprobe", source)


@pytest.mark.parametrize("payload", [{"streams": []}, {"streams": [{"codec_type": "audio"}]}])
def test_probe_requires_video_stream(monkeypatch, tmp_path: Path, payload):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    with pytest.raises(MediaProbeError, match="no video stream"):
        probe_media("ffprobe", source)


def test_probe_reports_ffprobe_failure(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result({}, returncode=1))
    with pytest.raises(MediaProbeError, match="ffprobe failed"):
        probe_media("ffprobe", source)
