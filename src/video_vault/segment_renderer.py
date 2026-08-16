"""Frame-accurate, normalized rendering of one Render Manifest segment."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from .audio_pipeline import atempo_filter, build_audio_filter, build_silence_filter, normalize_audio_role
from .color_pipeline import build_color_filter
from .encoder_contract import encoder_arguments, validate_encoder_contract
from .media_probe import MediaProbe, SourceProbeRegistry, probe_media
from .render_errors import SegmentRenderError, is_encoder_fallback_error
from .render_job_models import RenderCancelled
from .render_profiles import get_render_profile
from .source_fingerprint import resolve_source_fingerprint
from .segment_cache import (
    build_segment_cache_key,
    cache_key_payload,
    cache_paths,
    encoder_cache_identity,
    publish_cache_atomically,
    read_cache_metadata,
    write_cache_metadata_temp,
)


@dataclass(frozen=True)
class SegmentQCResult:
    passed: bool
    duration_seconds: float
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SegmentRenderResult:
    segment_id: str
    output_path: Path
    cache_key: str
    cache_hit: bool
    encoder_requested: str
    encoder_used: str
    duration_seconds: float
    warnings: tuple[str, ...] = ()


def render_segment(
    cfg: dict,
    manifest: dict,
    segment: dict,
    *,
    cache_root: Path | None = None,
    runner: Callable[..., Any] | None = None,
    source_probe: MediaProbe | None = None,
    source_probe_registry: SourceProbeRegistry | None = None,
) -> SegmentRenderResult:
    source = Path(str(segment.get("source_file") or "")).expanduser().resolve()
    if not source.is_file():
        raise SegmentRenderError(f"source file does not exist: {source}")
    profile_id = str((manifest.get("profile") or {}).get("profile_id") or "")
    profile = get_render_profile(profile_id)
    settings = dict(manifest.get("settings") or {})
    requested = str(settings.get("encoder") or "auto")
    encoder_identity = encoder_cache_identity(settings)
    contract = settings.get("encoder_contract")
    if isinstance(contract, Mapping):
        validate_encoder_contract(contract, profile)
        encoder = str(contract["implementation"])
    else:
        encoder = map_encoder(requested)
    audio = _effective_segment_audio(manifest, segment)
    normalize_audio_role(audio["role"])
    start = float(segment.get("source_in_seconds"))
    end = float(segment.get("source_out_seconds"))
    speed = float(segment.get("speed"))
    if not all(math.isfinite(value) for value in (start, end, speed)):
        raise SegmentRenderError("segment range and speed must be finite numbers")
    if start < 0 or end <= start:
        raise SegmentRenderError(f"invalid segment range: {start} -> {end}")
    if not 0.25 <= speed <= 4.0:
        raise SegmentRenderError("speed must be between 0.25 and 4.0")
    expected = float(segment.get("timeline_duration_seconds") or ((end - start) / speed))
    root = cache_root or Path(str(cfg.get("library_root") or ".")) / "08_projects" / f"project_{manifest.get('project_id')}" / "cache" / "segments"
    root.mkdir(parents=True, exist_ok=True)
    source_fingerprint = (
        source_probe_registry.fingerprint(source)
        if source_probe_registry is not None
        else resolve_source_fingerprint(source)
    )
    key = build_segment_cache_key(manifest, segment, source_fingerprint=source_fingerprint)
    paths = cache_paths(root, key)
    payload = cache_key_payload(manifest, segment, source_fingerprint=source_fingerprint)
    if _valid_cache(paths, payload, profile, expected, str(cfg.get("ffprobe_path") or "ffprobe")):
        metadata = read_cache_metadata(paths["metadata"]) or {}
        used = str(metadata.get("encoder_used") or encoder)
        return SegmentRenderResult(str(segment["segment_id"]), paths["output"], key, True, requested, used, expected, tuple(metadata.get("warnings") or []))
    _remove_invalid_cache(paths)

    warnings: list[str] = []
    attempts: list[dict[str, Any]] = []
    command: list[str] | None = None
    used = encoder
    qc_errors: tuple[str, ...] = ()
    try:
        probe = source_probe or (
            source_probe_registry.probe(source)
            if source_probe_registry is not None
            else probe_media(str(cfg.get("ffprobe_path") or "ffprobe"), source, "fast")
        )
        if probe.duration_seconds > 0 and end > probe.duration_seconds + 0.001:
            raise SegmentRenderError(f"segment end {end} exceeds source duration {probe.duration_seconds}")
        command = build_segment_ffmpeg_command(cfg, manifest, segment, probe, output=paths["partial"], encoder=encoder)
        result, used = _run_with_fallback(command, encoder, requested, runner, warnings, attempts, expected, allow_fallback=not isinstance(contract, Mapping))
        qc = validate_segment_output(paths["partial"], profile, expected, str(cfg.get("ffprobe_path") or "ffprobe"))
        qc_errors = qc.errors
        if not qc.passed:
            raise SegmentRenderError("segment QC failed: " + "; ".join(qc.errors))
        write_cache_metadata_temp(
            paths["metadata"],
            key,
            payload,
            encoder_requested=requested,
            encoder_used=used,
            encoder_contract_binding=encoder_identity["binding"],
            encoder_contract_version=encoder_identity.get("version", ""),
            encoder_contract_hash=encoder_identity.get("hash", ""),
            encoder_contract_implementation=encoder_identity.get("implementation", ""),
            duration_seconds=qc.duration_seconds,
            warnings=warnings,
        )
        publish_cache_atomically(paths["partial"], paths["output"], paths["metadata_temp"], paths["metadata"])
    except RenderCancelled:
        _cleanup_cache(paths)
        _write_render_log(paths["log"], str(segment.get("segment_id") or ""), key, requested, used, command, attempts, RenderCancelled("render cancellation requested"), qc_errors)
        raise
    except Exception as exc:
        _cleanup_cache(paths)
        _write_render_log(paths["log"], str(segment.get("segment_id") or ""), key, requested, used, command, attempts, exc, qc_errors)
        if isinstance(exc, SegmentRenderError):
            raise
        raise SegmentRenderError(str(exc)) from exc
    _write_render_log(paths["log"], str(segment.get("segment_id") or ""), key, requested, used, command, attempts, None, ())
    return SegmentRenderResult(str(segment["segment_id"]), paths["output"], key, False, requested, used, qc.duration_seconds, tuple(warnings))


def build_segment_ffmpeg_command(
    cfg: Mapping[str, Any],
    manifest: Mapping[str, Any],
    segment: Mapping[str, Any],
    probe: MediaProbe,
    *,
    output: str | Path,
    encoder: str | None = None,
) -> list[str]:
    profile = get_render_profile(str((manifest.get("profile") or {}).get("profile_id")))
    settings = dict(manifest.get("settings") or {})
    contract = settings.get("encoder_contract")
    start = float(segment["source_in_seconds"])
    end = float(segment["source_out_seconds"])
    speed = float(segment["speed"])
    duration = end - start
    timeline = duration / speed
    color = build_color_filter(dict(segment.get("color") or settings.get("color") or {}))
    audio = _effective_segment_audio(manifest, segment)
    normalize_audio_role(audio["role"])
    video_filters = [f"trim=start={start:.6f}:end={end:.6f}", "setpts=PTS-STARTPTS", f"setpts=PTS/{speed:g}"]
    if color:
        video_filters.append(color)
    video_filters.extend([f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease", f"pad={profile['width']}:{profile['height']}:(ow-iw)/2:(oh-ih)/2", f"fps={profile['fps']}", f"format={profile['pixel_format']}"])
    graph = [f"[0:v]{','.join(video_filters)}[vout]"]
    args = [str(cfg.get("ffmpeg_path") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(segment["source_file"])]
    if probe.has_audio:
        audio_filter = build_audio_filter(audio["role"], speed, settings, start=start, end=end, audio_settings=audio)
        graph.append(f"[0:a]{audio_filter}[aout]")
    else:
        args.extend(["-f", "lavfi", "-t", f"{timeline:.6f}", "-i", build_silence_filter(timeline)])
        graph.append(f"[1:a]atrim=duration={timeline:.6f},asetpts=PTS-STARTPTS[aout]")
    video_args = ["-c:v", encoder or map_encoder(str(settings.get("encoder") or "auto"))]
    if isinstance(contract, Mapping):
        validate_encoder_contract(contract, profile)
        video_args = encoder_arguments(contract)
    args.extend([
        "-filter_complex", ";".join(graph),
        "-map", "[vout]", "-map", "[aout]",
        *video_args,
        "-r", str(profile["fps"]), "-pix_fmt", str(profile["pixel_format"]),
        "-c:a", str(profile["audio_codec"]), "-ar", str(profile["audio_sample_rate"]), "-ac", str(profile["audio_channels"]),
        "-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"),
        "-colorspace", str(profile.get("color_matrix") or "bt709"), "-color_range", str(profile.get("color_range") or "tv"),
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ])
    return args


def _effective_segment_audio(manifest: Mapping[str, Any], segment: Mapping[str, Any]) -> dict[str, Any]:
    configured = segment.get("audio")
    if isinstance(configured, Mapping):
        result = dict(configured)
    else:
        result = {"role": segment.get("audio_role") or "keep_original"}
    if not result.get("role"):
        result["role"] = ((manifest.get("settings") or {}).get("audio") or {}).get("original_audio", {}).get("default_role", "lower")
    return result


def validate_segment_output(output_path: str | Path, profile: Mapping[str, Any], expected_duration_seconds: float, ffprobe_path: str = "ffprobe") -> SegmentQCResult:
    path = Path(output_path)
    if not path.is_file() or path.stat().st_size <= 0:
        return SegmentQCResult(False, 0.0, ("output is missing or empty",))
    try:
        probe = probe_media(ffprobe_path, path, "fast")
    except Exception as exc:
        return SegmentQCResult(False, 0.0, (str(exc),))
    errors: list[str] = []
    if not probe.has_video:
        errors.append("output has no video stream")
    if not probe.has_audio:
        errors.append("output has no audio stream")
    if (probe.width, probe.height) != (int(profile["width"]), int(profile["height"])):
        errors.append(f"resolution mismatch: {probe.width}x{probe.height}")
    if abs(probe.fps - float(profile["fps"])) > 0.01:
        errors.append(f"fps mismatch: {probe.fps}")
    if probe.pixel_format != str(profile["pixel_format"]):
        errors.append(f"pixel format mismatch: {probe.pixel_format}")
    if probe.sample_rate != int(profile["audio_sample_rate"]):
        errors.append(f"sample rate mismatch: {probe.sample_rate}")
    if probe.channels != int(profile["audio_channels"]):
        errors.append(f"channel mismatch: {probe.channels}")
    tolerance = max(0.10, 2 / float(profile["fps"]))
    if abs(probe.duration_seconds - expected_duration_seconds) > tolerance:
        errors.append(f"duration mismatch: {probe.duration_seconds:.3f} vs {expected_duration_seconds:.3f}")
    return SegmentQCResult(not errors, probe.duration_seconds, tuple(errors))


def map_encoder(requested: str) -> str:
    value = str(requested or "auto")
    if value == "auto":
        return "h264_nvenc"
    if value == "cpu":
        return "libx264"
    if value in {"libx264", "h264_nvenc"}:
        return value
    raise SegmentRenderError(f"unsupported encoder: {value}")


def _run_with_fallback(
    command: list[str],
    encoder: str,
    requested: str,
    runner: Callable[..., Any] | None,
    warnings: list[str],
    attempts: list[dict[str, Any]],
    expected_duration_seconds: float,
    *,
    allow_fallback: bool = True,
) -> tuple[Any, str]:
    result = _run_and_record(command, encoder, runner, attempts, expected_duration_seconds)
    if _returncode(result) == 0:
        return result, encoder
    stderr = str(getattr(result, "stderr", "") or "")
    if allow_fallback and encoder != "libx264" and is_encoder_fallback_error(stderr):
        warnings.append(f"encoder fallback: {encoder} -> libx264: {stderr.strip()[-300:]}")
        fallback_command = list(command)
        index = fallback_command.index("-c:v") + 1
        fallback_command[index] = "libx264"
        fallback_result = _run_and_record(fallback_command, "libx264", runner, attempts, expected_duration_seconds)
        if _returncode(fallback_result) == 0:
            return fallback_result, "libx264"
        raise SegmentRenderError(f"FFmpeg fallback failed: {getattr(fallback_result, 'stderr', '')}")
    raise SegmentRenderError(f"FFmpeg failed: {stderr[-1000:]}")


def _run_and_record(command: list[str], encoder: str, runner: Callable[..., Any] | None, attempts: list[dict[str, Any]], expected_duration_seconds: float | None = None) -> Any:
    try:
        result = _run(command, runner, expected_duration_seconds=expected_duration_seconds)
    except Exception as exc:
        attempts.append({"encoder": encoder, "command": list(command), "returncode": "exception", "stderr": str(exc)})
        raise
    attempts.append({"encoder": encoder, "command": list(command), "returncode": _returncode(result), "stderr": str(getattr(result, "stderr", "") or "")})
    return result


def _run(command: list[str], runner: Callable[..., Any] | None, *, expected_duration_seconds: float | None = None) -> Any:
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


def _returncode(result: Any) -> int:
    return int(getattr(result, "returncode", 0) or 0)


def _valid_cache(paths: dict[str, Path], payload: Mapping[str, Any], profile: Mapping[str, Any], expected: float, ffprobe_path: str) -> bool:
    metadata = read_cache_metadata(paths["metadata"])
    if not paths["output"].is_file() or paths["output"].stat().st_size <= 0 or not metadata:
        return False
    if metadata.get("cache_key") != paths["output"].stem or metadata.get("key_payload") != dict(payload):
        return False
    identity = payload.get("encoder_cache_identity")
    if not isinstance(identity, Mapping):
        return False
    if metadata.get("encoder_contract_binding") != identity.get("binding"):
        return False
    if identity.get("binding") == "resolved_contract":
        if any(
            metadata.get(metadata_key) != identity.get(identity_key)
            for metadata_key, identity_key in (
                ("encoder_contract_version", "version"),
                ("encoder_contract_hash", "hash"),
                ("encoder_contract_implementation", "implementation"),
            )
        ):
            return False
        if metadata.get("encoder_used") != identity.get("implementation"):
            return False
    elif metadata.get("encoder_requested") != identity.get("requested"):
        return False
    return validate_segment_output(paths["output"], profile, expected, ffprobe_path).passed


def _cleanup_partial(paths: dict[str, Path]) -> None:
    for key in ("partial", "metadata_temp"):
        try:
            paths[key].unlink(missing_ok=True)
        except OSError:
            pass


def _remove_invalid_cache(paths: dict[str, Path]) -> None:
    for key in ("output", "metadata", "partial", "metadata_temp"):
        try:
            paths[key].unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_cache(paths: dict[str, Path]) -> None:
    _remove_invalid_cache(paths)


def _write_render_log(
    path: Path,
    segment_id: str,
    cache_key: str,
    requested: str,
    used: str,
    command: list[str] | None,
    attempts: list[dict[str, Any]],
    error: Exception | None,
    qc_errors: tuple[str, ...],
) -> None:
    lines = [
        f"segment_id: {segment_id}",
        f"cache_key: {cache_key}",
        f"encoder_requested: {requested}",
        f"encoder_used_or_attempted: {used}",
    ]
    if command and not attempts:
        lines.append(f"command: {subprocess.list2cmdline(command)}")
    for index, attempt in enumerate(attempts, 1):
        lines.extend(
            [
                f"attempt_{index}_encoder: {attempt.get('encoder', '')}",
                f"attempt_{index}_return_code: {attempt.get('returncode', '')}",
                f"attempt_{index}_command: {subprocess.list2cmdline(attempt.get('command') or [])}",
                f"attempt_{index}_stderr:\n{attempt.get('stderr', '')}",
            ]
        )
    if qc_errors:
        lines.append("qc_errors:\n" + "\n".join(qc_errors))
    if error:
        lines.append(f"error: {error}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


__all__ = ["SegmentQCResult", "SegmentRenderResult", "build_segment_ffmpeg_command", "map_encoder", "render_segment", "validate_segment_output"]
