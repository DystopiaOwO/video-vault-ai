"""Synchronous approved-project renderer for Phase 4A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from .bgm_pipeline import BgmPipelineError, bgm_fingerprint, build_bgm_mix_command, validate_bgm_track
from .final_qc import FinalQCResult, sha256_file, validate_final_output
from .project import can_project_render, project_dir
from .render_manifest import manifest_hash, validate_render_manifest
from .segment_cache import build_segment_cache_key, cache_paths
from .segment_renderer import SegmentRenderResult, render_segment
from .timeline_assembler import TimelineAssemblyError, build_concat_file, build_timeline_command, run_command


class ProjectRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectRenderResult:
    project_id: int
    output_path: Path
    manifest_hash: str
    cache_hit: bool
    duration_seconds: float
    segment_results: tuple[SegmentRenderResult, ...]
    bgm_used: bool
    warnings: tuple[str, ...] = ()


def render_project(
    cfg: dict,
    db: Path,
    project_id: int,
    *,
    output_path: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> ProjectRenderResult:
    allowed, reason = can_project_render(cfg, db, project_id)
    if not allowed:
        raise PermissionError(f"project render 被擋下：{reason}")
    folder = project_dir(cfg, project_id)
    manifest_path = folder / "render_manifest.json"
    manifest = _read_json(manifest_path)
    review = _read_json(folder / "review_status.json")
    approved_hash = review.get("approved_manifest_hash")
    if not approved_hash or manifest.get("manifest_hash") != approved_hash or manifest_hash(manifest) != approved_hash:
        raise PermissionError("project render 被擋下：核准 Manifest snapshot hash 不一致")
    validation = validate_render_manifest(manifest)
    if validation["errors"]:
        raise ProjectRenderError("approved render manifest is invalid: " + "; ".join(validation["errors"]))
    _validate_phase4a_settings(manifest)
    segments = sorted(manifest["segments"], key=lambda item: int(item["order"]))
    tracks = list(manifest.get("bgm") or [])
    if len(tracks) > 1:
        raise ProjectRenderError("multiple BGM scheduling is not supported in Phase 4A")
    track = tracks[0] if tracks else None
    ffmpeg_path = str(cfg.get("ffmpeg_path") or "ffmpeg")
    ffprobe_path = str(cfg.get("ffprobe_path") or "ffprobe")
    if track is not None:
        try:
            validate_bgm_track(track, ffprobe_path)
            bgm_fp = bgm_fingerprint(track)
        except BgmPipelineError as exc:
            raise ProjectRenderError(str(exc)) from exc
    else:
        bgm_fp = None
    renders = folder / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    profile_id = str((manifest.get("profile") or {}).get("profile_id") or "unknown")
    output = Path(output_path).expanduser().resolve() if output_path else renders / f"project_{project_id}_{approved_hash[:12]}_{profile_id}.mp4"
    output = output.resolve()
    report_path = output.with_name(output.name + ".render.json")
    partial = output.with_name(output.name[:-4] + ".partial.mp4") if output.suffix.lower() == ".mp4" else output.with_name(output.name + ".partial.mp4")
    report_temp = report_path.with_name(f".{report_path.name}.tmp")
    log_path = renders / "logs" / f"{approved_hash}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    expected = sum(float(item["timeline_duration_seconds"]) for item in segments)
    if _final_cache_valid(output, report_path, manifest, approved_hash, profile_id, bgm_fp, ffprobe_path):
        report = _read_json(report_path)
        cache_root = folder / "cache" / "segments"
        cached_results = tuple(
            SegmentRenderResult(
                str(item["segment_id"]),
                cache_paths(cache_root, str(item["cache_key"]))["output"],
                str(item["cache_key"]),
                True,
                str(item.get("encoder_requested") or manifest.get("settings", {}).get("encoder", "auto")),
                str(item.get("encoder_used") or ""),
                float(item.get("duration_seconds") or 0),
                tuple(item.get("warnings") or []),
            )
            for item in report.get("segments", [])
        )
        return ProjectRenderResult(project_id, output, approved_hash, True, float(report.get("duration_seconds") or 0), cached_results, bool((report.get("bgm") or {}).get("used")), tuple((report.get("qc") or {}).get("warnings") or []))

    work_dir = folder / "work" / approved_hash
    work_dir.mkdir(parents=True, exist_ok=True)
    segment_results: list[SegmentRenderResult] = []
    command: list[str] | None = None
    concat_path = work_dir / "timeline.ffconcat"
    try:
        for segment in segments:
            segment_results.append(render_segment(cfg, manifest, segment, runner=runner))
        concat_path = build_concat_file([item.output_path for item in segment_results], concat_path)
        if track is None:
            command = build_timeline_command(ffmpeg_path, concat_path, partial, duration_seconds=expected)
        else:
            command = build_bgm_mix_command(ffmpeg_path, concat_path, partial, track, expected, manifest["profile"])
        result = run_command(command, runner)
        if int(getattr(result, "returncode", 0) or 0) != 0:
            raise ProjectRenderError(str(getattr(result, "stderr", "") or "FFmpeg project render failed"))
        qc = validate_final_output(partial, manifest, ffprobe_path)
        if not qc.passed:
            raise ProjectRenderError("final QC failed: " + "; ".join(qc.errors))
        report = build_render_report(project_id, manifest, output, qc, segment_results, track, bgm_fp, output_size=partial.stat().st_size)
        report_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        partial.replace(output)
        try:
            report_temp.replace(report_path)
        except OSError:
            output.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            report_temp.unlink(missing_ok=True)
            raise
        _write_log(log_path, project_id, approved_hash, segment_results, concat_path, command, result, qc, track, None)
        concat_path.unlink(missing_ok=True)
        return ProjectRenderResult(project_id, output, approved_hash, False, qc.duration_seconds, tuple(segment_results), track is not None, tuple(qc.warnings))
    except Exception as exc:
        output.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
        report_temp.unlink(missing_ok=True)
        _write_log(log_path, project_id, approved_hash, segment_results, concat_path, command, locals().get("result"), locals().get("qc"), track, exc)
        if isinstance(exc, (ProjectRenderError, PermissionError)):
            raise
        if isinstance(exc, (TimelineAssemblyError, BgmPipelineError)):
            raise ProjectRenderError(str(exc)) from exc
        raise


def build_render_report(
    project_id: int,
    manifest: Mapping[str, Any],
    output_path: Path,
    qc: FinalQCResult,
    segment_results: list[SegmentRenderResult] | tuple[SegmentRenderResult, ...],
    track: Mapping[str, Any] | None,
    bgm_fp: Mapping[str, Any] | None,
    *,
    output_size: int | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "manifest_hash": manifest["manifest_hash"],
        "profile_id": manifest["profile"]["profile_id"],
        "output_path": str(Path(output_path).resolve()),
        "output_size": int(output_size if output_size is not None else Path(output_path).stat().st_size),
        "output_sha256": qc.output_sha256,
        "duration_seconds": qc.duration_seconds,
        "segment_count": len(segment_results),
        "segments": [
            {
                "segment_id": item.segment_id,
                "cache_key": item.cache_key,
                "cache_hit": item.cache_hit,
                "encoder_requested": item.encoder_requested,
                "encoder_used": item.encoder_used,
                "duration_seconds": item.duration_seconds,
                "warnings": list(item.warnings),
            }
            for item in segment_results
        ],
        "bgm": {"used": bool(track), **dict(track or {}), "fingerprint": dict(bgm_fp or {})},
        "qc": {"passed": qc.passed, "warnings": list(qc.warnings), "errors": list(qc.errors)},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _validate_phase4a_settings(manifest: Mapping[str, Any]) -> None:
    settings = manifest.get("settings") or {}
    transition = settings.get("transition") or {}
    if str(transition.get("type") or "cut") != "cut" or abs(float(transition.get("duration_seconds") or 0)) > 0.000001:
        raise ProjectRenderError("unsupported transition in Phase 4A")
    if bool((settings.get("overlay") or {}).get("enabled", False)):
        raise ProjectRenderError("overlay is not supported in Phase 4A")


def _final_cache_valid(output: Path, report_path: Path, manifest: Mapping[str, Any], manifest_id: str, profile_id: str, bgm_fp: Mapping[str, Any] | None, ffprobe_path: str) -> bool:
    if not output.is_file() or not report_path.is_file():
        return False
    try:
        report = _read_json(report_path)
        if report.get("manifest_hash") != manifest_id or report.get("profile_id") != profile_id:
            return False
        expected_keys = [build_segment_cache_key(manifest, segment) for segment in sorted(manifest["segments"], key=lambda item: int(item["order"]))]
        actual_keys = [item.get("cache_key") for item in report.get("segments", [])]
        if actual_keys != expected_keys:
            return False
        if (report.get("bgm") or {}).get("fingerprint") != dict(bgm_fp or {}):
            return False
        if report.get("output_size") != output.stat().st_size or report.get("output_sha256") != sha256_file(output):
            return False
        qc = validate_final_output(output, manifest, ffprobe_path)
        return qc.passed and bool((report.get("qc") or {}).get("passed"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _write_log(path: Path, project_id: int, manifest_id: str, segment_results: list[SegmentRenderResult], concat_path: Path, command: list[str] | None, result: Any, qc: FinalQCResult | None, track: Mapping[str, Any] | None, error: Exception | None) -> None:
    lines = [f"project_id: {project_id}", f"manifest_hash: {manifest_id}", f"concat_list: {concat_path}"]
    for item in segment_results:
        lines.append(f"segment: {item.segment_id} cache_key={item.cache_key} cache_hit={item.cache_hit}")
    if command:
        lines.append("command: " + subprocess.list2cmdline(command))
    if result is not None:
        lines.append(f"return_code: {getattr(result, 'returncode', '')}")
        lines.append("stderr:\n" + str(getattr(result, "stderr", "") or ""))
    if track:
        lines.append("bgm: " + json.dumps(dict(track), ensure_ascii=False, sort_keys=True))
    if qc:
        lines.append("final_qc_errors:\n" + "\n".join(qc.errors))
    if error:
        lines.append(f"error: {error}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["ProjectRenderError", "ProjectRenderResult", "build_render_report", "render_project"]
