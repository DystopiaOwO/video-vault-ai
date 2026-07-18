"""Pure audio filter helpers for one normalized segment."""

from __future__ import annotations

import math
from typing import Any, Mapping


AUDIO_ROLES = {"keep_original", "lower_original", "mute"}


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
    if value not in AUDIO_ROLES:
        raise ValueError(f"unsupported audio role: {value}")
    return value


def audio_gain_db(role: str, settings: Mapping[str, Any] | None) -> float:
    normalize_audio_role(role)
    audio = dict((settings or {}).get("audio") or {})
    field = "lower_original_gain_db" if role == "lower_original" else "original_gain_db"
    return 0.0 if role == "mute" else float(audio.get(field, 0.0))


def build_audio_filter(role: str, speed: float, settings: Mapping[str, Any] | None, *, start: float, end: float) -> str:
    role = normalize_audio_role(role)
    filters = [f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS"]
    tempo = atempo_filter(speed)
    if tempo:
        filters.append(tempo)
    filters.extend(["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"])
    if role == "mute":
        filters.append("volume=0")
    else:
        filters.append(f"volume={10 ** (audio_gain_db(role, settings) / 20):.8f}")
    filters.append("asetpts=PTS-STARTPTS")
    return ",".join(filters)


def build_silence_filter(duration: float) -> str:
    return f"anullsrc=r=48000:cl=stereo:d={max(0.0, float(duration)):.6f}"


__all__ = ["AUDIO_ROLES", "audio_gain_db", "atempo_filter", "build_atempo_chain", "build_audio_filter", "build_silence_filter", "normalize_audio_role"]
