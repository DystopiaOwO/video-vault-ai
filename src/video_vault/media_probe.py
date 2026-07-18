"""Small, safe ffprobe adapter used by single-segment rendering."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .render_errors import MediaProbeError


@dataclass(frozen=True)
class MediaProbe:
    source_file: Path
    duration_seconds: float
    has_video: bool
    has_audio: bool
    width: int
    height: int
    fps: float
    fps_num: int
    fps_den: int
    pixel_format: str
    video_codec: str
    audio_codec: str
    sample_rate: int
    channels: int


MediaProbeResult = MediaProbe


def probe_media(ffprobe_path: str, path: Path) -> MediaProbe:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise MediaProbeError(f"source file does not exist: {source}")
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as exc:
        raise MediaProbeError(f"unable to start ffprobe: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffprobe error").strip()
        raise MediaProbeError(f"ffprobe failed for {source}: {detail}")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"ffprobe returned invalid JSON for {source}") from exc
    return _parse(source, raw)


def _parse(source: Path, raw: dict[str, Any]) -> MediaProbe:
    streams = list(raw.get("streams") or [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise MediaProbeError(f"no video stream found: {source}")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    fps_num, fps_den = _video_frame_rate(video)
    duration = _number((raw.get("format") or {}).get("duration")) or _number(video.get("duration")) or 0.0
    return MediaProbe(
        source_file=source,
        duration_seconds=duration,
        has_video=True,
        has_audio=audio is not None,
        width=_integer(video.get("width")),
        height=_integer(video.get("height")),
        fps=fps_num / fps_den if fps_den else 0.0,
        fps_num=fps_num,
        fps_den=fps_den,
        pixel_format=str(video.get("pix_fmt") or ""),
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or "") if audio else "",
        sample_rate=_integer(audio.get("sample_rate")) if audio else 0,
        channels=_integer(audio.get("channels")) if audio else 0,
    )


def _fraction(value: Any) -> tuple[int, int]:
    if value is None or str(value).strip().upper() in {"", "N/A", "0/0"}:
        raise MediaProbeError(f"invalid frame rate: {value!r}")
    try:
        numerator, denominator = str(value).strip().split("/", 1)
        num = int(numerator)
        den = int(denominator)
        if num <= 0 or den <= 0:
            raise ValueError
        return num, den
    except (TypeError, ValueError):
        raise MediaProbeError(f"invalid frame rate: {value!r}")


def _video_frame_rate(video: Mapping[str, Any]) -> tuple[int, int]:
    failures: list[str] = []
    for field in ("avg_frame_rate", "r_frame_rate"):
        value = video.get(field)
        try:
            return _fraction(value)
        except MediaProbeError as exc:
            failures.append(f"{field}={value!r} ({exc})")
    raise MediaProbeError("video has no valid frame rate; tried " + "; ".join(failures))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["MediaProbe", "MediaProbeError", "MediaProbeResult", "probe_media"]
