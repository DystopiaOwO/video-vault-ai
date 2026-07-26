"""Final project output QC and fingerprinting."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
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


def validate_final_output(output_path: Path, manifest: Mapping[str, Any], ffprobe_path: str = "ffprobe", *, ffmpeg_path: str = "ffmpeg", loudness: Any | None = None) -> FinalQCResult:
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
    if not probe.has_video:
        errors.append("output has no video stream")
    if not probe.has_audio:
        errors.append("output has no audio stream")
    if (probe.width, probe.height) != (int(profile.get("width", 0)), int(profile.get("height", 0))):
        errors.append(f"resolution mismatch: {probe.width}x{probe.height}")
    if abs(probe.fps - float(profile.get("fps", 0))) > 0.01:
        errors.append(f"fps mismatch: {probe.fps}")
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
        if expected and actual and actual != expected:
            errors.append(f"{label} mismatch: {actual}")
    expected = sum(float(item.get("timeline_duration_seconds", 0)) for item in manifest.get("segments", []))
    tolerance = max(0.15, 3 / float(profile.get("fps", 30)))
    if abs(probe.duration_seconds - expected) > tolerance:
        errors.append(f"duration mismatch: {probe.duration_seconds:.3f} vs {expected:.3f}")
    if getattr(probe, "source_file", None) is not None and not _decode_ok(path, ffmpeg_path):
        errors.append("full decode validation failed")
    if loudness is not None:
        # The measured first-pass value is not the final LUFS value, but it is
        # persisted for auditability. Final measurement is recorded separately
        # by the renderer when the profile enables normalisation.
        if float(getattr(loudness, "measured_TP", -999)) > float(getattr(loudness, "true_peak_db", -1.0)) + 12:
            errors.append("loudness measurement is implausible")
    return FinalQCResult(not errors, probe.duration_seconds, digest, tuple(errors), tuple((manifest.get("validation") or {}).get("warnings", [])))


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


def _decode_ok(path: Path, ffmpeg_path: str) -> bool:
    command = [str(ffmpeg_path), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-map", "0", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


__all__ = ["FinalQCResult", "sha256_file", "validate_final_output"]
