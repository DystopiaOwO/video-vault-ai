"""FFmpeg audio graph helpers for Render Pipeline v2.

The module only describes audio processing.  It does not own timeline order or
manifest compilation; callers pass the already ordered manifest segments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .render_types import BgmSettings, RenderSegment

AUDIO_ROLES = {"keep_original", "lower_original", "mute", "dialogue"}


@dataclass(frozen=True)
class AudioMixOptions:
    sample_rate: int = 48000
    channels: int = 2
    crossfade_ms: int = 80
    master_limiter: bool = True
    loudness_normalize: bool = False


def atempo_chain(speed: float) -> str:
    """Return an FFmpeg atempo chain supporting 0.25 through 4.0."""
    speed = float(speed)
    if not 0.25 <= speed <= 4.0:
        raise ValueError("audio speed must be between 0.25 and 4.0")
    # atempo accepts 0.5..2.0 per filter.  Factor values deterministically.
    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    if abs(remaining - 1.0) > 1e-9:
        factors.append(remaining)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def normalize_audio_role(role: str | None) -> str:
    value = str(role or "keep_original")
    if value not in AUDIO_ROLES:
        raise ValueError(f"unsupported audio role: {value}")
    return value


def segment_audio_filter(segment: RenderSegment, input_label: str = "0:a") -> str:
    """Build a normalized source-audio chain for one reviewed segment."""
    role = normalize_audio_role(segment.audio_role)
    if role == "mute":
        return f"[{input_label}]volume=0,aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[{segment.segment_id}_a]"
    filters = ["aresample=48000", "aformat=channel_layouts=stereo", "asetpts=PTS-STARTPTS"]
    if segment.speed != 1.0:
        filters.append(atempo_chain(segment.speed))
    if role == "lower_original":
        filters.append("volume=0.35")
    return f"[{input_label}]" + ",".join(filters) + f"[{segment.segment_id}_a]"


def dialogue_ducking_filter(*, input_label: str = "bgm_prepared", output_label: str = "bgm_ducked") -> str:
    """Lower BGM for a dialogue segment without changing its timeline PTS."""

    return f"[{input_label}]volume=0.35[{output_label}]"


def silence_filter(duration_ms: int, label: str = "silence") -> str:
    duration = max(0, int(duration_ms)) / 1000
    return f"anullsrc=r=48000:cl=stereo:d={duration:.3f}[{label}]"


def bgm_filter(bgm: BgmSettings, duration_ms: int, input_label: str = "bgm") -> str:
    """Build loop, trim, fades and volume for a BGM input."""
    if not bgm.enabled or not bgm.source_file:
        return ""
    duration = max(0, duration_ms) / 1000
    parts = [f"[{input_label}:a]", "aresample=48000", "aformat=channel_layouts=stereo"]
    if bgm.loop:
        parts.append("aloop=loop=-1:size=2e+09")
    parts.append(f"atrim=duration={duration:.3f}")
    parts.append("asetpts=PTS-STARTPTS")
    if bgm.fade_in_ms:
        parts.append(f"afade=t=in:st=0:d={bgm.fade_in_ms / 1000:.3f}")
    if bgm.fade_out_ms:
        start = max(0, duration - bgm.fade_out_ms / 1000)
        parts.append(f"afade=t=out:st={start:.3f}:d={bgm.fade_out_ms / 1000:.3f}")
    parts.append(f"volume={10 ** (bgm.volume_db / 20):.8f}")
    return ",".join(parts) + f"[{input_label}_prepared]"


def build_audio_filtergraph(
    segments: Sequence[RenderSegment],
    *,
    bgm: BgmSettings | None = None,
    timeline_duration_ms: int = 0,
    options: AudioMixOptions | None = None,
) -> str:
    """Return an audio-only filtergraph for ordered segments.

    Empty or missing source audio is represented by silence by the caller using
    ``segment_audio_filter``'s input mapping; this graph never references a
    second timeline ordering.
    """
    options = options or AudioMixOptions()
    if options.sample_rate != 48000 or options.channels != 2:
        raise ValueError("Render Pipeline v2 audio output must be 48kHz stereo")
    if not segments:
        return silence_filter(timeline_duration_ms, "mixed")
    labels = [f"{segment.segment_id}_a" for segment in segments]
    graph: list[str] = []
    if len(labels) == 1:
        mixed = f"[{labels[0]}]"
    else:
        crossfade = max(0, options.crossfade_ms) / 1000
        # Across pre-rendered segment clips, acrossfade keeps PTS monotonic.
        mixed = f"[{labels[0]}]"
        for index, label in enumerate(labels[1:], 1):
            out = f"crossfade_{index}"
            graph.append(f"{mixed}[{label}]acrossfade=d={crossfade:.3f}:c1=tri:c2=tri[{out}]")
            mixed = f"[{out}]"
    if bgm and bgm.enabled and bgm.source_file:
        graph.append(bgm_filter(bgm, timeline_duration_ms))
        if any(normalize_audio_role(segment.audio_role) == "dialogue" for segment in segments):
            graph.append(dialogue_ducking_filter())
            graph.append(f"{mixed}[bgm_ducked]amix=inputs=2:duration=first:dropout_transition=0[mixed_bgm]")
        else:
            graph.append(f"{mixed}[bgm_prepared]amix=inputs=2:duration=first:dropout_transition=0[mixed_bgm]")
        mixed = "[mixed_bgm]"
    else:
        graph.append(f"{mixed}anull[mixed_bgm]")
        mixed = "[mixed_bgm]"
    final = f"{mixed}aresample=48000,aformat=channel_layouts=stereo"
    if options.master_limiter:
        final += ",alimiter=limit=0.95:level_in=1:level_out=1"
    if options.loudness_normalize:
        final += ",loudnorm=I=-14:TP=-1.5:LRA=11"
    return ";".join(graph + [final + "[mixed]"])


__all__ = ["AUDIO_ROLES", "AudioMixOptions", "atempo_chain", "bgm_filter", "build_audio_filtergraph", "dialogue_ducking_filter", "normalize_audio_role", "segment_audio_filter", "silence_filter"]
