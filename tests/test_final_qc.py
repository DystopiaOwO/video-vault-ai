from pathlib import Path
import json
from types import SimpleNamespace

from video_vault.final_qc import validate_final_output


def test_final_qc_rejects_missing_file(tmp_path: Path):
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}
    result = validate_final_output(tmp_path / "missing.mp4", manifest)
    assert not result.passed
    assert "missing" in result.errors[0]


def test_final_qc_checks_all_profile_fields(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: SimpleNamespace(has_video=True, has_audio=True, width=1920, height=1080, fps=30, pixel_format="yuv420p", video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2, duration_seconds=1.01, frame_count=30))
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}
    result = validate_final_output(output, manifest)
    assert result.passed
    assert result.output_sha256


def test_final_qc_fails_closed_when_frame_count_is_missing(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: SimpleNamespace(has_video=True, has_audio=True, width=1920, height=1080, fps=30, pixel_format="yuv420p", video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2, duration_seconds=1.0, frame_count=0))
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}

    result = qc_module.validate_final_output(output, manifest)

    assert not result.passed
    assert any("frame count unavailable" in error for error in result.errors)


def test_final_qc_uses_packet_tails_for_av_drift(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    probe = SimpleNamespace(
        source_file=tmp_path / "source.mp4", has_video=True, has_audio=True,
        width=1920, height=1080, fps=30, fps_num=30, fps_den=1,
        pixel_format="yuv420p", video_codec="h264", audio_codec="aac",
        sample_rate=48000, channels=2, duration_seconds=1.0, frame_count=30,
        video_stream_index=0, audio_stream_index=1,
    )
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: probe)
    monkeypatch.setattr(qc_module, "_decode_report", lambda *args: {"ok": True, "returncode": 0, "stderr": ""})
    monkeypatch.setattr(qc_module, "_packet_diagnostics", lambda *args: {"stream_ends": {0: 1.0, 1: 1.3}, "errors": [], "warnings": [], "timestamp_monotonic": True})
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}

    result = qc_module.validate_final_output(output, manifest)

    assert not result.passed
    assert result.measurements["video"]["tail_source"] == "packet"
    assert any("A/V tail drift" in error for error in result.errors)


def test_final_qc_fails_closed_when_packet_tails_are_missing(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    probe = SimpleNamespace(
        source_file=tmp_path / "source.mp4", has_video=True, has_audio=True,
        width=1920, height=1080, fps=30, fps_num=30, fps_den=1,
        pixel_format="yuv420p", video_codec="h264", audio_codec="aac",
        sample_rate=48000, channels=2, duration_seconds=1.0, frame_count=30,
        video_stream_index=0, audio_stream_index=1,
    )
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: probe)
    monkeypatch.setattr(qc_module, "_decode_report", lambda *args: {"ok": True, "returncode": 0, "stderr": ""})
    monkeypatch.setattr(qc_module, "_packet_diagnostics", lambda *args: {"stream_ends": {}, "errors": [], "warnings": [], "timestamp_monotonic": True})
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}

    result = qc_module.validate_final_output(output, manifest)

    assert not result.passed
    assert "video packet tail unavailable" in result.errors
    assert "audio packet tail unavailable" in result.errors


def test_packet_diagnostics_rejects_duplicate_and_non_monotonic_timestamps(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    cases = [
        ([{"stream_index": 0, "dts_time": "0", "duration_time": "0.1"}, {"stream_index": 0, "dts_time": "0", "duration_time": "0.1"}], "duplicate timestamps"),
        ([{"stream_index": 0, "dts_time": "0.1", "duration_time": "0.1"}, {"stream_index": 0, "dts_time": "0.0", "duration_time": "0.1"}], "non-monotonic timestamps"),
    ]
    for packets, expected in cases:
        monkeypatch.setattr(qc_module.subprocess, "run", lambda *args, packets=packets, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({"packets": packets}), stderr=""))
        result = qc_module._packet_diagnostics(tmp_path / "out.mp4", "ffprobe")
        assert not result["timestamp_monotonic"]
        assert any(expected in error for error in result["errors"])


def test_final_qc_enforces_final_loudness_tolerance(monkeypatch, tmp_path: Path):
    import video_vault.final_qc as qc_module

    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: SimpleNamespace(has_video=True, has_audio=True, width=1920, height=1080, fps=30, pixel_format="yuv420p", video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2, duration_seconds=1.0))
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}
    loudness = SimpleNamespace(measured_I=-11.5, measured_TP=-0.7, target_lufs=-14.0, true_peak_db=-1.0)

    result = validate_final_output(output, manifest, loudness=loudness)

    assert not result.passed
    assert any("loudness mismatch" in error for error in result.errors)
    assert any("true peak exceeds" in error for error in result.errors)
