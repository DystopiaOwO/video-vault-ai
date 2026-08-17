import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.media_probe import SourceProbeRegistry, probe_media, probe_media_metadata
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


def test_fast_metadata_probe_omits_counting_flags(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1", "pix_fmt": "yuv420p", "codec_name": "h264"}],
        "format": {"duration": "2.5"},
    }
    commands = []
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda command, **kwargs: (commands.append(command) or _result(payload)))
    probe_media_metadata("ffprobe", source)
    assert "-count_frames" not in commands[0]
    assert "-count_packets" not in commands[0]


def test_probe_preserves_display_matrix_and_normalizes_display_geometry(monkeypatch, tmp_path: Path):
    source = tmp_path / "rotated.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "coded_width": 3840,
                "coded_height": 2176,
                "sample_aspect_ratio": "1:1",
                "avg_frame_rate": "60/1",
                "pix_fmt": "yuv420p10le",
                "codec_name": "hevc",
                "side_data_list": [
                    {
                        "side_data_type": "Display Matrix",
                        "displaymatrix": "matrix -90",
                        "rotation": -90,
                    }
                ],
            }
        ],
        "format": {"duration": "2.5"},
    }
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    result = probe_media_metadata("ffprobe", source)
    assert result.coded_width == 3840
    assert result.coded_height == 2176
    assert result.sample_aspect_ratio == "1:1"
    assert result.display_aspect_ratio == "9:16"
    assert result.display_ratio == pytest.approx(9 / 16)
    assert (result.display_width, result.display_height) == (2160, 3840)
    assert result.rotation_degrees == -90
    assert result.display_matrix == "matrix -90"
    assert result.display_geometry_source == "display_matrix"


def test_probe_uses_rotate_tag_when_display_matrix_is_missing(monkeypatch, tmp_path: Path):
    source = tmp_path / "tag-rotated.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "4:3",
                "avg_frame_rate": "30/1",
                "pix_fmt": "yuv420p",
                "codec_name": "h264",
                "tags": {"rotate": "90"},
            }
        ],
        "format": {"duration": "1"},
    }
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    result = probe_media_metadata("ffprobe", source)
    assert result.rotation_degrees == 90
    assert result.display_aspect_ratio == "27:64"
    assert result.display_ratio == pytest.approx(27 / 64)


def test_deep_probe_retains_frame_and_packet_counting(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {
        "streams": [{"codec_type": "video", "width": 1, "height": 1, "avg_frame_rate": "1/1", "codec_name": "h264"}],
        "format": {"duration": "1"},
    }
    commands = []
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda command, **kwargs: (commands.append(command) or _result(payload)))
    probe_media("ffprobe", source)
    assert "-count_frames" in commands[0]
    assert "-count_packets" in commands[0]


def test_source_probe_registry_deduplicates_unique_sources(monkeypatch, tmp_path: Path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    payload = {"streams": [{"codec_type": "video", "avg_frame_rate": "30/1"}], "format": {"duration": "1"}}
    calls = []
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda command, **kwargs: (calls.append(command) or _result(payload)))
    registry = SourceProbeRegistry("ffprobe")
    registry.probe(first)
    registry.probe(first)
    registry.probe(first)
    registry.probe(second)
    audit = registry.audit()
    assert len(calls) == 2
    assert audit["unique_source_count"] == 2
    assert audit["source_probe_calls"] == 2
    assert audit["source_probe_cache_hits"] == 2
    assert audit["source_probe_mode"] == "fast_metadata"


def test_source_probe_registry_blocks_same_path_identity_change(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    payload = {"streams": [{"codec_type": "video", "avg_frame_rate": "30/1"}], "format": {"duration": "1"}}
    monkeypatch.setattr("video_vault.media_probe.subprocess.run", lambda *args, **kwargs: _result(payload))
    original = {"size": 6, "mtime_ns": 10, "source_identity": {"contract": "test", "device": 1, "inode": 1}}
    replacement = {"size": 6, "mtime_ns": 10, "source_identity": {"contract": "test", "device": 1, "inode": 2}}
    states = iter([original, original, replacement])
    monkeypatch.setattr("video_vault.media_probe.source_stat", lambda path: next(states))
    registry = SourceProbeRegistry("ffprobe")
    registry.probe(source)
    with pytest.raises(MediaProbeError, match="identity changed"):
        registry.probe(source)
