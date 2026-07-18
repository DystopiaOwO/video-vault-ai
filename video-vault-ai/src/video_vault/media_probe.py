"""FFprobe adapter with normalized results and source-aware cache."""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .render_types import MediaProbeResult


class MediaProbeError(RuntimeError):
    """Raised when ffprobe cannot produce a valid media description."""


def probe_media(source: str | Path, cfg: Mapping[str, Any], *, cache_dir: str | Path | None = None, force: bool = False) -> MediaProbeResult:
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise MediaProbeError(f"Source file does not exist: {path}")
    stat = path.stat()
    cache_path = _cache_path(path, cache_dir)
    if not force and cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source_size") == stat.st_size and cached.get("source_mtime_ns") == stat.st_mtime_ns:
                return MediaProbeResult(**cached)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    command = [str(cfg.get("ffprobe_path", "ffprobe")), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise MediaProbeError(f"Unable to start ffprobe: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown ffprobe error").strip()
        raise MediaProbeError(f"ffprobe failed for {path}: {detail}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    result = _normalize(path, raw, stat.st_size, stat.st_mtime_ns)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def probe(source: str | Path, cfg: Mapping[str, Any], **kwargs: Any) -> MediaProbeResult:
    """Compatibility alias used by foundation callers."""

    return probe_media(source, cfg, **kwargs)


def _cache_path(source: Path, cache_dir: str | Path | None) -> Path | None:
    if cache_dir is None:
        return None
    safe_name = f"{hashlib.sha256(str(source).encode('utf-8')).hexdigest()}.json"
    return Path(cache_dir) / safe_name


def _normalize(path: Path, raw: Mapping[str, Any], size: int, mtime_ns: int) -> MediaProbeResult:
    streams = list(raw.get("streams") or [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    fps_num, fps_den = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    r_num, r_den = _fraction(video.get("r_frame_rate"))
    is_vfr = bool(video.get("avg_frame_rate") and video.get("r_frame_rate") and (fps_num, fps_den) != (r_num, r_den))
    rotation = _rotation(video)
    duration = _number(video.get("duration")) or _number((raw.get("format") or {}).get("duration")) or 0.0
    audio_sample_rate = _integer(audio[0].get("sample_rate")) if audio else None
    audio_channels = _integer(audio[0].get("channels")) if audio else None
    color_keys = ("color_range", "color_space", "color_transfer", "color_primaries")
    color_metadata = {key: video[key] for key in color_keys if video.get(key) not in (None, "")}
    return MediaProbeResult(
        source_file=str(path), duration_ms=max(0, round(duration * 1000)), width=_integer(video.get("width")) or 0,
        height=_integer(video.get("height")) or 0, rotation=rotation, fps_num=fps_num, fps_den=fps_den,
        is_vfr=is_vfr, time_base=str(video.get("time_base") or ""), pixel_format=str(video.get("pix_fmt") or ""),
        color_metadata=color_metadata, has_audio=bool(audio), audio_track_count=len(audio),
        audio_sample_rate=audio_sample_rate, audio_channels=audio_channels, source_size=size, source_mtime_ns=mtime_ns,
    )


def _fraction(value: Any) -> tuple[int, int]:
    if not value or value in ("0/0", "N/A"):
        return 0, 1
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator = int(denominator)
        return (int(numerator), denominator or 1)
    except (ValueError, TypeError):
        return 0, 1


def _rotation(stream: Mapping[str, Any]) -> int:
    tags = stream.get("tags") or {}
    side_data = stream.get("side_data_list") or []
    value = tags.get("rotate") or next((item.get("rotation") for item in side_data if item.get("rotation") is not None), 0)
    return _integer(value) or 0


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "N/A", "") else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "N/A", "") else None
    except (TypeError, ValueError):
        return None


__all__ = ["MediaProbeError", "MediaProbeResult", "probe", "probe_media"]
