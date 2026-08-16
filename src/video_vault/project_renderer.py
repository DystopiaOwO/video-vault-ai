"""Synchronous approved-project renderer for Phase 4A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess
import hashlib
from typing import Any, Callable, Mapping

from .bgm_pipeline import BgmPipelineError, bgm_fingerprint, build_bgm_mix_command, validate_bgm_track
from .artifact_retention import (
    RetentionError,
    ensure_render_free_space,
)
from .final_qc import FinalQCResult, sha256_file, validate_final_output
from .loudness import LoudnessError, build_second_pass_command, measure_loudness
from .media_probe import SourceProbeRegistry
from .project import can_project_render, project_dir
from .render_manifest import manifest_hash, validate_render_manifest
from .segment_cache import build_segment_cache_key, cache_paths
from .segment_renderer import SegmentRenderResult, render_segment
from .timeline_assembler import TimelineAssemblyError, build_concat_file, build_timeline_command, run_command
from .visual_renderer import (
    VisualRenderError,
    cleanup_visual_filter,
    prepare_visual_filter,
)
from .visual_compositor import VisualCompositionError, apply_lower_thirds, render_visual_cards, resolve_visual_timeline, stable_visual_hash


class ProjectRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalOutputPaths:
    output: Path
    partial: Path
    report: Path
    report_temp: Path
    log: Path
    managed: bool
    cache_hit: bool
    cache_miss_reason: str = ""


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
    execution: Any | None = None,
    approval_snapshot: Mapping[str, Any] | None = None,
    encoder_contract: Mapping[str, Any] | None = None,
) -> ProjectRenderResult:
    folder = project_dir(cfg, project_id)
    snapshot: Mapping[str, Any] | None = approval_snapshot
    if snapshot is None:
        allowed, reason = can_project_render(cfg, db, project_id)
        if not allowed:
            raise PermissionError(f"project render 被擋下：{reason}")
        manifest_path = folder / "render_manifest.json"
        manifest = _read_json(manifest_path)
        review = _read_json(folder / "review_status.json")
        approved_hash = review.get("approved_manifest_hash")
        if not approved_hash or manifest.get("manifest_hash") != approved_hash or manifest_hash(manifest) != approved_hash:
            raise PermissionError("project render 被擋下：核准 Manifest snapshot hash 不一致")
        from .approval_snapshot import load_approval_snapshot

        snapshot_token = str(review.get("approval_snapshot_path") or "")
        snapshot = load_approval_snapshot(folder / snapshot_token) if snapshot_token else None
    else:
        manifest = dict(snapshot.get("manifest") or {})
        approved_hash = str(snapshot.get("manifest_hash") or "")
        if not approved_hash:
            raise PermissionError("project render 被擋下：Render Job 缺少核准 snapshot")
    if snapshot is not None:
        from .approval_snapshot import validate_snapshot

        immutable_validation = validate_snapshot(snapshot, check_assets=True)
        if not immutable_validation["valid"]:
            raise PermissionError("project render 被擋下：approval snapshot 已失效：" + "; ".join(immutable_validation["errors"]))
    if manifest.get("manifest_hash") != approved_hash or manifest_hash(manifest) != approved_hash:
        raise PermissionError("project render 被擋下：immutable snapshot manifest hash 不一致")
    # Encoder choice is a job-scoped runtime contract.  It is deliberately
    # separate from the user-approved manifest hash, but is pinned before any
    # segment cache lookup and carried through the report.  Synchronous
    # callers (CLI/smoke/manual render) use the exact same resolution path as
    # RenderJobManager instead of leaving an unpinned encoder implicit.
    if encoder_contract is None:
        profile = manifest.get("profile") or {}
        # Older unit-test fixtures intentionally contain only profile_id.  A
        # real compiled manifest always has these canonical fields.
        if {"video_codec", "fps", "pixel_format"}.issubset(profile):
            from .encoder_contract import resolve_encoder_contract

            encoder_contract = resolve_encoder_contract(
                cfg,
                profile,
                str((manifest.get("settings") or {}).get("encoder") or "auto"),
            )
    if encoder_contract is not None:
        from .encoder_contract import validate_encoder_contract

        validate_encoder_contract(encoder_contract, manifest.get("profile") or {})
        manifest = deepcopy(manifest)
        manifest.setdefault("settings", {})["encoder_contract"] = dict(encoder_contract)
    validation = validate_render_manifest(manifest)
    if validation["errors"]:
        raise ProjectRenderError("approved render manifest is invalid: " + "; ".join(validation["errors"]))
    _validate_phase4a_settings(manifest)
    raw_visual_timeline = manifest.get("visual_timeline") if isinstance(manifest.get("visual_timeline"), Mapping) else {}
    if raw_visual_timeline.get("items"):
        try:
            visual_timeline = resolve_visual_timeline(raw_visual_timeline, manifest["segments"], manifest["profile"], require_assets=True)
        except VisualCompositionError as exc:
            raise ProjectRenderError(f"visual composition 被擋下：{exc.message}") from exc
        if raw_visual_timeline.get("resolution_hash") != visual_timeline.get("resolution_hash"):
            raise ProjectRenderError("visual composition resolution 與 approved manifest 不一致，請重新核准")
        if abs(float(manifest.get("expected_duration_seconds") or 0) - float(visual_timeline.get("resolved_duration_seconds") or 0)) > 0.001:
            raise ProjectRenderError("visual timeline duration 與 approved manifest 不一致，請重新核准")
    else:
        base_duration = round(sum(float(item.get("timeline_duration_seconds") or 0) for item in manifest["segments"]), 6)
        visual_timeline = {
            "contract_version": "visual-timeline-v1",
            "resolution_version": "visual-composition-v1",
            "items": [],
            "resolved_items": [],
            "sequence": [
                {"kind": "segment", "stable_id": str(item.get("segment_id") or ""), "type": "segment", "start_seconds": 0, "duration_seconds": float(item.get("timeline_duration_seconds") or 0)}
                for item in manifest["segments"]
            ],
            "resolved_duration_seconds": base_duration,
        }
        visual_timeline["resolution_hash"] = stable_visual_hash(visual_timeline)
    _execution_check(execution)
    _execution_update(execution, stage="validating", percent=5, message="核准與 Manifest 驗證完成")
    segments = sorted(manifest["segments"], key=lambda item: int(item["order"]))
    tracks = list(manifest.get("bgm") or [])
    if len(tracks) > 1:
        raise ProjectRenderError("multiple BGM scheduling is not supported in Phase 4A")
    track = tracks[0] if tracks else None
    normalization = dict(((manifest.get("settings") or {}).get("audio") or {}).get("normalization") or {})
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
    approved_source_fingerprints = _approved_source_fingerprints(snapshot)
    profile_id = str((manifest.get("profile") or {}).get("profile_id") or "unknown")
    paths = prepare_final_output_paths(
        folder,
        manifest,
        approved_hash,
        profile_id,
        ffprobe_path,
        bgm_fp,
        output_path,
        approval_snapshot=snapshot,
        encoder_contract=encoder_contract,
        loudness_policy=normalization,
        ffmpeg_path=ffmpeg_path,
        source_fingerprints=approved_source_fingerprints,
    )
    output = paths.output
    report_path = paths.report
    partial = paths.partial
    report_temp = paths.report_temp
    log_path = paths.log
    if paths.cache_hit:
        _execution_check(execution)
        _execution_update(execution, stage="done", percent=100, message="Final Cache 命中，正式輸出已存在")
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

    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        disk_preflight = ensure_render_free_space(cfg, manifest, output)
    except RetentionError as exc:
        raise ProjectRenderError(exc.message) from exc
    # Formal manifests carry the resolved visual sequence. Legacy fixtures that
    # only have visual_items still use the drawtext contract for compatibility;
    # resolved timelines are composed by visual_compositor exactly once.
    expected = float(visual_timeline["resolved_duration_seconds"])

    work_dir = folder / "work" / approved_hash
    segment_results: list[SegmentRenderResult] = []
    command: list[str] | None = None
    concat_path = work_dir / "timeline.ffconcat"
    visual_filter = None
    partial_created = False
    report_temp_created = False
    output_published = False
    report_published = False
    loudness_analysis = None
    loudness_final = None
    total_segment_duration = max(0.001, sum(float(item["timeline_duration_seconds"]) for item in segments))
    completed_segment_duration = 0.0
    source_probes = SourceProbeRegistry(
        ffprobe_path,
        approved_fingerprints=approved_source_fingerprints,
    )
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        if manifest.get("visual_items") and not visual_timeline.get("resolved_items"):
            visual_filter = prepare_visual_filter(manifest, work_dir)
        for index, segment in enumerate(segments, 1):
            _execution_check(execution)
            segment_duration = float(segment["timeline_duration_seconds"])
            segment_start = 5 + 70 * (completed_segment_duration / total_segment_duration)
            segment_span = 70 * (segment_duration / total_segment_duration)
            _execution_update(
                execution,
                stage="segments",
                percent=segment_start,
                message=f"正在輸出片段 {index}/{len(segments)}",
                current_segment_id=str(segment.get("segment_id") or ""),
                current_segment_index=index,
                force=True,
            )
            _execution_begin_ffmpeg(execution, "segments", segment_start, segment_span, segment_duration, f"正在輸出片段 {index}/{len(segments)}")
            segment_results.append(render_segment(cfg, manifest, segment, runner=runner, source_probe_registry=source_probes))
            completed_segment_duration += segment_duration
            _execution_check(execution)
            _execution_update(execution, stage="segments", percent=5 + 70 * (completed_segment_duration / total_segment_duration), message=f"已完成片段 {index}/{len(segments)}", current_segment_id=str(segment.get("segment_id") or ""), current_segment_index=index)
        # Production results carry the stable segment id.  Keep a positional
        # fallback for legacy callers/test doubles that only return one shared
        # placeholder id; the manifest order remains authoritative.
        segment_paths = {
            str(segment.get("segment_id") or ""): result.output_path
            for segment, result in zip(segments, segment_results)
        }
        segment_paths.update(
            {item.segment_id: item.output_path for item in segment_results if item.segment_id}
        )
        sequence_paths, visual_evidence, visual_overlays = render_visual_cards(
            visual_timeline,
            segment_paths,
            folder / "cache" / "visual",
            work_dir,
            manifest["profile"],
            ffmpeg_path,
            runner=runner or getattr(execution, "runner", None),
        )
        concat_path = build_concat_file(sequence_paths, concat_path)
        _execution_check(execution)
        _execution_begin_ffmpeg(execution, "assembling", 75, 20, expected, "正在組合時間軸與混音")
        if track is None:
            command = build_timeline_command(
                ffmpeg_path,
                concat_path,
                partial,
                duration_seconds=expected,
                normalization=None if normalization.get("enabled") else normalization,
                profile=manifest["profile"],
                video_filter=visual_filter.expression if visual_filter else None,
                encoder_contract=dict(encoder_contract or {}) if encoder_contract else None,
                force_audio_filter=bool(normalization.get("enabled")),
            )
        else:
            command = build_bgm_mix_command(
                ffmpeg_path,
                concat_path,
                partial,
                track,
                expected,
                manifest["profile"],
                normalization=None if normalization.get("enabled") else normalization,
                video_filter=visual_filter.expression if visual_filter else None,
                encoder_contract=encoder_contract if encoder_contract else None,
            )
        partial_created = True
        try:
            result = run_command(command, runner, expected_duration_seconds=expected)
        except TypeError as exc:
            # Keep Phase 3/4A test and caller callables compatible with the new optional hint.
            if "expected_duration_seconds" not in str(exc):
                raise
            result = run_command(command, runner)
        if int(getattr(result, "returncode", 0) or 0) != 0:
            raise ProjectRenderError(str(getattr(result, "stderr", "") or "FFmpeg project render failed"))
        if visual_overlays:
            visual_partial = partial.with_name(f".{partial.stem}.visual.mp4")
            apply_lower_thirds(ffmpeg_path, partial, visual_partial, visual_overlays, manifest["profile"], work_dir, expected, runner=runner or getattr(execution, "runner", None))
            visual_partial.replace(partial)
        if normalization.get("enabled"):
            _execution_check(execution)
            _execution_update(execution, stage="assembling", percent=92, message="正在量測並套用正式音量")
            loudness_analysis = measure_loudness(ffmpeg_path, partial, normalization)
            normalized_partial = partial.with_name(f".{partial.stem}.loudnorm.mp4")
            loudness_command = build_second_pass_command(
                ffmpeg_path,
                partial,
                normalized_partial,
                manifest["profile"],
                loudness_analysis,
                duration_seconds=expected,
            )
            second_result = run_command(loudness_command, runner, expected_duration_seconds=expected)
            if int(getattr(second_result, "returncode", 0) or 0) != 0:
                normalized_partial.unlink(missing_ok=True)
                raise ProjectRenderError(str(getattr(second_result, "stderr", "") or "two-pass loudnorm failed"))
            normalized_partial.replace(partial)
            loudness_final = measure_loudness(ffmpeg_path, partial, normalization)
        _execution_check(execution)
        _execution_update(execution, stage="final_qc", percent=95, message="正在進行 Final QC")
        qc = validate_final_output(partial, manifest, ffprobe_path, ffmpeg_path=ffmpeg_path, loudness=loudness_final)
        if not qc.passed:
            raise ProjectRenderError("final QC failed: " + "; ".join(qc.errors))
        report_loudness = {
            "analysis": loudness_analysis.to_dict() if loudness_analysis is not None else {},
            "final": loudness_final.to_dict() if loudness_final is not None else {},
        }
        report = build_render_report(project_id, manifest, output, qc, segment_results, track, bgm_fp, output_size=partial.stat().st_size, approval_snapshot=snapshot, encoder_contract=encoder_contract, loudness=report_loudness, loudness_policy=normalization, bgm_source="new" if (folder / "audio_settings.json").is_file() else "legacy", cache_miss_reason=paths.cache_miss_reason, disk_preflight=disk_preflight, visual_timeline=visual_timeline, visual_evidence=visual_evidence, source_probe_audit=source_probes.audit())
        report_temp_created = True
        report_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _execution_check(execution)
        _execution_update(execution, stage="publishing", percent=98, message="正在發佈正式輸出")
        publish = lambda: publish_final_render_atomically(partial, report_temp, output, report_path)
        if execution is not None:
            output_published, report_published = execution.publish_atomically(publish)
        else:
            output_published, report_published = publish()
        _write_log(log_path, project_id, approved_hash, segment_results, concat_path, command, result, qc, track, None)
        _cleanup_visual_work(work_dir)
        concat_path.unlink(missing_ok=True)
        cleanup_visual_filter(visual_filter)
        _execution_update(execution, stage="done", percent=100, message="正式輸出完成")
        return ProjectRenderResult(project_id, output, approved_hash, False, qc.duration_seconds, tuple(segment_results), track is not None, tuple(qc.warnings))
    except Exception as exc:
        cleanup_visual_filter(visual_filter)
        _cleanup_render_files(paths, concat_path, partial_created, report_temp_created, output_published, report_published)
        _cleanup_visual_work(work_dir)
        _write_log(log_path, project_id, approved_hash, segment_results, concat_path, command, locals().get("result"), locals().get("qc"), track, exc)
        if isinstance(exc, (ProjectRenderError, PermissionError)):
            raise
        if isinstance(exc, (TimelineAssemblyError, BgmPipelineError, LoudnessError, VisualRenderError)):
            raise ProjectRenderError(str(exc)) from exc
        if isinstance(exc, OSError):
            raise ProjectRenderError(f"project render failed: {exc}") from exc
        raise


def validate_project_output_path(
    folder: Path,
    manifest: Mapping[str, Any],
    output: Path,
    partial: Path,
    report_path: Path,
    report_temp: Path,
) -> None:
    candidates = {
        "output": Path(output).expanduser().resolve(),
        "partial": Path(partial).expanduser().resolve(),
        "report": Path(report_path).expanduser().resolve(),
        "report temp": Path(report_temp).expanduser().resolve(),
    }
    if candidates["output"].suffix.lower() != ".mp4":
        raise ProjectRenderError("Phase 4A project output must use the .mp4 extension")

    source_paths = [
        Path(str(segment.get("source_file"))).expanduser().resolve()
        for segment in manifest.get("segments", [])
        if isinstance(segment, Mapping) and str(segment.get("source_file") or "").strip()
    ]
    bgm_paths = [
        Path(str(track.get("source_path"))).expanduser().resolve()
        for track in manifest.get("bgm", []) or []
        if isinstance(track, Mapping) and str(track.get("source_path") or "").strip()
    ]
    folder = Path(folder).expanduser().resolve()
    protected_cache = (folder / "cache").resolve()
    protected_work = (folder / "work").resolve()
    protected_contracts = [
        folder / "render_manifest.json",
        folder / "review_status.json",
        folder / "render_settings.json",
        folder / "project_plan.json",
        folder / "feedback" / "segment_review.json",
    ]
    protected_contracts = [path.expanduser().resolve() for path in protected_contracts]

    for label, candidate in candidates.items():
        for source in source_paths:
            if candidate == source:
                raise ProjectRenderError(f"{label} path conflicts with source media: {source}")
        for bgm in bgm_paths:
            if candidate == bgm:
                raise ProjectRenderError(f"{label} path conflicts with BGM: {bgm}")
        if _is_within(candidate, protected_cache):
            raise ProjectRenderError(f"{label} path is inside protected segment cache: {candidate}")
        if _is_within(candidate, protected_work):
            raise ProjectRenderError(f"{label} path is inside protected project work directory: {candidate}")
        for contract in protected_contracts:
            if candidate == contract:
                raise ProjectRenderError(f"{label} path conflicts with project contract file: {contract}")


def prepare_final_output_paths(
    folder: Path,
    manifest: Mapping[str, Any],
    approved_hash: str,
    profile_id: str,
    ffprobe_path: str,
    bgm_fp: Mapping[str, Any] | None,
    output_path: Path | None = None,
    *,
    approval_snapshot: Mapping[str, Any] | None = None,
    encoder_contract: Mapping[str, Any] | None = None,
    loudness_policy: Mapping[str, Any] | None = None,
    ffmpeg_path: str = "ffmpeg",
    source_fingerprints: Mapping[str, Mapping[str, Any]] | None = None,
) -> FinalOutputPaths:
    renders = (Path(folder) / "renders").expanduser().resolve()
    managed = output_path is None
    output = (renders / f"project_{manifest.get('project_id')}_{approved_hash[:12]}_{profile_id}.mp4" if managed else Path(output_path)).expanduser().resolve()
    if output.suffix.lower() != ".mp4":
        raise ProjectRenderError("Phase 4A project output must use the .mp4 extension")
    report = output.with_name(output.name + ".render.json")
    partial = output.with_name(f"{output.stem}.partial.mp4")
    report_temp = report.with_name(f".{report.name}.tmp")
    log = renders / "logs" / f"{approved_hash}.log"
    validate_project_output_path(folder, manifest, output, partial, report, report_temp)
    cache_miss_reason = _final_cache_miss_reason(output, report, manifest, approved_hash, profile_id, bgm_fp, ffprobe_path, approval_snapshot=approval_snapshot, encoder_contract=encoder_contract, loudness_policy=loudness_policy, ffmpeg_path=ffmpeg_path, source_fingerprints=source_fingerprints)
    cache_hit = not cache_miss_reason
    paths = FinalOutputPaths(output, partial, report, report_temp, log, managed, cache_hit, cache_miss_reason)
    if cache_hit:
        return paths
    if managed:
        for stale in (output, report, partial, report_temp):
            _unlink_if_exists(stale)
    elif any(path.exists() for path in (output, report, partial, report_temp)):
        raise ProjectRenderError("custom output already exists and is not a valid cache")
    return paths


def publish_final_render_atomically(partial: Path, report_temp: Path, output: Path, report_path: Path) -> tuple[bool, bool]:
    try:
        partial.replace(output)
    except OSError as exc:
        _unlink_if_exists(partial)
        _unlink_if_exists(report_temp)
        raise ProjectRenderError(f"failed to publish final MP4: {exc}") from exc
    try:
        report_temp.replace(report_path)
    except OSError as exc:
        _unlink_if_exists(output)
        _unlink_if_exists(partial)
        _unlink_if_exists(report_temp)
        raise ProjectRenderError(f"failed to publish render report: {exc}") from exc
    return True, True


def _cleanup_render_files(paths: FinalOutputPaths, concat_path: Path, partial_created: bool, report_temp_created: bool, output_published: bool, report_published: bool) -> None:
    _unlink_if_exists(concat_path)
    if partial_created:
        _unlink_if_exists(paths.partial)
    if report_temp_created:
        _unlink_if_exists(paths.report_temp)
    if output_published:
        _unlink_if_exists(paths.output)
    if report_published:
        _unlink_if_exists(paths.report)


def _cleanup_visual_work(work_dir: Path) -> None:
    for path in work_dir.glob("visual-text-*.txt"):
        _unlink_if_exists(path)
    for path in work_dir.glob("card-*.txt"):
        _unlink_if_exists(path)
    for path in work_dir.glob("*.visual.mp4"):
        _unlink_if_exists(path)


def _unlink_if_exists(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


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
    approval_snapshot: Mapping[str, Any] | None = None,
    encoder_contract: Mapping[str, Any] | None = None,
    loudness: Any | None = None,
    loudness_policy: Mapping[str, Any] | None = None,
    bgm_source: str = "",
    cache_miss_reason: str = "",
    disk_preflight: Mapping[str, Any] | None = None,
    visual_timeline: Mapping[str, Any] | None = None,
    visual_evidence: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    source_probe_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    visual = dict(visual_timeline or {})
    return {
        "project_id": project_id,
        "manifest_hash": manifest["manifest_hash"],
        "approval_snapshot": {
            "snapshot_id": (approval_snapshot or {}).get("snapshot_id", ""),
            "snapshot_hash": (approval_snapshot or {}).get("snapshot_hash", ""),
            "schema_version": (approval_snapshot or {}).get("schema_version", ""),
            "approved_project_revision": (approval_snapshot or {}).get("approved_project_revision", ""),
        },
        "encoder_contract": dict(encoder_contract or {}),
        "encoder_probe_audit": dict((encoder_contract or {}).get("nvenc_probe") or {}),
        "loudness": dict(loudness) if isinstance(loudness, Mapping) else (loudness.to_dict() if loudness is not None and hasattr(loudness, "to_dict") else {}),
        "bgm_source": str(bgm_source or "unknown"),
        "color": {
            "project": dict((manifest.get("settings") or {}).get("color") or {}),
            "segments": {str(item.get("segment_id")): dict(item.get("color") or {}) for item in manifest.get("segments", []) if isinstance(item, Mapping)},
        },
        "timing": {"expected_duration_seconds": manifest.get("expected_duration_seconds"), "measured_duration_seconds": qc.duration_seconds},
        "visual_timeline": {
            "contract_version": visual.get("contract_version", ""),
            "resolution_version": visual.get("resolution_version", ""),
            "resolution_hash": visual.get("resolution_hash", ""),
            "duration_seconds": visual.get("resolved_duration_seconds", manifest.get("expected_duration_seconds")),
            "items": [dict(item) for item in (visual_evidence or [])],
            "item_count": len(visual_evidence or manifest.get("visual_items") or []),
            "item_ids": [str(item.get("stable_id") or "") for item in (visual_evidence or manifest.get("visual_items") or []) if isinstance(item, Mapping)],
            "composed": bool(visual_evidence or manifest.get("visual_items")),
        },
        "qc_schema_version": 2,
        "cache": {
            "qc_policy_version": 2,
            "miss_reason": str(cache_miss_reason or ""),
            "loudness_policy_key": _policy_key(loudness_policy),
            "snapshot_id": (approval_snapshot or {}).get("snapshot_id", ""),
            "snapshot_hash": (approval_snapshot or {}).get("snapshot_hash", ""),
            "encoder_contract_hash": (encoder_contract or {}).get("contract_hash") or _stable_hash(dict(encoder_contract or {})),
            "visual_timeline_hash": stable_visual_hash(visual) if visual else "",
        },
        "probe_audit": dict(source_probe_audit or {}),
        "disk_preflight": dict(disk_preflight or {}),
        "measurements": dict(qc.measurements or {}),
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


def _approved_source_fingerprints(snapshot: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """Index immutable approval evidence without hashing any source file."""

    result: dict[str, Mapping[str, Any]] = {}
    for asset in (snapshot or {}).get("assets", []) or []:
        if not isinstance(asset, Mapping) or str(asset.get("kind") or "") != "source":
            continue
        path = str(asset.get("canonical_path") or "").strip()
        if not path:
            continue
        result[str(Path(path).expanduser().resolve())] = asset
    return result


def _validate_phase4a_settings(manifest: Mapping[str, Any]) -> None:
    settings = manifest.get("settings") or {}
    transition = settings.get("transition") or {}
    if str(transition.get("type") or "cut") != "cut" or abs(float(transition.get("duration_seconds") or 0)) > 0.000001:
        raise ProjectRenderError("unsupported transition in Phase 4A")
    if bool((settings.get("overlay") or {}).get("enabled", False)):
        raise ProjectRenderError("overlay is not supported in Phase 4A")


def _final_cache_valid(output: Path, report_path: Path, manifest: Mapping[str, Any], manifest_id: str, profile_id: str, bgm_fp: Mapping[str, Any] | None, ffprobe_path: str, **kwargs: Any) -> bool:
    return not _final_cache_miss_reason(output, report_path, manifest, manifest_id, profile_id, bgm_fp, ffprobe_path, **kwargs)


def _final_cache_miss_reason(output: Path, report_path: Path, manifest: Mapping[str, Any], manifest_id: str, profile_id: str, bgm_fp: Mapping[str, Any] | None, ffprobe_path: str, *, approval_snapshot: Mapping[str, Any] | None = None, encoder_contract: Mapping[str, Any] | None = None, loudness_policy: Mapping[str, Any] | None = None, ffmpeg_path: str = "ffmpeg", source_fingerprints: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    if not output.is_file() or not report_path.is_file():
        return "output_or_report_missing"
    try:
        report = _read_json(report_path)
        if report.get("manifest_hash") != manifest_id or report.get("profile_id") != profile_id:
            return "manifest_or_profile_changed"
        if int(report.get("qc_schema_version") or 0) < 2:
            return "qc_policy_outdated"
        cache = report.get("cache") if isinstance(report.get("cache"), Mapping) else {}
        if int(cache.get("qc_policy_version") or 0) != 2:
            return "qc_policy_changed"
        expected_snapshot = approval_snapshot or {}
        report_snapshot = report.get("approval_snapshot") if isinstance(report.get("approval_snapshot"), Mapping) else {}
        for key in ("snapshot_id", "snapshot_hash"):
            expected = str(expected_snapshot.get(key) or "")
            if expected and str(report_snapshot.get(key) or "") != expected:
                return "approval_snapshot_changed"
            if expected and str(cache.get(key) or "") != expected:
                return "approval_snapshot_changed"
        if encoder_contract:
            expected_encoder_hash = str(encoder_contract.get("contract_hash") or _stable_hash(encoder_contract))
            actual_encoder_hash = str((report.get("encoder_contract") or {}).get("contract_hash") or _stable_hash(report.get("encoder_contract") or {}))
            if actual_encoder_hash != expected_encoder_hash:
                return "encoder_contract_changed"
            if str(cache.get("encoder_contract_hash") or "") != expected_encoder_hash:
                return "encoder_contract_changed"
        if _policy_key(loudness_policy) != str(cache.get("loudness_policy_key") or _policy_key(report.get("loudness_policy") or {})):
            return "loudness_policy_changed"
        current_visual = manifest.get("visual_timeline")
        current_visual_hash = stable_visual_hash(current_visual) if isinstance(current_visual, Mapping) and current_visual else ""
        if str(cache.get("visual_timeline_hash") or "") != current_visual_hash:
            return "visual_timeline_changed"
        expected_keys = [
            build_segment_cache_key(
                manifest,
                segment,
                source_fingerprint=(source_fingerprints or {}).get(
                    str(Path(str(segment.get("source_file") or "")).expanduser().resolve())
                ),
            )
            for segment in sorted(manifest["segments"], key=lambda item: int(item["order"]))
        ]
        actual_keys = [item.get("cache_key") for item in report.get("segments", [])]
        if actual_keys != expected_keys:
            return "segment_cache_changed"
        if (report.get("bgm") or {}).get("fingerprint") != dict(bgm_fp or {}):
            return "bgm_changed"
        if report.get("output_size") != output.stat().st_size or report.get("output_sha256") != sha256_file(output):
            return "output_hash_changed"
        if not bool((report.get("qc") or {}).get("passed")):
            return "previous_qc_failed"
        measured = None
        if loudness_policy and bool(loudness_policy.get("enabled")):
            from .loudness import measure_loudness

            measured = measure_loudness(ffmpeg_path, output, loudness_policy)
        qc = validate_final_output(output, manifest, ffprobe_path, ffmpeg_path=ffmpeg_path, loudness=measured)
        if not qc.passed:
            return "final_qc_revalidation_failed"
        if measured is not None:
            final = (report.get("loudness") or {}).get("final") or {}
            if abs(float(final.get("measured_I", 999)) - measured.measured_I) > 0.2 or abs(float(final.get("measured_TP", 999)) - measured.measured_TP) > 0.2:
                return "loudness_measurement_changed"
        return ""
    except Exception:
        return "cache_report_invalid"


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _policy_key(value: Mapping[str, Any] | None) -> str:
    return _stable_hash(value or {})


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


def _execution_check(execution: Any | None) -> None:
    if execution is not None:
        execution.check_cancelled()


def _execution_update(execution: Any | None, **changes: Any) -> None:
    if execution is not None:
        execution.update(**changes)


def _execution_begin_ffmpeg(execution: Any | None, stage: str, base_percent: float, span_percent: float, expected_duration: float, message: str) -> None:
    if execution is not None:
        execution.begin_ffmpeg(stage, base_percent, span_percent, expected_duration, message)


__all__ = [
    "FinalOutputPaths",
    "ProjectRenderError",
    "ProjectRenderResult",
    "build_render_report",
    "prepare_final_output_paths",
    "publish_final_render_atomically",
    "render_project",
    "validate_project_output_path",
]
