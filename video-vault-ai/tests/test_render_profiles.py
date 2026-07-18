import pytest

from video_vault.render_profiles import get_render_profile, list_render_profiles, resolve_encoder, serialize_render_profile, validate_render_profile


def test_fixed_profiles_are_deterministic():
    assert list_render_profiles() == (
        "preview_1080p30", "preview_1080p60", "final_1080p30", "final_1080p60", "final_2160p30", "final_2160p60"
    )
    profile = get_render_profile("final_2160p60")
    assert (profile.width, profile.height, profile.fps_num, profile.fps_den) == (3840, 2160, 60, 1)
    assert profile.audio_sample_rate == 48000
    assert profile.audio_channels == 2
    assert resolve_encoder(profile) == ("h264_nvenc", "libx264")


def test_profile_validation_and_serialization():
    with pytest.raises(ValueError, match="Unknown render profile"):
        validate_render_profile("4k_magic")
    data = serialize_render_profile("preview_1080p30")
    assert data["name"] == "preview_1080p30"
    assert data["video_encoder"] == "h264_nvenc"

