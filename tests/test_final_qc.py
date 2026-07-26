from pathlib import Path
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
    monkeypatch.setattr(qc_module, "probe_media", lambda *args: SimpleNamespace(has_video=True, has_audio=True, width=1920, height=1080, fps=30, pixel_format="yuv420p", video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2, duration_seconds=1.01))
    manifest = {"profile": {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}, "segments": [{"timeline_duration_seconds": 1}]}
    result = validate_final_output(output, manifest)
    assert result.passed
    assert result.output_sha256


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
