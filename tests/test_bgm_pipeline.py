from pathlib import Path

import pytest

from video_vault.bgm_pipeline import BgmPipelineError, bgm_fingerprint, build_bgm_filter, build_bgm_mix_command, validate_bgm_track


def _track(path: Path, loop=True):
    return {"track_id": 2, "source_path": str(path), "gain_db": -12, "loop": loop, "fade_in_seconds": 2, "fade_out_seconds": 4}


def test_bgm_filter_clamps_fades_and_apads_non_loop():
    result = build_bgm_filter(_track(Path("music.mp3"), loop=False), 3)
    assert "volume=0.25118864" in result
    assert "afade=t=in:st=0:d=2.000000" in result
    assert "apad" in result
    assert "afade=t=out:st=0.000000:d=3.000000" in result
    assert "atrim=duration=3.000000" in result


def test_bgm_mix_command_loop_and_no_shell_path():
    track = _track(Path("C:/Music Files/song;[one].mp3"), loop=True)
    command = build_bgm_mix_command("ffmpeg", Path("timeline.ffconcat"), Path("out.partial.mp4"), track, 5, {"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2})
    assert "-stream_loop" in command and "-1" in command
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in " ".join(command)
    assert "-c:v copy" in " ".join(command)
    assert "out.partial.mp4" in command[-1]


def test_bgm_validation_rejects_missing_source(tmp_path: Path):
    with pytest.raises(BgmPipelineError, match="does not exist"):
        validate_bgm_track(_track(tmp_path / "missing.mp3"))


def test_bgm_fingerprint_changes_when_source_changes(tmp_path: Path):
    source = tmp_path / "music.mp3"
    source.write_bytes(b"a")
    first = bgm_fingerprint(_track(source))
    source.write_bytes(b"b")
    second = bgm_fingerprint(_track(source))
    assert first["source_sha256"] != second["source_sha256"]
