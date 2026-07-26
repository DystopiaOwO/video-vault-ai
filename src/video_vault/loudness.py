"""Deterministic two-pass loudnorm helpers for formal renders."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping


class LoudnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoudnessMeasurement:
    measured_I: float
    measured_LRA: float
    measured_TP: float
    measured_thresh: float
    offset: float
    target_lufs: float
    true_peak_db: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def measure_loudness(ffmpeg_path: str, source: Path, normalization: Mapping[str, Any]) -> LoudnessMeasurement:
    target = float(normalization.get("target_lufs", -14.0))
    peak = float(normalization.get("true_peak_db", -1.0))
    command = [str(ffmpeg_path), "-hide_banner", "-nostdin", "-i", str(Path(source).resolve()), "-map", "0:a:0", "-af", f"loudnorm=I={target:.3f}:TP={peak:.3f}:LRA=11:print_format=json", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoudnessError(f"loudness analysis could not start: {exc}") from exc
    if result.returncode != 0:
        raise LoudnessError("loudness analysis failed: " + (result.stderr or result.stdout or "unknown error")[-1000:])
    payload = _loudnorm_json(result.stderr or result.stdout)
    try:
        return LoudnessMeasurement(
            measured_I=float(payload["input_i"]),
            measured_LRA=float(payload["input_lra"]),
            measured_TP=float(payload["input_tp"]),
            measured_thresh=float(payload["input_thresh"]),
            offset=float(payload["target_offset"]),
            target_lufs=target,
            true_peak_db=peak,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LoudnessError("loudness analysis returned incomplete JSON") from exc


def build_second_pass_command(ffmpeg_path: str, source: Path, output: Path, profile: Mapping[str, Any], measurement: LoudnessMeasurement) -> list[str]:
    filter_value = (
        f"loudnorm=I={measurement.target_lufs:.3f}:TP={measurement.true_peak_db:.3f}:LRA=11:"
        f"measured_I={measurement.measured_I:.3f}:measured_LRA={measurement.measured_LRA:.3f}:"
        f"measured_TP={measurement.measured_TP:.3f}:measured_thresh={measurement.measured_thresh:.3f}:"
        f"offset={measurement.offset:.3f}:linear=true:print_format=summary"
    )
    return [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(Path(source).resolve()),
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-af", filter_value,
        "-c:a", str(profile["audio_codec"]), "-ar", str(profile["audio_sample_rate"]), "-ac", str(profile["audio_channels"]),
        "-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"),
        "-colorspace", str(profile.get("color_matrix") or "bt709"), "-color_range", str(profile.get("color_range") or "tv"),
        "-movflags", "+faststart", "-f", "mp4", str(Path(output)),
    ]


def _loudnorm_json(text: str) -> dict[str, Any]:
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LoudnessError("loudness analysis did not return JSON")
    return json.loads(text[start:end + 1])


__all__ = ["LoudnessError", "LoudnessMeasurement", "build_second_pass_command", "measure_loudness"]
