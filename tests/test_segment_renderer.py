from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.media_probe import MediaProbe
from video_vault.segment_renderer import build_segment_ffmpeg_command, map_encoder
from video_vault.render_errors import is_encoder_fallback_error


def _probe(has_audio=True):
    return MediaProbe(Path("source.mp4"), 5, True, has_audio, 1280, 720, 24, 24, 1, "yuv420p", "h264", "aac" if has_audio else "", 48000 if has_audio else 0, 2 if has_audio else 0)


def _manifest(settings=None):
    return {"profile": {"profile_id": "accurate_preview_1080p"}, "settings": settings or {"encoder": "cpu", "color": {"mode": "none"}, "audio": {}}}


def test_command_uses_filter_trim_normalization_and_audio():
    command = build_segment_ffmpeg_command(
        {"ffmpeg_path": "ffmpeg"},
        _manifest(),
        {"source_file": "source.mp4", "source_in_seconds": 1.25, "source_out_seconds": 3.875, "speed": 1, "audio_role": "lower_original"},
        _probe(),
        output="out.partial.mp4",
        encoder="libx264",
    )
    text = " ".join(command)
    assert "trim=start=1.250000:end=3.875000" in text
    assert "atrim=start=1.250000:end=3.875000" in text
    assert "setpts=PTS-STARTPTS" in text
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in text
    assert "pad=1920:1080" in text
    assert "fps=30" in text and "format=yuv420p" in text
    assert "-c copy" not in text
    assert "-map [vout] -map [aout]" in text


def test_no_audio_source_gets_silence_input():
    command = build_segment_ffmpeg_command(
        {"ffmpeg_path": "ffmpeg"},
        _manifest(),
        {"source_file": "silent.mp4", "source_in_seconds": 0, "source_out_seconds": 2, "speed": 1, "audio_role": "mute"},
        _probe(False),
        output="out.partial.mp4",
        encoder="libx264",
    )
    text = " ".join(command)
    assert "anullsrc=r=48000:cl=stereo" in text
    assert "-map [aout]" in text


@pytest.mark.parametrize(("requested", "expected"), [("auto", "h264_nvenc"), ("cpu", "libx264"), ("libx264", "libx264"), ("h264_nvenc", "h264_nvenc")])
def test_encoder_mapping(requested, expected):
    assert map_encoder(requested) == expected


def test_fallback_only_matches_encoder_failures():
    assert is_encoder_fallback_error("Cannot load NVENC")
    assert is_encoder_fallback_error("No capable devices found")
    assert not is_encoder_fallback_error("Invalid argument in filter graph")
    assert not is_encoder_fallback_error("No such file or directory: source.mp4")
