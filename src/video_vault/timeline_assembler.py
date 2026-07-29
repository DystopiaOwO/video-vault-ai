"""Synchronous hard-cut assembly for normalized Segment Cache outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Callable, Sequence

from .audio_pipeline import build_project_audio_filter
from .encoder_contract import encoder_arguments


class TimelineAssemblyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimelineAssemblyResult:
    output_path: Path
    concat_path: Path
    duration_seconds: float
    command: tuple[str, ...]


def escape_ffconcat_path(path: Path | str) -> str:
    """Return a concat-demuxer-safe path without invoking a shell."""
    value = str(Path(path).expanduser().resolve()).replace("\\", "/")
    return value.replace("'", "'\\''")


def build_concat_file(segment_paths: Sequence[Path | str], output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ffconcat version 1.0"]
    for path in segment_paths:
        absolute = Path(path).expanduser().resolve()
        if not absolute.is_file():
            raise TimelineAssemblyError(f"segment cache file does not exist: {absolute}")
        lines.append(f"file '{escape_ffconcat_path(absolute)}'")
    if len(lines) == 1:
        raise TimelineAssemblyError("cannot assemble an empty segment list")
    temp = output_path.with_name(f".{output_path.name}.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return output_path


def build_timeline_command(
    ffmpeg_path: str,
    concat_path: Path,
    output_path: Path,
    *,
    include_audio: bool = True,
    duration_seconds: float | None = None,
    normalization: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    video_filter: str | None = None,
    encoder_contract: dict[str, Any] | None = None,
) -> list[str]:
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
        "-map",
        "0:v:0",
    ]
    if include_audio and not (normalization and normalization.get("enabled")):
        command += ["-map", "0:a:0"]
    if video_filter:
        if not profile or not encoder_contract:
            raise TimelineAssemblyError(
                "visual composition requires profile and encoder contract"
            )
        command += ["-vf", video_filter]
        command += encoder_arguments(encoder_contract)
        command += ["-pix_fmt", str(profile["pixel_format"])]
    else:
        if profile and encoder_contract:
            command += encoder_arguments(encoder_contract)
            command += ["-r", str(profile["fps"]), "-fps_mode", "cfr", "-pix_fmt", str(profile["pixel_format"])]
        else:
            command += ["-c:v", "copy"]
    if include_audio and normalization and normalization.get("enabled"):
        if not profile:
            raise TimelineAssemblyError("audio normalization requires a render profile")
        command += [
            "-filter_complex",
            build_project_audio_filter(
                profile,
                normalization,
                duration_seconds=duration_seconds,
            ),
            "-map",
            "[aout]",
            "-c:a",
            str(profile["audio_codec"]),
            "-ar",
            str(profile["audio_sample_rate"]),
            "-ac",
            str(profile["audio_channels"]),
        ]
    elif include_audio:
        command += ["-c:a", "copy"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero"]
    if profile:
        command += ["-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"), "-colorspace", str(profile.get("color_matrix") or "bt709"), "-color_range", str(profile.get("color_range") or "tv")]
    if duration_seconds is not None:
        command += ["-t", f"{float(duration_seconds):.6f}"]
    command += ["-f", "mp4", str(Path(output_path))]
    return command


def run_command(command: list[str], runner: Callable[..., Any] | None = None, *, expected_duration_seconds: float | None = None) -> Any:
    if runner is None:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if hasattr(runner, "run"):
        try:
            return runner.run(command, capture_output=True, text=True, check=False, expected_duration_seconds=expected_duration_seconds)
        except TypeError:
            return runner.run(command)
    try:
        return runner(command, capture_output=True, text=True, check=False, expected_duration_seconds=expected_duration_seconds)
    except TypeError:
        return runner(command)


def assemble_timeline(
    segment_paths: Sequence[Path | str],
    work_dir: Path,
    output_path: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    duration_seconds: float | None = None,
    runner: Callable[..., Any] | None = None,
) -> TimelineAssemblyResult:
    concat_path = build_concat_file(segment_paths, Path(work_dir) / "timeline.ffconcat")
    command = build_timeline_command(ffmpeg_path, concat_path, output_path, duration_seconds=duration_seconds)
    result = run_command(command, runner, expected_duration_seconds=duration_seconds)
    if int(getattr(result, "returncode", 0) or 0) != 0:
        raise TimelineAssemblyError(str(getattr(result, "stderr", "") or "FFmpeg timeline assembly failed"))
    return TimelineAssemblyResult(Path(output_path), concat_path, float(duration_seconds or 0), tuple(command))


__all__ = [
    "TimelineAssemblyError",
    "TimelineAssemblyResult",
    "assemble_timeline",
    "build_concat_file",
    "build_timeline_command",
    "escape_ffconcat_path",
    "run_command",
]
