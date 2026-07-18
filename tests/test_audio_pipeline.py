import pytest

from video_vault.audio_pipeline import audio_gain_db, build_audio_filter, build_atempo_chain, build_silence_filter


@pytest.mark.parametrize(
    ("speed", "expected"),
    [(0.25, [0.5, 0.5]), (0.5, [0.5]), (1.0, []), (2.0, [2.0]), (4.0, [2.0, 2.0])],
)
def test_atempo_chain_supports_boundary_speeds(speed, expected):
    assert build_atempo_chain(speed) == expected


def test_atempo_rejects_speed_outside_supported_range():
    with pytest.raises(ValueError, match="between 0.25 and 4.0"):
        build_atempo_chain(4.01)


def test_audio_roles_use_manifest_gains_and_keep_audio_stream():
    settings = {"audio": {"original_gain_db": -3, "lower_original_gain_db": -12}}
    assert audio_gain_db("keep_original", settings) == -3
    assert audio_gain_db("lower_original", settings) == -12
    assert audio_gain_db("mute", settings) == 0
    assert "volume=0" in build_audio_filter("mute", 1, settings, start=0, end=2)
    assert build_silence_filter(2).startswith("anullsrc=r=48000:cl=stereo")
