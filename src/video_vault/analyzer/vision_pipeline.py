from __future__ import annotations

from pathlib import Path
import json

from .cloud_provider import CloudProvider
from .frame_analysis import PROMPT_VERSION, cache_key, merge_frames_to_segments
from .multi_frame import (
    MULTI_FRAME_CONTRACT_VERSION,
    MULTI_FRAME_PROMPT_VERSION,
    MAX_WINDOW_FRAMES,
    MIN_WINDOW_FRAMES,
    MultiFrameUnsupported,
    MultiFrameValidationError,
    model_provenance,
    normalize_window_result,
    atomic_write_json,
    plan_frame_windows,
    provider_capability,
    validate_window,
    window_cache_key,
    write_non_mandatory_evidence,
    write_window_evidence,
)
from .mock_provider import MockProvider
from .local_provider import LocalProvider
from ..database import frames as db_frames, replace_segments, update_frame_analysis
from ..segment_state_migration import migrate_segment_state_for_video


class AnalysisCancelled(RuntimeError):
    pass


def provider_from_config(cfg: dict):
    name = cfg.get("ai", {}).get("provider", "mock")
    if name == "cloud":
        return CloudProvider(cfg)
    if name == "local":
        return LocalProvider(cfg)
    if name == "mock":
        return MockProvider(cfg)
    raise ValueError(f"unsupported AI provider: {name}")


def analyze_frame_manifest(
    video: dict,
    cfg: dict,
    frame_manifest: list[dict],
    progress=None,
    should_cancel=None,
    duration_seconds: float | None = None,
) -> dict:
    """Analyze an explicit frame manifest without mutating published DB rows."""
    provider = provider_from_config(cfg)
    raw_dir = Path(cfg["library_root"]) / "05_index" / "raw_ai_outputs"
    analyzed = []
    total = len(frame_manifest)
    cache_hits = 0
    vision_calls = 0
    for index, frame in enumerate(frame_manifest, 1):
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")
        frame_path = Path(str(frame["frame_path"]))
        timestamp = float(frame.get("timestamp_seconds") or 0)
        key = cache_key(
            frame_path,
            provider.provider,
            provider.model,
            getattr(provider, "prompt_version", PROMPT_VERSION),
        )
        raw_path = raw_dir / f"{key}.json"
        if raw_path.exists():
            result = json.loads(raw_path.read_text(encoding="utf-8"))["parsed"]
            cache_hits += 1
        else:
            result, raw = provider.analyze_frame(frame_path, timestamp, video)
            vision_calls += 1
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps(
                    {"frame": str(frame_path), "parsed": result, "raw": raw},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        analyzed.append(
            {
                "frame_path": str(frame_path),
                "timestamp_seconds": timestamp,
                **result,
            }
        )
        if progress:
            progress(index, total, frame)
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")
    sampling_policy = cfg.get("_sampling_policy") or {}
    grouping_interval = float(
        sampling_policy.get("baseline_interval_seconds")
        or cfg["frame_interval_seconds"]
    )
    perceived_segments = merge_frames_to_segments(
        analyzed,
        grouping_interval,
        duration_seconds=(
            float(video.get("duration_seconds") or 0)
            if duration_seconds is None
            else duration_seconds
        ),
    )
    return {
        "provider": provider.provider,
        "model": provider.model,
        "frames": analyzed,
        "segments": perceived_segments,
        "cache_hits": cache_hits,
        "vision_calls": vision_calls,
    }


def analyze_frame_windows(
    video: dict,
    cfg: dict,
    frame_manifest: list[dict],
    progress=None,
    should_cancel=None,
    duration_seconds: float | None = None,
    evidence_root: Path | None = None,
    windows: list[dict] | None = None,
    non_mandatory_fragments: list[dict] | None = None,
) -> dict:
    """Analyze explicit 3-5 frame windows through a provider contract.

    A short clip with fewer than three usable samples is returned as a blocked
    formal result. It never invokes the single-frame analyzer or produces
    publishable segments. A provider that cannot consume multi-image input is
    always rejected.
    """

    provider = provider_from_config(cfg)
    duration = float(video.get("duration_seconds") or 0) if duration_seconds is None else float(duration_seconds)
    capability = provider_capability(provider, cfg)
    max_images = int(capability.get("maximum_images") or 0)
    if windows is None:
        plan = plan_frame_windows(
            frame_manifest,
            duration,
            max_frames=max_images if max_images >= MIN_WINDOW_FRAMES else MAX_WINDOW_FRAMES,
        )
        planned_windows = list(plan["mandatory_windows"])
        uncovered_fragments = list(plan["non_mandatory_fragments"])
    else:
        planned_windows = list(windows)
        uncovered_fragments = list(non_mandatory_fragments or [])
    evidence_root = evidence_root or (Path(cfg["library_root"]) / "05_index" / "multi_frame_evidence")
    non_mandatory_evidence_path = write_non_mandatory_evidence(uncovered_fragments, evidence_root)
    non_mandatory_fragment_uuids = [str(item.get("fragment_uuid") or "") for item in uncovered_fragments]
    validation_reports: list[dict] = []
    eligible: list[dict] = []
    invalid_windows: list[dict] = []
    for window in planned_windows:
        validation = validate_window(window)
        window["validation"] = validation
        validation_reports.append({"window_uuid": window.get("window_uuid"), **validation})
        if validation["status"] == "pass":
            eligible.append(window)
        else:
            invalid_windows.append(window)

    if invalid_windows or not eligible:
        for window in planned_windows:
            write_window_evidence(
                window,
                None,
                window.get("validation") or {},
                evidence_root,
                ffmpeg_path=str(cfg.get("ffmpeg_path") or "ffmpeg"),
            )
        reasons: list[str] = []
        for window in invalid_windows:
            for reason in (window.get("validation") or {}).get("needs_review_reasons") or []:
                if reason not in reasons:
                    reasons.append(str(reason))
        for fragment in uncovered_fragments:
            reason = str(fragment.get("reason") or "")
            if reason and reason not in reasons:
                reasons.append(reason)
        if not reasons:
            reasons.append("missing_mandatory_window_results")
        if not bool(capability.get("supports_multi_image")):
            if "multi_frame_capability_unverified" not in reasons:
                reasons.append("multi_frame_capability_unverified")
        provenance = model_provenance(provider, capability)
        planned_window_uuids = [str(window.get("window_uuid") or "") for window in planned_windows]
        return {
            "provider": provider.provider,
            "model": provider.model,
            "frames": [],
            "segments": [],
            "window_manifest": planned_windows,
            "non_mandatory_evidence": uncovered_fragments,
            "window_results": [],
            "window_validation": {
                "status": "blocked",
                "checks": validation_reports,
                "evidence_artifact_ids": [str(window.get("window_uuid") or "") for window in planned_windows],
                "planned_window_uuids": planned_window_uuids,
                "covered_window_uuids": [],
                "needs_review_reasons": reasons,
            },
            "multi_frame_contract": {
                "version": MULTI_FRAME_CONTRACT_VERSION,
                "schema_version": 1,
                "prompt_version": MULTI_FRAME_PROMPT_VERSION,
                "provider": provider.provider,
                "model": provider.model,
                "model_provenance": provenance,
                "supports_multi_frame": bool(capability.get("supports_multi_image")),
                "max_images": max_images,
                "capability": capability,
                "mandatory_window_uuids": planned_window_uuids,
                "covered_window_uuids": [],
                "non_mandatory_fragment_uuids": non_mandatory_fragment_uuids,
                "non_mandatory_evidence_path": non_mandatory_evidence_path,
                "status": "blocked",
                "failure_reasons": reasons,
            },
            "cache_hits": 0,
            "vision_calls": 0,
        }

    if not bool(capability.get("supports_multi_image")):
        raise MultiFrameUnsupported(
            f"provider {provider.provider} does not have verified multi-image capability; "
            "no single-frame fallback is allowed"
        )
    if max_images < 3:
        raise MultiFrameUnsupported(f"provider {provider.provider} allows fewer than three images per window")

    raw_dir = Path(cfg["library_root"]) / "05_index" / "raw_ai_outputs" / "multiframe"
    window_results: list[dict] = []
    cache_hits = 0
    vision_calls = 0
    frame_to_result: dict[str, dict] = {}
    for index, window in enumerate(eligible, 1):
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")
        frame_entries = list(window.get("frames") or [])
        if len(frame_entries) > max_images:
            raise MultiFrameValidationError(f"window {window.get('window_uuid')} exceeds provider image limit")
        frame_paths = [Path(str(frame.get("frame_path") or "")) for frame in frame_entries]
        timestamps = [float(frame.get("timestamp_seconds") or 0) for frame in frame_entries]
        key = window_cache_key(
            window,
            provider=provider.provider,
            model=provider.model,
            prompt_version=str(getattr(provider, "multi_frame_prompt_version", MULTI_FRAME_PROMPT_VERSION)),
            policy=window.get("window_policy"),
            provider_contract_version=str(capability.get("provider_contract_version") or ""),
            maximum_images=max_images,
            supported_image_formats=list(capability.get("supported_image_formats") or []),
        )
        cache_path = raw_dir / f"{key}.json"
        cache_hit = False
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if str(cached.get("cache_key") or "") != key:
                    raise ValueError("stale multi-frame cache contract")
                payload = cached["parsed"]
                normalized = normalize_window_result(payload, window)
                raw = cached.get("raw") or {}
                cache_hits += 1
                cache_hit = True
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, MultiFrameValidationError):
                payload = None
                raw = {}
        if not cache_hit:
            analyze_window = getattr(provider, "analyze_window", None)
            if not callable(analyze_window):
                raise MultiFrameUnsupported(f"provider {provider.provider} has no multi-frame method")
            payload, raw = analyze_window(frame_paths, timestamps, video)
            if should_cancel and should_cancel():
                raise AnalysisCancelled("perception cancelled by user")
            normalized = normalize_window_result(payload, window)
            atomic_write_json(
                cache_path,
                {
                    "cache_key": key,
                    "window_uuid": window.get("window_uuid"),
                    "parsed": normalized,
                    "raw": raw,
                    "provider_contract": capability,
                },
            )
            vision_calls += 1
        validation = dict(window.get("validation") or validate_window(window))
        evidence = write_window_evidence(
            window,
            normalized,
            validation,
            evidence_root,
            ffmpeg_path=str(cfg.get("ffmpeg_path") or "ffmpeg"),
            raw_response=raw,
            provider_contract=capability,
        )
        if evidence.get("contact_sheet_error"):
            validation.setdefault("checks", []).append({
                "code": "contact_sheet",
                "status": "blocked",
                "error": evidence["contact_sheet_error"],
            })
            validation["status"] = "blocked"
            validation.setdefault("needs_review_reasons", []).append("contact_sheet_unavailable")
        validation["evidence_artifact_ids"] = [str(window.get("window_uuid") or "")]
        result = {
            "window_uuid": str(window.get("window_uuid") or ""),
            "ordinal": int(window.get("ordinal") or index),
            "start_seconds": float(window.get("start_seconds") or 0),
            "end_seconds": float(window.get("end_seconds") or 0),
            "frame_timestamps": timestamps,
            "frame_fingerprints": [str(frame.get("fingerprint") or "") for frame in frame_entries],
            "cache_key": key,
            "cache_hit": cache_hit,
            "validation": validation,
            "model_provenance": model_provenance(provider, capability),
            "evidence": evidence,
            **normalized,
        }
        window_results.append(result)
        for frame in frame_entries:
            frame_to_result[str(frame.get("frame_path") or "")] = result
        if progress:
            progress(index, len(eligible), window)
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")

    analyzed_frames = []
    for frame in sorted(frame_manifest, key=lambda item: float(item.get("timestamp_seconds") or 0)):
        result = frame_to_result.get(str(frame.get("frame_path") or ""))
        if result is None:
            continue
        analyzed_frames.append(
            {
                "frame_path": str(frame.get("frame_path") or ""),
                "timestamp_seconds": float(frame.get("timestamp_seconds") or 0),
                "summary": result["summary"],
                "tags": result.get("tags") or [],
                "visual_quality_score": float(result["technical_quality"]["score"]),
                "usefulness_score": float(result["confidence"]),
                "suggested_use": result["shot_role"],
                "window_uuid": result["window_uuid"],
                "window_confidence": result["confidence"],
                "action": result["action"],
                "natural_audio_recommendation": result["natural_audio_recommendation"],
            }
        )
    segments = []
    for result in window_results:
        segments.append(
            {
                "start_seconds": result["start_seconds"],
                "end_seconds": result["end_seconds"],
                "segment_type": result["shot_role"] or "multi_frame",
                "title": result["summary"] or result["action"] or "多影格候選片段",
                "reason": f"{result['action']}；{result['summary']}",
                "tags": result.get("tags") or [],
                "score": result["confidence"],
                "suggested_use": result["shot_role"] or "補畫面",
                "window_uuid": result["window_uuid"],
                "action": result["action"],
                "shot_role": result["shot_role"],
                "technical_quality": result["technical_quality"],
                "duplicate_group": result["duplicate_group"],
                "natural_audio_recommendation": result["natural_audio_recommendation"],
                "confidence": result["confidence"],
            }
        )
    result_statuses = [str(item.get("validation", {}).get("status") or "") for item in window_results]
    overall_status = "pass" if result_statuses and all(status == "pass" for status in result_statuses) else "blocked"
    return {
        "provider": provider.provider,
        "model": provider.model,
        "frames": analyzed_frames,
        "segments": segments,
        "window_manifest": planned_windows,
        "non_mandatory_evidence": uncovered_fragments,
        "window_results": window_results,
        "window_validation": {
            "status": overall_status,
            "checks": validation_reports,
            "evidence_artifact_ids": [str(item.get("window_uuid") or "") for item in window_results],
            "planned_window_uuids": [str(item.get("window_uuid") or "") for item in planned_windows],
            "covered_window_uuids": [str(item.get("window_uuid") or "") for item in window_results],
            "needs_review_reasons": [] if overall_status == "pass" else ["window_validation_warning"],
        },
        "multi_frame_contract": {
            "version": MULTI_FRAME_CONTRACT_VERSION,
            "schema_version": 1,
            "prompt_version": str(getattr(provider, "multi_frame_prompt_version", MULTI_FRAME_PROMPT_VERSION)),
            "provider": provider.provider,
            "model": provider.model,
            "model_provenance": model_provenance(provider, capability),
            "supports_multi_frame": bool(capability.get("supports_multi_image")),
            "max_images": max_images,
            "capability": capability,
            "mandatory_window_uuids": [str(item.get("window_uuid") or "") for item in planned_windows],
            "covered_window_uuids": [str(item.get("window_uuid") or "") for item in window_results],
            "non_mandatory_fragment_uuids": non_mandatory_fragment_uuids,
            "non_mandatory_evidence_path": non_mandatory_evidence_path,
            "status": overall_status,
        },
        "cache_hits": cache_hits,
        "vision_calls": vision_calls,
    }


def analyze_video_frames(db: Path, video: dict, cfg: dict, progress=None) -> dict:
    """Legacy immediate-publish wrapper used by CLI and non-project flows."""
    frame_rows = [dict(frame) for frame in db_frames(db, int(video["id"]))]
    result = analyze_frame_manifest(video, cfg, frame_rows, progress)
    for frame_row, analyzed in zip(frame_rows, result["frames"], strict=True):
        update_frame_analysis(db, int(frame_row["id"]), analyzed)
    migration = replace_segments(db, int(video["id"]), result["segments"])
    project_migrations = migrate_segment_state_for_video(
        cfg,
        db,
        int(video["id"]),
        migration,
    )
    return {
        **result,
        "segment_identity_migration": migration,
        "project_segment_state_migrations": project_migrations,
    }
