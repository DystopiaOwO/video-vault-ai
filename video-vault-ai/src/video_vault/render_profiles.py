"""固定的 Render Pipeline v2 輸出 Profile registry。"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .render_types import RenderProfile


_PROFILES: dict[str, RenderProfile] = {
    "preview_1080p30": RenderProfile(name="preview_1080p30", width=1920, height=1080, fps_num=30),
    "preview_1080p60": RenderProfile(name="preview_1080p60", width=1920, height=1080, fps_num=60),
    "final_1080p30": RenderProfile(name="final_1080p30", width=1920, height=1080, fps_num=30),
    "final_1080p60": RenderProfile(name="final_1080p60", width=1920, height=1080, fps_num=60),
    "final_2160p30": RenderProfile(name="final_2160p30", width=3840, height=2160, fps_num=30),
    "final_2160p60": RenderProfile(name="final_2160p60", width=3840, height=2160, fps_num=60),
}


def list_render_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def validate_render_profile(name: str) -> str:
    if name not in _PROFILES:
        allowed = ", ".join(_PROFILES)
        raise ValueError(f"Unknown render profile {name!r}; expected one of: {allowed}")
    return name


def get_render_profile(name: str, *, encoder: str | None = None) -> RenderProfile:
    validate_render_profile(name)
    profile = _PROFILES[name]
    return replace(profile, video_encoder=encoder or profile.video_encoder)


def resolve_encoder(profile: RenderProfile | str, encoder: str | None = None) -> tuple[str, str]:
    """Return the requested encoder and its deterministic CPU fallback."""

    resolved = get_render_profile(profile) if isinstance(profile, str) else profile
    return encoder or resolved.video_encoder, resolved.fallback_video_encoder


def serialize_render_profile(profile: RenderProfile | str) -> dict[str, object]:
    resolved = get_render_profile(profile) if isinstance(profile, str) else profile
    return {
        "name": resolved.name,
        "width": resolved.width,
        "height": resolved.height,
        "fps_num": resolved.fps_num,
        "fps_den": resolved.fps_den,
        "pixel_format": resolved.pixel_format,
        "audio_sample_rate": resolved.audio_sample_rate,
        "audio_channels": resolved.audio_channels,
        "video_encoder": resolved.video_encoder,
        "fallback_video_encoder": resolved.fallback_video_encoder,
    }


def profiles() -> Iterable[RenderProfile]:
    """Iterate over registry values without exposing its mutable mapping."""

    return tuple(_PROFILES.values())


__all__ = [
    "get_render_profile",
    "list_render_profiles",
    "profiles",
    "resolve_encoder",
    "serialize_render_profile",
    "validate_render_profile",
]
