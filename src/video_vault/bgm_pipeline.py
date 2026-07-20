"""Single-track BGM validation and mixing filters for Phase 4A."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .media_probe import MediaProbe


class BgmPipelineError(ValueError):
    pass


def validate_bgm_track(track: Mapping[str, Any], ffprobe_path: str = "ffprobe") -> MediaProbe:
    source = Path(str(track.get("source_path") or "")).expanduser().resolve()
    if not source.is_file():
        raise BgmPipelineError(f"BGM source does not exist: {source}")
    gain = _finite(track.get("gain_db", 0.0), "BGM gain_db")
    start = _nonnegative(track.get("start_seconds", 0.0), "BGM start_seconds")
    fade_in = _nonnegative(track.get("fade_in_seconds", 0.0), "BGM fade_in_seconds")
    fade_out = _nonnegative(track.get("fade_out_seconds", 0.0), "BGM fade_out_seconds")
    del gain, start, fade_in, fade_out
    try:
        probe = _probe_audio(ffprobe_path, source)
    except Exception as exc:
        raise BgmPipelineError(f"BGM ffprobe failed: {exc}") from exc
    if not probe.has_audio:
        raise BgmPipelineError(f"BGM has no audio stream: {source}")
    return probe


def bgm_fingerprint(track: Mapping[str, Any]) -> dict[str, Any]:
    source = Path(str(track.get("source_path") or "")).expanduser().resolve()
    stat = source.stat() if source.exists() else None
    return {
        "track_id": track.get("track_id"),
        "source_path": str(source),
        "source_size": stat.st_size if stat else None,
        "source_mtime_ns": stat.st_mtime_ns if stat else None,
        "source_sha256": _sha256(source),
        "gain_db": _finite(track.get("gain_db", 0.0), "BGM gain_db"),
        "start_seconds": _finite(track.get("start_seconds", 0.0), "BGM start_seconds"),
        "loop": bool(track.get("loop", True)),
        "fade_in_seconds": _nonnegative(track.get("fade_in_seconds", 0.0), "BGM fade_in_seconds"),
        "fade_out_seconds": _nonnegative(track.get("fade_out_seconds", 0.0), "BGM fade_out_seconds"),
    }


def build_bgm_filter(track: Mapping[str, Any], timeline_duration: float) -> str:
    duration = _positive(timeline_duration, "timeline duration")
    gain_db = _finite(track.get("gain_db", 0.0), "BGM gain_db")
    fade_in = min(_nonnegative(track.get("fade_in_seconds", 0.0), "BGM fade_in_seconds"), duration)
    fade_out = min(_nonnegative(track.get("fade_out_seconds", 0.0), "BGM fade_out_seconds"), duration)
    filters = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    filters.append(f"volume={10 ** (gain_db / 20):.8f}")
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if not bool(track.get("loop", True)):
        filters.append("apad")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, duration - fade_out):.6f}:d={fade_out:.6f}")
    filters.extend([f"atrim=duration={duration:.6f}", "asetpts=PTS-STARTPTS"])
    return ",".join(filters)


def build_bgm_mix_command(
    ffmpeg_path: str,
    concat_path: Path,
    output_path: Path,
    track: Mapping[str, Any],
    timeline_duration: float,
    profile: Mapping[str, Any],
    *,
    normalization: Mapping[str, Any] | None = None,
) -> list[str]:
    source = Path(str(track.get("source_path") or "")).expanduser().resolve()
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(Path(concat_path).resolve()),
    ]
    if bool(track.get("loop", True)):
        command += ["-stream_loop", "-1"]
    start_seconds = max(0.0, _finite(track.get("start_seconds", 0.0), "BGM start_seconds"))
    if start_seconds:
        command += ["-ss", f"{start_seconds:.6f}"]
    command += ["-i", str(source)]
    bgm_filter = build_bgm_filter(track, timeline_duration)
    graph = (
        f"[1:a]{bgm_filter}[bgm];"
        "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"aresample={int(profile['audio_sample_rate'])},aformat=sample_fmts=fltp:channel_layouts=stereo"
    )
    norm = dict(normalization or {})
    if bool(norm.get("enabled", False)):
        target = _finite(norm.get("target_lufs", -14.0), "normalization target_lufs")
        peak = _finite(norm.get("true_peak_db", -1.0), "normalization true_peak_db")
        graph += f",loudnorm=I={target:.3f}:TP={peak:.3f}:LRA=11"
    graph += "[aout]"
    command += [
        "-filter_complex",
        graph,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        str(profile["audio_codec"]),
        "-ar",
        str(profile["audio_sample_rate"]),
        "-ac",
        str(profile["audio_channels"]),
        "-t",
        f"{float(timeline_duration):.6f}",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "mp4",
        str(output_path),
    ]
    return command


def _finite(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BgmPipelineError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise BgmPipelineError(f"{field} must be finite")
    return number


def _nonnegative(value: Any, field: str) -> float:
    number = _finite(value, field)
    if number < 0:
        raise BgmPipelineError(f"{field} cannot be negative")
    return number


def _positive(value: Any, field: str) -> float:
    number = _finite(value, field)
    if number <= 0:
        raise BgmPipelineError(f"{field} must be positive")
    return number


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe_audio(ffprobe_path: str, source: Path) -> MediaProbe:
    command = [str(ffprobe_path), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise BgmPipelineError((result.stderr or result.stdout or "unknown ffprobe error").strip())
    raw = json.loads(result.stdout)
    streams = list(raw.get("streams") or [])
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not audio:
        raise BgmPipelineError(f"no audio stream found: {source}")
    duration = float((raw.get("format") or {}).get("duration") or audio.get("duration") or 0)
    return MediaProbe(
        source_file=source,
        duration_seconds=duration,
        has_video=False,
        has_audio=True,
        width=0,
        height=0,
        fps=0,
        fps_num=0,
        fps_den=1,
        pixel_format="",
        video_codec="",
        audio_codec=str(audio.get("codec_name") or ""),
        sample_rate=int(audio.get("sample_rate") or 0),
        channels=int(audio.get("channels") or 0),
    )


__all__ = ["BgmPipelineError", "bgm_fingerprint", "build_bgm_filter", "build_bgm_mix_command", "validate_bgm_track"]
