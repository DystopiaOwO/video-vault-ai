import os
from pathlib import Path

import pytest

from video_vault.timeline_assembler import TimelineAssemblyError, build_concat_file, build_timeline_command, escape_ffconcat_path


def test_concat_path_escapes_windows_and_special_characters(tmp_path: Path):
    source = tmp_path / "Project dir,semi;[test]'quote 中文" / "clip #1.mp4"
    source.parent.mkdir()
    source.write_bytes(b"clip")
    escaped = escape_ffconcat_path(source)
    if os.name == "nt":
        assert "C:/" in escaped
    else:
        assert escaped.startswith("/")
    assert "'\\''" in escaped
    assert "Project dir,semi;[test]" in escaped
    assert "quote 中文" in escaped
    assert "clip #1.mp4" in escaped
    concat = build_concat_file([source], tmp_path / "timeline.ffconcat")
    text = concat.read_text(encoding="utf-8")
    assert text.startswith("ffconcat version 1.0\nfile '")
    assert "clip #1.mp4" in text


def test_concat_rejects_missing_or_empty_inputs(tmp_path: Path):
    with pytest.raises(TimelineAssemblyError, match="empty segment"):
        build_concat_file([], tmp_path / "timeline.ffconcat")
    with pytest.raises(TimelineAssemblyError, match="does not exist"):
        build_concat_file([tmp_path / "missing.mp4"], tmp_path / "timeline.ffconcat")


def test_timeline_command_uses_concat_and_video_audio_copy():
    command = build_timeline_command("ffmpeg", Path("timeline.ffconcat"), Path("out.mp4"), duration_seconds=3)
    text = " ".join(command)
    assert "-f concat" in text
    assert "-safe 0" in text
    assert "-c:v copy" in text
    assert "-c:a copy" in text
    assert "-avoid_negative_ts make_zero" in text


def test_timeline_command_can_reencode_audio_to_the_approved_duration():
    command = build_timeline_command(
        "ffmpeg",
        Path("timeline.ffconcat"),
        Path("out.mp4"),
        duration_seconds=3,
        profile={"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        force_audio_filter=True,
    )
    text = " ".join(command)

    assert "-c:a copy" not in text
    assert "-map [aout]" in text
    assert "apad,atrim=duration=3.000000,asetpts=PTS-STARTPTS" in text
