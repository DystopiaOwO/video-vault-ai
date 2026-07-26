"""Deterministic Phase 2 render profile registry."""

from __future__ import annotations

from copy import deepcopy


_PROFILES: dict[str, dict[str, object]] = {
    "accurate_preview_1080p": {
        "profile_id": "accurate_preview_1080p",
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
    },
    "final_1080p": {
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
    },
}


def list_render_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def get_render_profile(profile_id: str) -> dict[str, object]:
    try:
        return deepcopy(_PROFILES[profile_id])
    except KeyError as exc:
        allowed = ", ".join(_PROFILES)
        raise ValueError(f"Unknown render profile {profile_id!r}; expected one of: {allowed}") from exc


__all__ = ["get_render_profile", "list_render_profiles"]
