import pytest

from video_vault.render_profiles import get_render_profile, list_render_profiles


def test_render_profiles_are_deterministic():
    assert list_render_profiles() == ("accurate_preview_1080p", "final_1080p", "final_1080p_portrait")
    assert get_render_profile("final_1080p") == {
        "profile_id": "final_1080p",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "video_codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_matrix": "bt709",
        "color_range": "tv",
        "hdr_intent": "sdr",
    }
    portrait = get_render_profile("final_1080p_portrait")
    assert (portrait["width"], portrait["height"]) == (1080, 1920)


def test_unknown_profile_fails_clearly():
    with pytest.raises(ValueError, match="Unknown render profile"):
        get_render_profile("missing")
