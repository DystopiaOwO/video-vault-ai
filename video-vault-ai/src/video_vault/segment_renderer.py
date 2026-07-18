"""Accurate, normalized FFmpeg rendering for one reviewed segment."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from .color_pipeline import check_color_metadata, color_filter, validate_color_settings
from .media_probe import MediaProbeResult, probe_media
from .render_profiles import get_render_profile
from .render_types import ColorSettings, RenderProfile, RenderSegment
from .segment_cache import cache_paths, cache_key_payload, is_valid_cache, segment_cache_key, write_cache_metadata


@dataclass(frozen=True)
class SegmentRenderResult:
    path: Path
    cache_key: str
    cache_hit: bool
    warnings: tuple[str, ...] = ()


def atempo_chain(speed: float) -> str:
    if not 0.25 <= speed <= 4.0:
        raise ValueError("speed must be between 0.25 and 4.0")
    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:g}" for factor in factors)


def build_segment_ffmpeg_command(
    segment: RenderSegment,
    probe: MediaProbeResult,
    profile: RenderProfile | str,
    *,
    ffmpeg_path: str = "ffmpeg",
    output: str | Path = "segment.mp4",
    encoder: str | None = None,
    color: ColorSettings | Mapping[str, Any] | None = None,
) -> list[str]:
    resolved = get_render_profile(profile) if isinstance(profile, str) else profile
    duration = max(0.0, (segment.source_out_ms - segment.source_in_ms) / 1000.0)
    if duration <= 0:
        raise ValueError("segment source_out must be greater than source_in")
    speed = float(segment.speed)
    if not 0.25 <= speed <= 4.0:
        raise ValueError("speed must be between 0.25 and 4.0")
    vf = [f"trim=start={segment.source_in_ms / 1000:.3f}:duration={duration:.3f}", "setpts=PTS-STARTPTS"]
    if speed != 1.0:
        vf.append(f"setpts=PTS/{speed:g}")
    vf.append(f"scale={resolved.width}:{resolved.height}:force_original_aspect_ratio=decrease")
    vf.append(f"pad={resolved.width}:{resolved.height}:(ow-iw)/2:(oh-ih)/2")
    vf.append(f"fps={resolved.fps_num}/{resolved.fps_den}")
    vf.append(f"format={resolved.pixel_format}")
    color_expr = color_filter(color)
    if color_expr:
        vf.append(color_expr)
    args = [ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-i", segment.source_file]
    filter_complex: list[str] = [f"[0:v]{','.join(vf)}[vout]"]
    if probe.has_audio:
        af = [f"atrim=start={segment.source_in_ms / 1000:.3f}:duration={duration:.3f}", "asetpts=PTS-STARTPTS"]
        if speed != 1.0:
            af.append(atempo_chain(speed))
        af.extend(["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo", "asetpts=PTS-STARTPTS"])
        filter_complex.append(f"[0:a]{','.join(af)}[aout]")
        audio_map = "[aout]"
    else:
        args.extend(["-f", "lavfi", "-t", f"{duration / speed:.3f}", "-i", "anullsrc=r=48000:cl=stereo"])
        filter_complex.append(f"[1:a]atrim=duration={duration / speed:.3f},asetpts=PTS-STARTPTS[aout]")
        audio_map = "[aout]"
    requested_encoder = encoder or resolved.video_encoder
    args.extend(["-filter_complex", ";".join(filter_complex), "-map", "[vout]", "-map", audio_map,
                 "-c:v", requested_encoder, "-r", f"{resolved.fps_num}/{resolved.fps_den}",
                 "-pix_fmt", resolved.pixel_format, "-c:a", "aac", "-ar", "48000", "-ac", "2",
                 "-movflags", "+faststart", str(output)])
    return args


def render_segment(
    segment: RenderSegment,
    cfg: Mapping[str, Any],
    *,
    cache_dir: str | Path,
    profile: RenderProfile | str = "preview_1080p30",
    color: ColorSettings | Mapping[str, Any] | None = None,
    encoder: str | None = None,
    force: bool = False,
    runner: Callable[..., Any] | None = None,
) -> SegmentRenderResult:
    resolved = get_render_profile(profile) if isinstance(profile, str) else profile
    selected_encoder = encoder or resolved.video_encoder
    color_settings = validate_color_settings(color)
    payload = cache_key_payload(segment, color=color_settings, profile=resolved, encoder=selected_encoder)
    key = segment_cache_key(segment, color=color_settings, profile=resolved, encoder=selected_encoder)
    paths = cache_paths(cache_dir, key)
    if not force and is_valid_cache(paths, payload):
        return SegmentRenderResult(paths.media, key, True)
    probe = probe_media(segment.source_file, cfg, cache_dir=Path(cache_dir) / "probe")
    warnings = tuple(check_color_metadata(probe.color_metadata))
    paths.media.parent.mkdir(parents=True, exist_ok=True)
    command = build_segment_ffmpeg_command(segment, probe, resolved, ffmpeg_path=str(cfg.get("ffmpeg_path", "ffmpeg")), output=paths.media, encoder=selected_encoder, color=color_settings)
    execute = runner or subprocess.run
    try:
        completed = execute(command, check=True, capture_output=True, text=True, encoding="utf-8")
        stderr = getattr(completed, "stderr", "") or ""
    except Exception:
        if paths.media.exists() and paths.media.stat().st_size == 0:
            paths.media.unlink()
        raise
    paths.log.write_text(stderr, encoding="utf-8")
    write_cache_metadata(paths, payload, source_file=segment.source_file, duration_ms=round((segment.source_out_ms - segment.source_in_ms) / segment.speed), warnings=list(warnings))
    return SegmentRenderResult(paths.media, key, False, warnings)


__all__ = ["SegmentRenderResult", "atempo_chain", "build_segment_ffmpeg_command", "render_segment"]
