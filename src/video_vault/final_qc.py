"""Final project output QC and fingerprinting.

The final file is treated as an encoded artifact, not as a promise based on
the input manifest.  When a real probe is available we decode the published
file and inspect packet timestamps as part of the same QC result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .media_probe import probe_media


@dataclass(frozen=True)
class FinalQCResult:
    passed: bool
    duration_seconds: float
    output_sha256: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    measurements: Mapping[str, Any] = field(default_factory=dict)


def validate_final_output(
    output_path: Path,
    manifest: Mapping[str, Any],
    ffprobe_path: str = "ffprobe",
    *,
    ffmpeg_path: str = "ffmpeg",
    loudness: Any | None = None,
) -> FinalQCResult:
    path = Path(output_path)
    digest = _sha256(path) if path.is_file() else ""
    if not path.is_file() or path.stat().st_size <= 0:
        return FinalQCResult(False, 0.0, digest, ("output is missing or empty",))
    try:
        probe = probe_media(ffprobe_path, path)
    except Exception as exc:
        return FinalQCResult(False, 0.0, digest, (str(exc),))

    profile = manifest.get("profile") or {}
    errors: list[str] = []
    warnings: list[str] = list((manifest.get("validation") or {}).get("warnings", []))
    measurements: dict[str, Any] = {
        "duration_seconds": float(getattr(probe, "duration_seconds", 0.0)),
        "fps": {
            "num": int(getattr(probe, "fps_num", 0) or 0),
            "den": int(getattr(probe, "fps_den", 0) or 0),
            "value": float(getattr(probe, "fps", 0.0) or 0.0),
        },
        "frame_count": int(getattr(probe, "frame_count", 0) or 0),
        "video": {
            "start_seconds": float(getattr(probe, "video_start_seconds", 0.0) or 0.0),
            "end_seconds": float(getattr(probe, "video_end_seconds", getattr(probe, "duration_seconds", 0.0)) or 0.0),
        },
        "audio": {
            "start_seconds": float(getattr(probe, "audio_start_seconds", 0.0) or 0.0),
            "end_seconds": float(getattr(probe, "audio_end_seconds", getattr(probe, "duration_seconds", 0.0)) or 0.0),
        },
    }
    if not probe.has_video:
        errors.append("output has no video stream")
    if not probe.has_audio:
        errors.append("output has no audio stream")
    if (probe.width, probe.height) != (int(profile.get("width", 0)), int(profile.get("height", 0))):
        errors.append(f"resolution mismatch: {probe.width}x{probe.height}")
    if abs(probe.fps - float(profile.get("fps", 0))) > 0.01:
        errors.append(f"fps mismatch: {probe.fps}")
    if profile.get("fps_num") and profile.get("fps_den") and (probe.fps_num, probe.fps_den) != (int(profile["fps_num"]), int(profile["fps_den"])):
        errors.append(f"exact FPS mismatch: {probe.fps_num}/{probe.fps_den}")
    if probe.pixel_format != str(profile.get("pixel_format", "")):
        errors.append(f"pixel format mismatch: {probe.pixel_format}")
    if probe.video_codec != str(profile.get("video_codec", "")):
        errors.append(f"video codec mismatch: {probe.video_codec}")
    if probe.audio_codec != str(profile.get("audio_codec", "")):
        errors.append(f"audio codec mismatch: {probe.audio_codec}")
    if probe.sample_rate != int(profile.get("audio_sample_rate", 0)):
        errors.append(f"sample rate mismatch: {probe.sample_rate}")
    if probe.channels != int(profile.get("audio_channels", 0)):
        errors.append(f"channel mismatch: {probe.channels}")
    for actual, expected, label in (
        (getattr(probe, "color_primaries", ""), profile.get("color_primaries", ""), "color primaries"),
        (getattr(probe, "color_transfer", ""), profile.get("color_transfer", ""), "color transfer"),
        (getattr(probe, "color_matrix", ""), profile.get("color_matrix", ""), "color matrix"),
        (getattr(probe, "color_range", ""), profile.get("color_range", ""), "color range"),
    ):
        if expected and not actual:
            errors.append(f"missing {label}: expected {expected}")
        elif expected and actual != expected:
            errors.append(f"{label} mismatch: {actual}")

    expected_duration = sum(float(item.get("timeline_duration_seconds", 0)) for item in manifest.get("segments", []))
    tolerance = max(0.15, 3 / float(profile.get("fps", 30)))
    if abs(probe.duration_seconds - expected_duration) > tolerance:
        errors.append(f"duration mismatch: {probe.duration_seconds:.3f} vs {expected_duration:.3f}")
    if getattr(probe, "source_file", None) is not None:
        decode = _decode_report(path, ffmpeg_path)
        measurements["decode"] = decode
        if not decode["ok"]:
            errors.append("full decode validation failed")
            if decode.get("stderr"):
                errors.append("decode: " + str(decode["stderr"])[-1000:])
        packet = _packet_diagnostics(path, ffprobe_path)
        measurements.update(packet)
        errors.extend(packet.get("errors", []))
        warnings.extend(packet.get("warnings", []))
        stream_ends = packet.get("stream_ends") or {}
        stream_starts = packet.get("stream_starts") or {}
        video_stream_value = getattr(probe, "video_stream_index", -1)
        audio_stream_value = getattr(probe, "audio_stream_index", -1)
        video_stream = int(video_stream_value) if video_stream_value is not None else -1
        audio_stream = int(audio_stream_value) if audio_stream_value is not None else -1
        if video_stream < 0:
            errors.append("video stream index unavailable for packet-tail validation")
        elif video_stream not in stream_ends:
            errors.append("video packet tail unavailable")
        else:
            measurements["video"]["end_seconds"] = float(stream_ends[video_stream])
            if video_stream in stream_starts:
                measurements["video"]["start_seconds"] = float(stream_starts[video_stream])
            measurements["video"]["tail_source"] = "packet"
        if probe.has_audio:
            if audio_stream < 0:
                errors.append("audio stream index unavailable for packet-tail validation")
            elif audio_stream not in stream_ends:
                errors.append("audio packet tail unavailable")
            else:
                measurements["audio"]["end_seconds"] = float(stream_ends[audio_stream])
                if audio_stream in stream_starts:
                    measurements["audio"]["start_seconds"] = float(stream_starts[audio_stream])
                measurements["audio"]["tail_source"] = "packet"

    expected_frames = int(round(expected_duration * float(profile.get("fps", 0) or 0))) if expected_duration and profile.get("fps") else 0
    actual_frames = int(measurements.get("frame_count", 0) or 0)
    if expected_frames:
        if actual_frames <= 0:
            errors.append(f"frame count unavailable: expected approximately {expected_frames}")
        elif abs(actual_frames - expected_frames) > 1:
            errors.append(f"frame count mismatch: {actual_frames} vs {expected_frames}")
    video_end = float((measurements.get("video") or {}).get("end_seconds") or 0)
    audio_end = float((measurements.get("audio") or {}).get("end_seconds") or 0)
    tail_tolerance = max(0.05, 2 / float(profile.get("fps", 30) or 30))
    if (
        getattr(probe, "source_file", None) is not None
        and "tail_source" in (measurements.get("video") or {})
        and "tail_source" in (measurements.get("audio") or {})
        and abs(
            (video_end - float((measurements.get("video") or {}).get("start_seconds") or 0))
            - (audio_end - float((measurements.get("audio") or {}).get("start_seconds") or 0))
        ) > tail_tolerance
    ):
        video_span = video_end - float((measurements.get("video") or {}).get("start_seconds") or 0)
        audio_span = audio_end - float((measurements.get("audio") or {}).get("start_seconds") or 0)
        errors.append(f"A/V tail drift: {abs(video_span - audio_span):.3f}s")

    if loudness is not None:
        target_lufs = float(getattr(loudness, "target_lufs", -14.0))
        true_peak = float(getattr(loudness, "true_peak_db", -1.0))
        measured_lufs = float(getattr(loudness, "measured_I", -999.0))
        measured_peak = float(getattr(loudness, "measured_TP", 999.0))
        measurements["loudness"] = {"measured_lufs": measured_lufs, "true_peak_db": measured_peak, "target_lufs": target_lufs, "true_peak_limit_db": true_peak}
        if not math.isfinite(measured_lufs) or abs(measured_lufs - target_lufs) > 1.0:
            errors.append(f"loudness mismatch: {measured_lufs:.2f} LUFS vs {target_lufs:.2f} LUFS")
        if not math.isfinite(measured_peak) or measured_peak > true_peak + 0.1:
            errors.append(f"true peak exceeds policy: {measured_peak:.2f} dBTP vs {true_peak:.2f} dBTP")
    return FinalQCResult(not errors, probe.duration_seconds, digest, tuple(errors), tuple(warnings), measurements)


def sha256_file(path: Path) -> str:
    return _sha256(Path(path))


def _sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_report(path: Path, ffmpeg_path: str) -> dict[str, Any]:
    command = [str(ffmpeg_path), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-map", "0", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)
        return {"ok": result.returncode == 0, "returncode": result.returncode, "stderr": (result.stderr or "").strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": None, "stderr": str(exc)}


def _decode_ok(path: Path, ffmpeg_path: str) -> bool:
    return bool(_decode_report(path, ffmpeg_path)["ok"])


def _packet_diagnostics(path: Path, ffprobe_path: str) -> dict[str, Any]:
    command = [str(ffprobe_path), "-v", "error", "-show_packets", "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)
        if result.returncode != 0:
            return {"timestamp_monotonic": False, "errors": ["timestamp probe failed"], "warnings": []}
        packets = json.loads(result.stdout).get("packets") or []
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return {"timestamp_monotonic": False, "errors": [f"timestamp probe failed: {exc}"], "warnings": []}
    last: dict[int, float] = {}
    counts: dict[int, int] = {}
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    errors: list[str] = []
    for packet in packets:
        try:
            stream = int(packet.get("stream_index"))
            raw_order_time = packet.get("dts_time") if packet.get("dts_time") not in (None, "N/A") else packet.get("pts_time")
            raw_tail_time = packet.get("pts_time") if packet.get("pts_time") not in (None, "N/A") else packet.get("dts_time")
            timestamp = float(raw_order_time)
            tail_timestamp = float(raw_tail_time)
            duration = float(packet.get("duration_time") or 0)
        except (TypeError, ValueError):
            continue
        if stream in last:
            delta = timestamp - last[stream]
            if abs(delta) <= 0.000001:
                errors.append(f"duplicate timestamps on stream {stream}")
                break
            if delta < 0:
                errors.append(f"non-monotonic timestamps on stream {stream}")
                break
        last[stream] = timestamp
        starts.setdefault(stream, tail_timestamp)
        ends[stream] = max(ends.get(stream, tail_timestamp), tail_timestamp + max(0.0, duration))
        counts[stream] = counts.get(stream, 0) + 1
    return {
        "timestamp_monotonic": not errors,
        "timestamp_policy": "strictly_increasing_per_stream",
        "packet_timestamps_available": bool(ends),
        "errors": errors,
        "warnings": [],
        "packet_counts": counts,
        "stream_starts": starts,
        "stream_ends": ends,
    }


__all__ = ["FinalQCResult", "sha256_file", "validate_final_output"]
