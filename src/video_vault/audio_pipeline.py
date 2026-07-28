"""Pure audio filter helpers for one normalized segment."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .audio_state import AUDIO_ROLES as PROJECT_AUDIO_ROLES


AUDIO_ROLES = {"keep_original", "lower_original", "mute", "keep", "lower", "bgm_only"}


def build_atempo_chain(speed: float) -> list[float]:
    value = float(speed)
    if not 0.25 <= value <= 4.0:
        raise ValueError("speed must be between 0.25 and 4.0")
    factors: list[float] = []
    remaining = value
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    if not math.isclose(remaining, 1.0, rel_tol=0, abs_tol=1e-9):
        factors.append(remaining)
    return factors


def atempo_filter(speed: float) -> str:
    return ",".join(f"atempo={factor:g}" for factor in build_atempo_chain(speed))


def normalize_audio_role(role: str | None) -> str:
    value = str(role or "keep_original")
    if value in {"keep_original", "lower_original"}:
        return value
    if value in PROJECT_AUDIO_ROLES:
        return value
    raise ValueError(f"unsupported audio role: {value}")


def audio_gain_db(role: str, settings: Mapping[str, Any] | None) -> float:
    role = normalize_audio_role(role)
    audio = dict((settings or {}).get("audio") or {})
    original = dict(audio.get("original_audio") or {})
    if role in {"mute", "bgm_only"}:
        return 0.0
    if role in {"lower_original", "lower"}:
        return float(original.get("lower_volume_db", audio.get("lower_original_gain_db", -8.0)))
    return float(original.get("default_volume_db", audio.get("original_gain_db", 0.0)))


def build_audio_filter(
    role: str,
    speed: float,
    settings: Mapping[str, Any] | None,
    *,
    start: float,
    end: float,
    audio_settings: Mapping[str, Any] | None = None,
) -> str:
    role = normalize_audio_role(role)
    segment_audio = dict(audio_settings or {})
    filters = [f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS"]
    tempo = atempo_filter(speed)
    if tempo:
        filters.append(tempo)
    filters.extend(["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"])
    effective_gain = segment_audio.get("volume_db")
    if effective_gain is None:
        effective_gain = audio_gain_db(role, settings)
    if role in {"mute", "bgm_only"}:
        filters.append("volume=0")
    else:
        filters.append(f"volume={10 ** (float(effective_gain) / 20):.8f}")
    timeline_duration = max(0.0, (float(end) - float(start)) / float(speed))
    fade_in = max(0.0, float(segment_audio.get("fade_in_seconds", 0.0) or 0.0))
    fade_out = max(0.0, float(segment_audio.get("fade_out_seconds", 0.0) or 0.0))
    preview_slice = bool(segment_audio.get("_preview_slice", False))
    timeline_offset = max(0.0, float(segment_audio.get("_timeline_offset_seconds", 0.0) or 0.0))
    segment_timeline_duration = max(
        timeline_duration,
        float(segment_audio.get("_segment_timeline_duration_seconds", timeline_duration) or timeline_duration),
    )
    if fade_in > 0 and (not preview_slice or timeline_offset < fade_in):
        remaining = fade_in - timeline_offset if preview_slice else fade_in
        if remaining > 0:
            filters.append(f"afade=t=in:st=0:d={min(remaining, timeline_duration):.6f}")
    if fade_out > 0 and timeline_duration > 0:
        fade_start = segment_timeline_duration - fade_out - timeline_offset if preview_slice else timeline_duration - fade_out
        if fade_start < timeline_duration:
            local_start = max(0.0, fade_start)
            filters.append(f"afade=t=out:st={local_start:.6f}:d={min(fade_out, timeline_duration - local_start):.6f}")
    filters.append("asetpts=PTS-STARTPTS")
    return ",".join(filters)


def build_silence_filter(duration: float) -> str:
    return f"anullsrc=r=48000:cl=stereo:d={max(0.0, float(duration)):.6f}"


def build_project_audio_filter(
    profile: Mapping[str, Any],
    normalization: Mapping[str, Any] | None = None,
    *,
    bgm_label: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    """Build the shared final project-audio filter used by preview and final render."""
    if bgm_label:
        graph = f"[0:a][{bgm_label}]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
    else:
        graph = "[0:a]anull"
    graph += f",aresample={int(profile['audio_sample_rate'])},aformat=sample_fmts=fltp:channel_layouts=stereo"
    if duration_seconds is not None:
        duration = max(0.001, float(duration_seconds))
        graph += f",apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"
    norm = dict(normalization or {})
    if bool(norm.get("enabled", False)):
        target = float(norm.get("target_lufs", -14.0))
        peak = float(norm.get("true_peak_db", -1.0))
        graph += f",loudnorm=I={target:.3f}:TP={peak:.3f}:LRA=11"
    return graph + "[aout]"


__all__ = ["AUDIO_ROLES", "audio_gain_db", "atempo_filter", "build_atempo_chain", "build_audio_filter", "build_project_audio_filter", "build_silence_filter", "normalize_audio_role"]
