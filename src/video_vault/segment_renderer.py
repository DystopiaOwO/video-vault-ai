"""Frame-accurate, normalized rendering of one Render Manifest segment."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import math
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping

from .audio_pipeline import atempo_filter, build_audio_filter, build_silence_filter, normalize_audio_role
from .color_pipeline import build_color_filter
from .encoder_contract import encoder_arguments, validate_encoder_contract
from .gpu_execution import GPUExecutionRegistry, apply_visual_execution_contract
from .visual_style import resolve_visual_render_plan
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
    gpu_execution_contract: dict[str, Any] | None = None
    elapsed_seconds: float = 0.0
    visual_render_plan: dict[str, Any] | None = None


def render_segment(
    cfg: dict,
    manifest: dict,
    segment: dict,
    *,
    cache_root: Path | None = None,
    runner: Callable[..., Any] | None = None,
    source_probe: MediaProbe | None = None,
    source_probe_registry: SourceProbeRegistry | None = None,
    gpu_execution_registry: GPUExecutionRegistry | None = None,
    visual_style_snapshot: Mapping[str, Any] | None = None,
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
    probe = source_probe or (
        source_probe_registry.probe(source)
        if source_probe_registry is not None
        else probe_media(str(cfg.get("ffprobe_path") or "ffprobe"), source, "fast")
    )
    gpu_registry = gpu_execution_registry or GPUExecutionRegistry(cfg)
    gpu_contract = gpu_registry.resolve(manifest, segment, probe, contract if isinstance(contract, Mapping) else None)
    effective_manifest = deepcopy(manifest)
    effective_manifest.setdefault("settings", {})["gpu_execution_contract"] = dict(gpu_contract)
    visual_plan = _resolve_visual_plan(visual_style_snapshot, profile, segment, settings, probe)
    gpu_contract = apply_visual_execution_contract(gpu_contract, visual_plan, encoder=encoder)
    effective_manifest.setdefault("settings", {})["gpu_execution_contract"] = dict(gpu_contract)
    if visual_plan:
        effective_manifest["visual_render_plan_hash"] = str(visual_plan.get("resolved_hash") or "")
    settings = dict(effective_manifest.get("settings") or {})
    contract = settings.get("encoder_contract")
    root = cache_root or Path(str(cfg.get("library_root") or ".")) / "08_projects" / f"project_{manifest.get('project_id')}" / "cache" / "segments"
    root.mkdir(parents=True, exist_ok=True)
    source_fingerprint = (
        source_probe_registry.fingerprint(source)
        if source_probe_registry is not None
        else resolve_source_fingerprint(source)
    )
    key = build_segment_cache_key(effective_manifest, segment, source_fingerprint=source_fingerprint)
    paths = cache_paths(root, key)
    payload = cache_key_payload(effective_manifest, segment, source_fingerprint=source_fingerprint)
    if _valid_cache(paths, payload, profile, expected, str(cfg.get("ffprobe_path") or "ffprobe")):
        metadata = read_cache_metadata(paths["metadata"]) or {}
        used = str(metadata.get("encoder_used") or encoder)
        return SegmentRenderResult(
            str(segment["segment_id"]),
            paths["output"],
            key,
            True,
            requested,
            used,
            expected,
            tuple(metadata.get("warnings") or []),
            dict(metadata.get("gpu_execution_contract") or gpu_contract),
            float(metadata.get("elapsed_seconds") or 0.0),
            dict(metadata.get("visual_render_plan") or visual_plan or {}) or None,
        )
    _remove_invalid_cache(paths)

    warnings: list[str] = []
    attempts: list[dict[str, Any]] = []
    command: list[str] | None = None
    used = encoder
    qc_errors: tuple[str, ...] = ()
    started = time.perf_counter()
    try:
        if probe.duration_seconds > 0 and end > probe.duration_seconds + 0.001:
            raise SegmentRenderError(f"segment end {end} exceeds source duration {probe.duration_seconds}")
        command = build_segment_ffmpeg_command(cfg, effective_manifest, segment, probe, output=paths["partial"], encoder=encoder, visual_render_plan=visual_plan)
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
            gpu_execution_contract=dict(gpu_contract),
            gpu_execution_contract_version=str(gpu_contract.get("version") or ""),
            gpu_execution_contract_hash=str(gpu_contract.get("contract_hash") or ""),
            gpu_execution_decode_used=str(gpu_contract.get("decode_used") or "cpu"),
            gpu_execution_filter_used=str(gpu_contract.get("filter_used") or "cpu"),
            gpu_execution_hardware_api=str(gpu_contract.get("hardware_api") or "cpu"),
            gpu_execution_filter_chain=list(gpu_contract.get("filter_chain") or []),
            gpu_execution_fallback_reason=str(gpu_contract.get("fallback_reason") or ""),
            duration_seconds=qc.duration_seconds,
            elapsed_seconds=round(time.perf_counter() - started, 6),
            warnings=warnings,
            visual_render_plan=visual_plan,
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
    return SegmentRenderResult(
        str(segment["segment_id"]), paths["output"], key, False, requested, used, qc.duration_seconds,
        tuple(warnings), dict(gpu_contract), round(time.perf_counter() - started, 6),
        visual_plan,
    )


def build_segment_ffmpeg_command(
    cfg: Mapping[str, Any],
    manifest: Mapping[str, Any],
    segment: Mapping[str, Any],
    probe: MediaProbe,
    *,
    output: str | Path,
    encoder: str | None = None,
    visual_render_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    profile = get_render_profile(str((manifest.get("profile") or {}).get("profile_id")))
    settings = dict(manifest.get("settings") or {})
    contract = settings.get("encoder_contract")
    gpu_contract = settings.get("gpu_execution_contract") if isinstance(settings.get("gpu_execution_contract"), Mapping) else {}
    start = float(segment["source_in_seconds"])
    end = float(segment["source_out_seconds"])
    speed = float(segment["speed"])
    duration = end - start
    timeline = duration / speed
    color = "" if visual_render_plan else build_color_filter(dict(segment.get("color") or settings.get("color") or {}))
    visual_filter = str((visual_render_plan or {}).get("color_filter") or "")
    visual_title_filter = str(((visual_render_plan or {}).get("title") or {}).get("filter") or "")
    audio = _effective_segment_audio(manifest, segment)
    normalize_audio_role(audio["role"])
    video_filters = [f"trim=start={start:.6f}:end={end:.6f}", "setpts=PTS-STARTPTS", f"setpts=PTS/{speed:g}"]
    gpu_path = str(gpu_contract.get("decode_used") or "") == "nvdec" and str(gpu_contract.get("hardware_api") or "") == "cuda"
    if gpu_path and visual_render_plan:
        # The effective contract is nvdec_cpu_visual_nvenc, so the approved
        # visual graph is explicitly evaluated after the CUDA->CPU boundary.
        video_filters.extend(["hwdownload", "format=yuv420p"])
    elif gpu_path:
        video_filters.append(f"scale_cuda={profile['width']}:{profile['height']}:format={profile['pixel_format']}")
        video_filters.append(
            "setparams="
            f"colorspace={str(profile.get('color_matrix') or 'bt709')}:"
            f"color_primaries={str(profile.get('color_primaries') or 'bt709')}:"
            f"color_trc={str(profile.get('color_transfer') or 'bt709')}:"
            f"range={'limited' if str(profile.get('color_range') or 'tv') == 'tv' else 'full'}"
        )
        if color:
            video_filters.extend(["hwdownload", "format=yuv420p", color])
    else:
        if color:
            video_filters.append(color)
        geometry = gpu_contract.get("display_geometry") if isinstance(gpu_contract.get("display_geometry"), Mapping) else {}
        policy = str(geometry.get("composition_policy") or settings.get("display_geometry_policy") or "preserve_aspect_pad")
        geometry_normalization = _display_geometry_normalization_filter(probe)
        if visual_render_plan:
            geometry_filters = [] if str(visual_render_plan.get("graph_type") or "linear") == "split_background_overlay" else [str(visual_render_plan.get("filter_graph") or "")]
        elif policy == "crop_to_fill":
            geometry_filters = [
                geometry_normalization,
                f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=increase",
                f"crop={profile['width']}:{profile['height']}:(iw-ow)/2:(ih-oh)/2",
            ]
        else:
            background = str(settings.get("display_background_color") or "black") if policy == "background" else "black"
            geometry_filters = [
                geometry_normalization,
                f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease",
                f"pad={profile['width']}:{profile['height']}:(ow-iw)/2:(oh-ih)/2:color={background}",
            ]
        video_filters.extend([*geometry_filters, f"fps={profile['fps']}", f"format={profile['pixel_format']}", "setsar=1"])
        if visual_filter and not visual_render_plan:
            video_filters.append(visual_filter)
        if visual_title_filter and not visual_render_plan:
            video_filters.append(visual_title_filter)
        video_filters.append(
            "setparams="
            f"colorspace={str(profile.get('color_matrix') or 'bt709')}:"
            f"color_primaries={str(profile.get('color_primaries') or 'bt709')}:"
            f"color_trc={str(profile.get('color_transfer') or 'bt709')}:"
            f"range={'limited' if str(profile.get('color_range') or 'tv') == 'tv' else 'full'}"
        )
    graph: list[str] = []
    if visual_render_plan and str(visual_render_plan.get("graph_type") or "linear") == "split_background_overlay":
        graph.append(f"[0:v]{','.join(video_filters)}[visual_in]")
        # Background graph already includes its color/title tail so it is not
        # accidentally applied twice at this boundary.
        suffix = ""
        visual_graph = str(visual_render_plan.get("filter_complex") or "").replace("[0:v]", "[visual_in]", 1)
        if suffix:
            visual_graph = visual_graph + f",{suffix}"
        if gpu_path:
            visual_graph += ",".join(["", f"fps={profile['fps']}", f"format={profile['pixel_format']}", "setsar=1", "setparams=" f"colorspace={str(profile.get('color_matrix') or 'bt709')}:color_primaries={str(profile.get('color_primaries') or 'bt709')}:color_trc={str(profile.get('color_transfer') or 'bt709')}:range={'limited' if str(profile.get('color_range') or 'tv') == 'tv' else 'full'}"])
        graph.append(visual_graph + "[vout]")
    else:
        if visual_render_plan and gpu_path:
            # GPU mixed path needs the same linear visual plan as Preview.
            video_filters.append(str(visual_render_plan.get("filter_graph") or ""))
            video_filters.extend([
                f"fps={profile['fps']}",
                f"format={profile['pixel_format']}",
                "setsar=1",
                "setparams="
                f"colorspace={str(profile.get('color_matrix') or 'bt709')}:"
                f"color_primaries={str(profile.get('color_primaries') or 'bt709')}:"
                f"color_trc={str(profile.get('color_transfer') or 'bt709')}:"
                f"range={'limited' if str(profile.get('color_range') or 'tv') == 'tv' else 'full'}",
            ])
        graph.append(f"[0:v]{','.join(video_filters)}[vout]")
    args = [str(cfg.get("ffmpeg_path") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if gpu_path:
        args.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
    else:
        # FFmpeg's CPU path enables autorotate by default.  Do not add a
        # spelling-dependent boolean CLI flag here: FFmpeg 6 accepts the
        # option differently from newer builds and can then fail to register
        # the input before parsing the filter graph.  The display-matrix
        # contract is already enforced by MediaProbe and the CPU-only
        # resolver path; the default autorotate behavior is the portable part.
        pass
    args.extend(["-i", str(segment["source_file"])])
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
    output_video_args = ["-r", str(profile["fps"])]
    if gpu_path:
        # CUDA frames do not use the CPU ``fps`` filter.  Bound the output
        # duration explicitly so CFR muxing cannot retain an extra source
        # frame when the input cadence is not an exact multiple of the target.
        output_video_args.extend(["-t", f"{timeline:.6f}"])
    # ``scale_cuda`` already establishes the CUDA yuv420p format consumed by
    # h264_nvenc.  Repeating a software ``-pix_fmt yuv420p`` output constraint
    # makes FFmpeg insert an invalid auto_scale transfer on this path.  CPU
    # and mixed paths retain the explicit output pixel-format contract.
    if not gpu_path:
        output_video_args.extend(["-pix_fmt", str(profile["pixel_format"])])
    args.extend([
        "-filter_complex", ";".join(graph),
        "-map", "[vout]", "-map", "[aout]",
        *video_args,
        *output_video_args,
        "-c:a", str(profile["audio_codec"]), "-ar", str(profile["audio_sample_rate"]), "-ac", str(profile["audio_channels"]),
        "-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"),
        *([] if gpu_path else ["-colorspace", str(profile.get("color_matrix") or "bt709")]),
        "-color_range", str(profile.get("color_range") or "tv"),
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ])
    return args


def _resolve_visual_plan(snapshot: Mapping[str, Any] | None, profile: Mapping[str, Any], segment: Mapping[str, Any], settings: Mapping[str, Any], probe: MediaProbe | None = None) -> dict[str, Any] | None:
    if not isinstance(snapshot, Mapping):
        return None
    color = segment.get("color") if isinstance(segment.get("color"), Mapping) else settings.get("color")
    return resolve_visual_render_plan(
        snapshot,
        width=int(profile["width"]),
        height=int(profile["height"]),
        title_text=str(segment.get("title_text") or segment.get("title") or ""),
        color_settings=dict(color or {}),
        source_display_ratio=float(probe.display_ratio or 0.0) if probe is not None else None,
        source_geometry=_probe_geometry(probe),
        title_role=str(segment.get("title_role") or "chapter_title"),
        title_duration_seconds=float(segment.get("timeline_duration_seconds") or 0.0) or None,
    )


def _probe_geometry(probe: MediaProbe | None) -> dict[str, Any]:
    if probe is None:
        return {}
    return {
        "coded_width": int(probe.coded_width or probe.width),
        "coded_height": int(probe.coded_height or probe.height),
        "sample_aspect_ratio": str(probe.sample_aspect_ratio or "1:1"),
        "display_aspect_ratio": str(probe.display_aspect_ratio or ""),
        "display_ratio": float(probe.display_ratio or 0.0),
        "display_width": int(probe.display_width or probe.width),
        "display_height": int(probe.display_height or probe.height),
        "rotation_degrees": int(probe.rotation_degrees or 0),
        "display_matrix": str(probe.display_matrix or ""),
        "source_orientation": "portrait" if float(probe.display_ratio or 0.0) < 1 else "landscape" if float(probe.display_ratio or 0.0) else "unknown",
        "provenance": str(probe.display_geometry_source or "unknown"),
    }


def _display_geometry_normalization_filter(probe: MediaProbe) -> str:
    """Convert source sample aspect ratio into square-pixel display geometry.

    FFmpeg's ``setsar=1`` is only safe after the source's display geometry has
    been represented in pixel dimensions.  Expanding the coded width by SAR
    first keeps scale/crop/pad decisions based on displayed pixels rather than
    the coded raster.  The final ``setsar=1`` is part of the returned filter.
    """

    raw_sar = str(probe.sample_aspect_ratio or "1:1").strip().replace("/", ":")
    try:
        numerator_text, denominator_text = raw_sar.split(":", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (TypeError, ValueError):
        numerator, denominator = 1, 1
    if numerator <= 0 or denominator <= 0:
        numerator, denominator = 1, 1
    # FFmpeg's default autorotate inserts the display transform before this
    # user filter graph.  A quarter-turn swaps the pixel axes and FFmpeg
    # correspondingly exposes the reciprocal SAR on the rotated frame.
    # Normalize that post-autorotate SAR, rather than applying the original
    # probe value to the swapped dimensions.
    if abs(int(probe.rotation_degrees or 0)) % 180 == 90:
        numerator, denominator = denominator, numerator
    if numerator == denominator:
        return "setsar=1"
    return f"scale=ceil(iw*{numerator}/{denominator}/2)*2:ih:eval=init,setsar=1"


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
    gpu_identity = payload.get("gpu_execution_identity")
    if not isinstance(gpu_identity, Mapping):
        return False
    if metadata.get("gpu_execution_contract_version") != gpu_identity.get("version"):
        return False
    if metadata.get("gpu_execution_contract_hash") != gpu_identity.get("hash"):
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
