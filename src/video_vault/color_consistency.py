"""Project-level color consistency state, analysis, and preview helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Mapping

from .project import mark_project_needs_review, project_dir
from .render_settings import load_render_settings


COLOR_STATE_VERSION = 2
ADJUSTMENT_DEFAULTS: dict[str, float] = {
    "exposure": 0.0,
    "temperature": 0.0,
    "tint": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gamma": 1.0,
    "highlights": 0.0,
    "shadows": 0.0,
}
ADJUSTMENT_LIMITS: dict[str, tuple[float, float]] = {
    "exposure": (-1.5, 1.0),
    "temperature": (-30.0, 30.0),
    "tint": (-20.0, 20.0),
    "contrast": (0.85, 1.15),
    "saturation": (0.8, 1.2),
    "gamma": (0.85, 1.15),
    "highlights": (-1.0, 1.0),
    "shadows": (-1.0, 1.0),
}
COLOR_MODES = frozenset({"none", "safe_restore", "warm_food", "manual", "dji_lut", "dji_dlog", "dji_dlog_m"})
LUT_MODES = frozenset({"dji_lut", "dji_dlog", "dji_dlog_m"})
_USER_EDITABLE_COLOR_FIELDS = frozenset({"enabled", "applied", "segments"})
_USER_EDITABLE_SEGMENT_FIELDS = frozenset({"enabled", "locked", "excluded", "applied"})
_EXCLUDED_SEGMENT_WARNING = "此段已排除色彩分析"


class ColorReferenceError(ValueError):
    """A reference frame cannot be created or safely used."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def color_state_path(cfg: dict, project_id: int) -> Path:
    return project_dir(cfg, project_id) / "color_consistency.json"


def color_dir(cfg: dict, project_id: int) -> Path:
    path = project_dir(cfg, project_id) / "color"
    path.mkdir(parents=True, exist_ok=True)
    (path / "reference_frames").mkdir(parents=True, exist_ok=True)
    return path


def default_color_state() -> dict[str, Any]:
    adjustments = deepcopy(ADJUSTMENT_DEFAULTS)
    return {
        "schema_version": COLOR_STATE_VERSION,
        "enabled": True,
        "reference": {},
        "references": [],
        "analysis": {},
        "suggested": {"mode": "none", "lut_path": "", "lut_kind": "", **deepcopy(adjustments)},
        "applied": {"mode": "none", "lut_path": "", "lut_kind": "", **adjustments},
        "segments": {},
    }


def load_project_color_state(cfg: dict, project_id: int) -> dict[str, Any]:
    path = color_state_path(cfg, project_id)
    if not path.exists():
        return default_color_state()
    try:
        return normalize_color_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default_color_state()


def has_color_state(cfg: dict, project_id: int) -> bool:
    return color_state_path(cfg, project_id).is_file()


def normalize_color_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    base = default_color_state()
    source = dict(state or {})
    result = _merge(base, source)
    result["schema_version"] = COLOR_STATE_VERSION
    result["enabled"] = _as_bool(result.get("enabled", True))
    result["reference"] = dict(result.get("reference") or {})
    result["references"] = [dict(item) for item in result.get("references") or [] if isinstance(item, Mapping)]
    result["analysis"] = dict(result.get("analysis") or {})
    result["suggested"] = _normalize_adjustment_set(result.get("suggested"), base["suggested"])
    result["applied"] = _normalize_adjustment_set(result.get("applied"), base["applied"])
    segments: dict[str, Any] = {}
    for segment_id, value in (result.get("segments") or {}).items():
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        item["enabled"] = _as_bool(item.get("enabled", True))
        item["locked"] = _as_bool(item.get("locked", False))
        item["excluded"] = _as_bool(item.get("excluded", False))
        item["reference_candidate"] = _as_bool(item.get("reference_candidate", False))
        item["confidence"] = _finite_confidence(item.get("confidence", 0.0))
        item["warnings"] = [str(warning) for warning in item.get("warnings", []) or []]
        item["suggested"] = _normalize_adjustment_set(item.get("suggested"), result["suggested"])
        item["applied"] = _normalize_adjustment_set(item.get("applied"), result["applied"])
        segments[str(segment_id)] = item
    result["segments"] = segments
    return result


def save_project_color_state(cfg: dict, db: Path, project_id: int, state: Mapping[str, Any], *, mark_review: bool = True) -> Path:
    normalized = normalize_color_state(state)
    path = color_state_path(cfg, project_id)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def effective_color_settings(state: Mapping[str, Any], segment_id: str | None = None) -> dict[str, Any]:
    normalized = normalize_color_state(state)
    if not normalized["enabled"]:
        return {**deepcopy(normalized["applied"]), "mode": "none"}
    result = deepcopy(normalized["applied"])
    if segment_id:
        override = normalized["segments"].get(str(segment_id), {})
        if override.get("excluded") or not override.get("enabled", True):
            result["mode"] = "none"
        elif isinstance(override.get("applied"), Mapping):
            result.update(override["applied"])
    return result


def analyze_project_color(cfg: dict, db: Path, project_id: int, *, force: bool = False) -> dict[str, Any]:
    existing = load_project_color_state(cfg, project_id)
    if existing.get("analysis") and not force:
        return existing
    references = _reference_candidates(db, project_id, cfg)
    excluded_ids = {str(key) for key, value in existing.get("segments", {}).items() if value.get("excluded")}
    references = [item for item in references if not _is_excluded_reference(item, excluded_ids)]
    selected = _keep_or_select_reference(existing, references)
    if selected:
        selected = ensure_reference_frame(cfg, db, project_id, selected)
        references = [selected if item.get("id") == selected.get("id") else item for item in references]
    stats = _reference_luma(cfg, selected) if selected else _empty_color_stats()
    suggested = _suggested_adjustments(cfg, project_id, stats, existing)
    settings = load_render_settings(cfg, project_id)
    configured = dict(settings.get("color") or {})
    if not configured.get("lut_path"):
        configured["lut_path"] = str(cfg.get("color", {}).get("dji_lut_path") or "")
    mode = str(configured.get("mode") or "none")
    if mode == "none" and configured.get("lut_path"):
        mode = str(cfg.get("color", {}).get("default_mode") or "dji_dlog_m")
    suggested.update({"mode": mode, "lut_path": str(configured.get("lut_path") or ""), "lut_kind": str(configured.get("lut_kind") or mode)})
    applied = existing["applied"] if existing.get("analysis") else deepcopy(suggested)
    segment_states = _analyze_segments(cfg, db, project_id, existing, selected, stats, applied)
    analysis_warnings = _lut_warnings(suggested)
    state = normalize_color_state({
        **existing,
        "reference": selected or {},
        "references": references,
        "analysis": {
            "reference": selected or {},
            "luma": stats,
            "confidence": _confidence(stats, bool(selected)),
            "basis_text": _basis_text(selected, stats),
            "source_count": len(_project_source_ids(db, project_id)),
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warnings": analysis_warnings,
            "statistics": stats,
        },
        "suggested": suggested,
        "applied": applied,
        "segments": segment_states,
    })
    save_project_color_state(cfg, db, project_id, state)
    return state


def set_color_reference(cfg: dict, db: Path, project_id: int, reference_id: str) -> dict[str, Any]:
    state = load_project_color_state(cfg, project_id)
    selected = next((item for item in state["references"] if str(item.get("id")) == str(reference_id)), None)
    if not selected:
        raise ValueError(f"找不到色彩基準畫面：{reference_id}")
    selected = ensure_reference_frame(cfg, db, project_id, selected)
    stats = _reference_luma(cfg, selected)
    suggested = _suggested_adjustments(cfg, project_id, stats, state)
    suggested.update({"mode": state["suggested"].get("mode", "none"), "lut_path": state["suggested"].get("lut_path", ""), "lut_kind": state["suggested"].get("lut_kind", "")})
    references = [selected if str(item.get("id")) == str(selected.get("id")) else item for item in state.get("references", [])]
    segment_states = _analyze_segments(cfg, db, project_id, state, selected, stats, state["applied"], preserve_manual_segments=True)
    state = normalize_color_state({
        **state,
        "reference": selected,
        "references": references,
        "analysis": {
            **state.get("analysis", {}),
            "reference": selected,
            "luma": stats,
            "confidence": _confidence(stats, True),
            "basis_text": _basis_text(selected, stats),
            "warnings": _lut_warnings(suggested),
            "statistics": stats,
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "suggested": suggested,
        "segments": segment_states,
    })
    save_project_color_state(cfg, db, project_id, state)
    return state


def update_color_state(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any]) -> dict[str, Any]:
    current = load_project_color_state(cfg, project_id)
    updated = _merge(current, _user_editable_color_patch(patch))
    state = normalize_color_state(updated)
    save_project_color_state(cfg, db, project_id, state)
    return state


def preview_cache_key(source: Path, state: Mapping[str, Any], settings: Mapping[str, Any] | None = None, kind: str = "after") -> str:
    path = source.expanduser().resolve()
    normalized = normalize_color_state(state)
    requested = dict(settings or {})
    segment_id = requested.get("segment_id")
    start = requested.get("start", requested.get("start_seconds"))
    duration = requested.get("duration", requested.get("duration_seconds"))
    effective = requested.get("effective_settings", requested.get("effective"))
    if not isinstance(effective, Mapping):
        metadata_fields = {"segment_id", "start", "start_seconds", "duration", "duration_seconds", "effective", "effective_settings"}
        effective = requested if requested and not (set(requested) & metadata_fields) else effective_color_settings(normalized, str(segment_id) if segment_id is not None else None)
    payload = {
        "segment_id": str(segment_id) if segment_id is not None else None,
        "start_seconds": _cache_number(start),
        "duration_seconds": _cache_number(duration),
        "effective_settings": dict(effective),
        "effective_lut_fingerprint": _lut_cache_fingerprint(effective),
        "source_fingerprint": _source_cache_fingerprint(path),
        "pipeline_version": COLOR_STATE_VERSION,
        "kind": kind,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def render_project_color_previews(cfg: dict, db: Path, project_id: int, *, force: bool = False, seconds: int = 4) -> dict[str, Any]:
    from .color import render_color_preview
    from .database import project_videos
    from .project import project_segments

    state = load_project_color_state(cfg, project_id)
    if not state.get("analysis"):
        state = analyze_project_color(cfg, db, project_id)
    out_dir = project_dir(cfg, project_id) / "output" / "color_previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    previews = []
    plan_path = project_dir(cfg, project_id) / "project_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    segments = [row for row in project_segments(cfg, project_id, plan) if _as_bool(row.get("include", True))]
    segment_by_video = {}
    for row in segments:
        segment_by_video.setdefault(int(row.get("video_id") or 0), []).append(row)
    for video in project_videos(db, project_id):
        source = Path(str(video["current_path"])).expanduser().resolve()
        video_segments = segment_by_video.get(int(video["id"]), [])
        if not video_segments:
            video_segments = [{"segment_id": f"video:{video['id']}", "video_id": int(video["id"]), "start_seconds": float((state.get("reference") or {}).get("timestamp_seconds") or 0), "end_seconds": float((state.get("reference") or {}).get("timestamp_seconds") or 0) + seconds}]
        for segment in video_segments:
            segment_id = str(segment.get("segment_id") or f"video:{video['id']}")
            effective = effective_color_settings(state, segment_id)
            start = max(0.0, float(segment.get("start_seconds") or 0))
            end = max(start + 0.1, float(segment.get("end_seconds") or start + seconds))
            duration = min(float(seconds), max(0.1, end - start))
            key = preview_cache_key(source, state, {"segment_id": segment_id, "start": start, "duration": duration, "effective_settings": effective}, kind="before-after")
            stem = _safe_token(segment_id)
            before = out_dir / f"{stem}_before_{key[:12]}.mp4"
            after = out_dir / f"{stem}_after_{key[:12]}.mp4"
            metadata_path = out_dir / f"{stem}_{key[:12]}.json"
            cached = before.is_file() and after.is_file() and metadata_path.is_file()
            if force or not cached:
                _render_preview_pair(render_color_preview, source, before, after, cfg, effective, start, duration)
                metadata_path.write_text(json.dumps({"cache_key": key, "segment_id": segment_id, "source": str(source), "source_fingerprint": _source_cache_fingerprint(source), "start_seconds": start, "duration_seconds": duration, "pipeline_version": COLOR_STATE_VERSION, "reference": state.get("reference", {}), "effective": effective, "effective_lut_fingerprint": _lut_cache_fingerprint(effective), "before": before.name, "after": after.name}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                cached = False
            previews.append({"video_id": int(video["id"]), "segment_id": segment_id, "before": before.name, "after": after.name, "cache_key": key, "cache_hit": cached, "start_seconds": start, "duration_seconds": duration})
    return {"ok": True, "state": state, "previews": [_preview_urls(cfg, project_id, item) for item in previews], "files": [_preview_url(project_id, item["after"]) for item in previews]}


def ensure_reference_frame(cfg: dict, db: Path, project_id: int, reference: Mapping[str, Any]) -> dict[str, Any]:
    source = Path(str(reference.get("source_file") or "")).expanduser().resolve()
    if not source.is_file():
        raise ColorReferenceError("source_missing", f"原始素材不存在：{source}")
    timestamp = _finite_number(reference.get("timestamp_seconds"), -1.0)
    if timestamp < 0:
        raise ColorReferenceError("invalid_timestamp", "Reference timestamp 無效")
    duration = _probe_duration(cfg, source)
    if duration is not None and timestamp > duration + 0.001:
        raise ColorReferenceError("timestamp_out_of_range", f"Reference timestamp {timestamp:.3f} 超過素材長度 {duration:.3f}")
    output_dir = color_dir(cfg, project_id) / "reference_frames"
    filename = f"reference_{_safe_token(str(reference.get('id') or 'frame'))}_{round(timestamp * 1000)}.jpg"
    frame_path = output_dir / filename
    if not frame_path.exists():
        cmd = [str(cfg.get("ffmpeg_path") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", "-y", str(frame_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode != 0 or not frame_path.is_file() or frame_path.stat().st_size <= 0:
            frame_path.unlink(missing_ok=True)
            raise ColorReferenceError("frame_extract_failed", f"無法擷取 Reference frame：{result.stderr[-500:]}")
    enriched = {**dict(reference), "frame_path": str(frame_path), "frame_name": frame_path.name, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    reference_path = color_dir(cfg, project_id) / "reference.json"
    temp = reference_path.with_name(".reference.json.tmp")
    temp.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(reference_path)
    return enriched


def color_state_for_api(cfg: dict, project_id: int, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = normalize_color_state(state or load_project_color_state(cfg, project_id))
    if result.get("reference"):
        result["reference"] = _reference_url(result["reference"], project_id)
    result["references"] = [_reference_url(item, project_id) for item in result.get("references", [])]
    if isinstance(result.get("analysis"), Mapping) and isinstance(result["analysis"].get("reference"), Mapping):
        result["analysis"] = {**result["analysis"], "reference": _reference_url(result["analysis"]["reference"], project_id)}
    return result


def preview_file_path(cfg: dict, project_id: int, token: str) -> Path:
    return _safe_project_media_path(project_dir(cfg, project_id) / "output" / "color_previews", token)


def reference_file_path(cfg: dict, project_id: int, token: str) -> Path:
    return _safe_project_media_path(color_dir(cfg, project_id) / "reference_frames", token)


def _normalize_adjustment_set(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    data = {**dict(fallback), **(dict(value) if isinstance(value, Mapping) else {})}
    data["mode"] = str(data.get("mode") or "none")
    data["lut_path"] = str(data.get("lut_path") or "")
    data["lut_kind"] = str(data.get("lut_kind") or "")
    for key, (lower, upper) in ADJUSTMENT_LIMITS.items():
        try:
            number = float(data.get(key, ADJUSTMENT_DEFAULTS[key]))
        except (TypeError, ValueError):
            number = ADJUSTMENT_DEFAULTS[key]
        if not math.isfinite(number):
            number = ADJUSTMENT_DEFAULTS[key]
        data[key] = round(min(upper, max(lower, number)), 6)
    return data


def _suggested_adjustments(cfg: dict, project_id: int, stats: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    average = float(stats.get("average") or 128)
    highlights = float(stats.get("highlight_ratio") or 0)
    result = deepcopy(ADJUSTMENT_DEFAULTS)
    if highlights > 0.18 or average > 178:
        result.update(exposure=-0.5, contrast=0.96, saturation=0.96, gamma=0.9)
    elif highlights > 0.09 or average > 158:
        result.update(exposure=-0.25, contrast=0.98, saturation=0.98, gamma=0.94)
    elif average < 78:
        result.update(exposure=0.35, contrast=1.02, saturation=1.02, gamma=1.05)
    else:
        result.update(exposure=-0.05, contrast=1.0, saturation=0.99, gamma=0.98)
    return _normalize_adjustment_set(result, ADJUSTMENT_DEFAULTS)


def _reference_candidates(db: Path, project_id: int, cfg: dict | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    project_intervals: list[dict[str, Any]] = []
    if cfg is not None:
        try:
            from .project import project_dir, project_segments
            plan_path = project_dir(cfg, project_id) / "project_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
            color_state = load_project_color_state(cfg, project_id)
            overrides = color_state.get("segments", {})
            for segment in project_segments(cfg, project_id, plan):
                segment_id = str(segment.get("segment_id") or "")
                if not segment_id:
                    continue
                override = overrides.get(segment_id, {})
                start = float(segment.get("start_seconds") or 0)
                end = max(start, float(segment.get("end_seconds") or start))
                project_intervals.append({
                    "video_id": int(segment.get("video_id") or 0),
                    "segment_id": segment_id,
                    "start_seconds": start,
                    "end_seconds": end,
                    "enabled": _as_bool(segment.get("include", True)) and _as_bool(override.get("enabled", True)),
                    "excluded": _as_bool(override.get("excluded", False)),
                    "manual_order": int(segment.get("manual_order") or 999999),
                })
        except (OSError, ValueError, TypeError):
            project_intervals = []
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            videos = [dict(row) for row in con.execute("select id, current_path, filename from videos where id in (select video_id from project_videos where project_id=?)", (project_id,))]
            video_map = {int(row["id"]): row for row in videos}
            for row in con.execute("select video_id, start_seconds, end_seconds, title, reason, score from segments where video_id in (select video_id from project_videos where project_id=?)", (project_id,)):
                video = video_map.get(int(row["video_id"]), {})
                start = float(row["start_seconds"] or 0)
                end = float(row["end_seconds"] or start)
                mapped = _map_project_interval(project_intervals, int(row["video_id"]), start)
                if mapped and (not mapped["enabled"] or mapped["excluded"]):
                    continue
                reference_id = str(mapped["segment_id"]) if mapped else f"segment:{row['video_id']}:{round(start * 1000)}"
                result.append({"id": f"segment:{row['video_id']}:{round(start * 1000)}", "segment_id": reference_id, "type": "segment", "video_id": int(row["video_id"]), "source_file": str(video.get("current_path") or ""), "timestamp_seconds": round((start + end) / 2, 3), "start_seconds": start, "end_seconds": end, "label": str(row["title"] or row["reason"] or ""), "score": float(row["score"] or 0)})
            for row in con.execute("select video_id, timestamp_seconds, vision_summary, score_usefulness from frames where video_id in (select video_id from project_videos where project_id=?)", (project_id,)):
                video = video_map.get(int(row["video_id"]), {})
                timestamp = float(row["timestamp_seconds"] or 0)
                mapped = _map_project_interval(project_intervals, int(row["video_id"]), timestamp)
                if mapped and (not mapped["enabled"] or mapped["excluded"]):
                    continue
                candidate = {"id": f"frame:{row['video_id']}:{round(timestamp * 1000)}", "type": "frame", "video_id": int(row["video_id"]), "source_file": str(video.get("current_path") or ""), "timestamp_seconds": timestamp, "label": str(row["vision_summary"] or ""), "score": float(row["score_usefulness"] or 0)}
                if mapped:
                    candidate["segment_id"] = str(mapped["segment_id"])
                result.append(candidate)
    except sqlite3.Error:
        return []
    return sorted(result, key=lambda item: (-float(item.get("score") or 0), int(item.get("video_id") or 0), float(item.get("timestamp_seconds") or 0), str(item.get("id") or "")))


def _map_project_interval(intervals: list[Mapping[str, Any]], video_id: int, timestamp: float) -> Mapping[str, Any] | None:
    matches = [
        interval for interval in intervals
        if int(interval.get("video_id") or 0) == video_id
        and float(interval.get("start_seconds") or 0) <= timestamp <= float(interval.get("end_seconds") or 0)
    ]
    return min(
        matches,
        key=lambda interval: (
            float(interval.get("end_seconds") or 0) - float(interval.get("start_seconds") or 0),
            int(interval.get("manual_order") or 999999),
            str(interval.get("segment_id") or ""),
        ),
    ) if matches else None


def _keep_or_select_reference(state: Mapping[str, Any], references: list[dict[str, Any]]) -> dict[str, Any]:
    current = str((state.get("reference") or {}).get("id") or "")
    return next((item for item in references if item["id"] == current), references[0] if references else {})


def _is_excluded_reference(item: Mapping[str, Any], excluded_ids: set[str]) -> bool:
    if str(item.get("id") or "") in excluded_ids or str(item.get("segment_id") or "") in excluded_ids:
        return True
    if item.get("type") != "segment":
        return False
    video_id = str(item.get("video_id") or "")
    start = _finite_number(item.get("start_seconds"), -1)
    return any(key.startswith(f"{video_id}:") and abs(_finite_number(key.rsplit(":", 1)[-1], -2) / 1000 - start) <= 0.001 for key in excluded_ids)


def _reference_luma(cfg: dict, reference: Mapping[str, Any]) -> dict[str, Any]:
    from .color import _brightness_stats_at
    source = Path(str(reference.get("source_file") or "")).expanduser().resolve()
    if not source.is_file():
        raise ColorReferenceError("source_missing", f"原始素材不存在：{source}")
    timestamp = _finite_number(reference.get("timestamp_seconds"), -1.0)
    if timestamp < 0:
        raise ColorReferenceError("invalid_timestamp", "Reference timestamp 無效")
    duration = _probe_duration(cfg, source)
    if duration is not None and timestamp > duration + 0.001:
        raise ColorReferenceError("timestamp_out_of_range", f"Reference timestamp {timestamp:.3f} 超過素材長度 {duration:.3f}")
    return _merge_color_stats(_brightness_stats_at(source, cfg, timestamp), source, cfg, timestamp)


def _confidence(stats: Mapping[str, Any], has_reference: bool) -> str:
    if not has_reference:
        return "low"
    if int(stats.get("sampled_frames") or 0) and float(stats.get("highlight_ratio") or 0) < 0.3:
        return "medium"
    return "low"


def _empty_color_stats() -> dict[str, Any]:
    return {"average": 128.0, "highlight_ratio": 0.0, "shadow_ratio": 0.0, "sampled_frames": 0, "sampled_pixel_count": 0, "sampled_frame_count": 0, "saturation_tendency": 0.0, "white_balance_tendency": "unknown"}


def _finite_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _finite_confidence(value: Any) -> float:
    return round(min(1.0, max(0.0, _finite_number(value, 0.0))), 3)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _probe_duration(cfg: dict, source: Path) -> float | None:
    try:
        cmd = [str(cfg.get("ffprobe_path") or "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
        value = float((result.stdout or "").strip())
        return value if math.isfinite(value) and value >= 0 else None
    except (OSError, ValueError):
        return None


def _safe_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return token.strip("._")[:100] or "preview"


def _render_preview_pair(render_preview, source: Path, before: Path, after: Path, cfg: dict, effective: Mapping[str, Any], start: float, duration: float) -> None:
    try:
        render_preview(source, before, cfg, color_settings={"mode": "none"}, seconds=duration, start_seconds=start)
        render_preview(source, after, cfg, color_settings=dict(effective), seconds=duration, start_seconds=start)
    except Exception:
        before.unlink(missing_ok=True)
        after.unlink(missing_ok=True)
        raise


def _preview_url(project_id: int, token: str) -> str:
    from urllib.parse import quote
    return f"/api/project/color-preview-file?project_id={int(project_id)}&file={quote(str(token), safe='') }"


def _preview_urls(cfg: dict, project_id: int, item: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(item), "before_url": _preview_url(project_id, str(item["before"])), "after_url": _preview_url(project_id, str(item["after"]))}


def _reference_url(reference: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    result = dict(reference)
    result.pop("frame_path", None)
    source_file = result.pop("source_file", None)
    if source_file:
        result["source_name"] = Path(str(source_file)).name
    if result.get("frame_name"):
        from urllib.parse import quote
        result["frame_url"] = f"/api/project/color-reference-file?project_id={int(project_id)}&file={quote(str(result['frame_name']), safe='')}"
    return result


def _safe_project_media_path(root: Path, token: str) -> Path:
    name = str(token or "")
    candidate = Path(name)
    if not name or candidate.name != name or candidate.is_absolute() or ".." in candidate.parts:
        raise FileNotFoundError("media token 無效")
    root = root.resolve()
    resolved = (root / name).resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise FileNotFoundError("找不到指定預覽檔")
    return resolved


def _lut_warnings(settings: Mapping[str, Any]) -> list[str]:
    mode = str(settings.get("mode") or "none")
    path = Path(str(settings.get("lut_path") or "")).expanduser()
    if mode in LUT_MODES and not path.is_file():
        return [f"LUT 檔案不存在：{path.resolve()}"]
    return []


def _lut_cache_fingerprint(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    lut_value = str(settings.get("lut_path") or "").strip()
    if not lut_value:
        return None
    lut = Path(lut_value).expanduser().resolve()
    try:
        stat = lut.stat()
    except OSError:
        return {"path": str(lut), "size": None, "mtime_ns": None, "sha256": None}
    digest = hashlib.sha256()
    try:
        with lut.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return {"path": str(lut), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": None}
    return {"path": str(lut), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def _source_cache_fingerprint(source: Path) -> dict[str, Any]:
    try:
        stat = source.stat()
    except OSError:
        return {"path": str(source), "size": None, "mtime_ns": None}
    return {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cache_number(value: Any) -> float | int | None:
    if value is None:
        return None
    number = _finite_number(value, math.nan)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _analyze_segments(cfg: dict, db: Path, project_id: int, existing: Mapping[str, Any], reference: Mapping[str, Any], reference_stats: Mapping[str, Any], project_applied: Mapping[str, Any], *, preserve_manual_segments: bool = False) -> dict[str, Any]:
    from .project import project_dir, project_segments

    plan_path = project_dir(cfg, project_id) / "project_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    old_segments = dict(existing.get("segments") or {})
    result: dict[str, Any] = {}
    ref_luma = float(reference_stats.get("average") or 128)
    segments = project_segments(cfg, project_id, plan)
    planned_ids = {str(segment.get("segment_id") or "") for segment in segments}
    if preserve_manual_segments:
        segments.extend({**dict(reference), "segment_id": segment_id, "_fallback_reference": True} for segment_id in old_segments if segment_id not in planned_ids)
    for segment in segments:
        segment_id = str(segment.get("segment_id") or "")
        if not segment_id:
            continue
        old = dict(old_segments.get(segment_id) or {})
        if preserve_manual_segments and _as_bool(old.get("locked", False)):
            result[segment_id] = deepcopy(old)
            continue
        excluded = _as_bool(old.get("excluded", False))
        if excluded:
            result[segment_id] = _excluded_segment_state(old, project_applied)
            continue
        if segment.get("_fallback_reference"):
            candidate = dict(reference)
        else:
            candidate = {
                "source_file": str(segment.get("source_file") or _project_video_source(db, segment.get("video_id")) or reference.get("source_file") or ""),
                "timestamp_seconds": (float(segment.get("start_seconds") or 0) + float(segment.get("end_seconds") or 0)) / 2,
            }
        try:
            stats = _reference_luma(cfg, candidate)
            confidence = _numeric_confidence(stats, reference_stats, bool(reference))
            suggested = _suggested_adjustments(cfg, project_id, stats, existing)
            suggested["highlights"] = _clamp((float(reference_stats.get("highlight_ratio") or 0) - float(stats.get("highlight_ratio") or 0)) * 2, -1, 1)
            suggested["shadows"] = _clamp((ref_luma - float(stats.get("average") or 128)) / 128, -1, 1)
            warnings = _stats_warnings(stats)
        except ColorReferenceError as exc:
            stats = _empty_color_stats()
            confidence = 0.0
            suggested = deepcopy(project_applied)
            warnings = [str(exc)]
        locked = _as_bool(old.get("locked", False))
        if locked:
            suggested = deepcopy(old.get("suggested") if isinstance(old.get("suggested"), Mapping) else suggested)
            applied = deepcopy(old.get("applied") if isinstance(old.get("applied"), Mapping) else project_applied)
        else:
            applied = deepcopy(old.get("applied") if isinstance(old.get("applied"), Mapping) else (suggested if confidence >= 0.55 else project_applied))
        result[segment_id] = {
            "enabled": _as_bool(old.get("enabled", True)),
            "locked": locked,
            "excluded": False,
            "reference_candidate": True,
            "suggested": suggested,
            "applied": applied,
            "confidence": confidence,
            "warnings": warnings,
        }
    return result


def _excluded_segment_state(old: Mapping[str, Any], project_applied: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(old))
    result["enabled"] = _as_bool(old.get("enabled", True))
    result["locked"] = _as_bool(old.get("locked", False))
    result["excluded"] = True
    result["reference_candidate"] = False
    result["suggested"] = deepcopy(old.get("suggested") if isinstance(old.get("suggested"), Mapping) else project_applied)
    result["applied"] = deepcopy(old.get("applied") if isinstance(old.get("applied"), Mapping) else project_applied)
    warnings = [str(warning) for warning in old.get("warnings", []) or []]
    if _EXCLUDED_SEGMENT_WARNING not in warnings:
        warnings.append(_EXCLUDED_SEGMENT_WARNING)
    result["warnings"] = warnings
    result["confidence"] = 0.0
    return result


def _project_video_source(db: Path, video_id: Any) -> str:
    try:
        with sqlite3.connect(db) as con:
            row = con.execute("select current_path from videos where id=?", (int(video_id),)).fetchone()
        return str(row[0]) if row and row[0] else ""
    except (sqlite3.Error, TypeError, ValueError):
        return ""


def _numeric_confidence(stats: Mapping[str, Any], reference: Mapping[str, Any], has_reference: bool) -> float:
    if not has_reference:
        return 0.0
    average_delta = abs(float(stats.get("average") or 128) - float(reference.get("average") or 128)) / 128
    clipping = float(stats.get("highlight_ratio") or 0) + float(stats.get("shadow_ratio") or 0)
    return round(max(0.0, min(1.0, 0.9 - average_delta - clipping)), 3)


def _stats_warnings(stats: Mapping[str, Any]) -> list[str]:
    warnings = []
    if float(stats.get("highlight_ratio") or 0) > 0.2:
        warnings.append("高光裁切偏高，建議不要過度拉亮")
    if float(stats.get("shadow_ratio") or 0) > 0.2:
        warnings.append("陰影裁切偏高，暗部細節可能不足")
    if float(stats.get("saturation_tendency") or 0) > 0.9:
        warnings.append("高飽和像素比例偏高")
    return warnings


def _merge_color_stats(stats: Mapping[str, Any], source: Path, cfg: dict, timestamp: float) -> dict[str, Any]:
    rgb = _rgb_stats(source, cfg, timestamp)
    result = {**dict(stats), **rgb}
    result.setdefault("luminance_percentile", {"p10": result.get("average", 128), "p50": result.get("average", 128), "p90": result.get("average", 128)})
    result.setdefault("luminance_range", [result.get("average", 128), result.get("average", 128)])
    result.setdefault("shadow_ratio", 0.0)
    result.setdefault("saturation_tendency", 0.0)
    result.setdefault("sampled_pixel_count", 0)
    result.setdefault("sampled_frame_count", result.get("sampled_frames", 0))
    return result


def _rgb_stats(source: Path, cfg: dict, timestamp: float) -> dict[str, Any]:
    cmd = [str(cfg.get("ffmpeg_path") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(source), "-vf", "scale=160:-1,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
        data = proc.stdout
        pixels = [data[index:index + 3] for index in range(0, len(data) - 2, 3)]
        usable = []
        for red, green, blue in pixels:
            values = [red, green, blue]
            luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            chroma = (max(values) - min(values)) / max(max(values), 1)
            if 8 <= luminance <= 247 and chroma <= 0.98:
                usable.append((luminance, red, green, blue, chroma))
        if not usable:
            return {"rgb_mean": {"r": 0, "g": 0, "b": 0}, "sampled_pixel_count": 0, "saturation_tendency": 0.0, "white_balance_tendency": "unknown"}
        luminances = sorted(item[0] for item in usable)
        r_mean = sum(item[1] for item in usable) / len(usable)
        g_mean = sum(item[2] for item in usable) / len(usable)
        b_mean = sum(item[3] for item in usable) / len(usable)
        return {"rgb_mean": {"r": round(r_mean, 2), "g": round(g_mean, 2), "b": round(b_mean, 2)}, "luminance_percentile": {"p10": round(_percentile(luminances, 0.1), 2), "p50": round(_percentile(luminances, 0.5), 2), "p90": round(_percentile(luminances, 0.9), 2)}, "luminance_range": [round(luminances[0], 2), round(luminances[-1], 2)], "saturation_tendency": round(sum(item[4] for item in usable) / len(usable), 4), "white_balance_tendency": _white_balance_tendency(r_mean, g_mean, b_mean), "sampled_pixel_count": len(usable), "sampled_frame_count": 1}
    except (OSError, ValueError):
        return {"rgb_mean": {"r": 0, "g": 0, "b": 0}, "sampled_pixel_count": 0, "saturation_tendency": 0.0, "white_balance_tendency": "unknown"}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))
    return values[index]


def _white_balance_tendency(red: float, green: float, blue: float) -> str:
    if red > blue * 1.08:
        return "偏暖"
    if blue > red * 1.08:
        return "偏冷"
    if green > (red + blue) / 2 * 1.08:
        return "偏綠"
    return "中性"


def _basis_text(reference: Mapping[str, Any] | None, stats: Mapping[str, Any]) -> str:
    label = str((reference or {}).get("label") or "核心畫面")
    return f"以「{label}」作為色彩基準；平均亮度 {float(stats.get('average') or 0):.1f}，高光比例 {float(stats.get('highlight_ratio') or 0):.1%}。建議值僅在安全範圍內微調，仍需人工確認。"


def _project_source_ids(db: Path, project_id: int) -> list[int]:
    try:
        with sqlite3.connect(db) as con:
            return [int(row[0]) for row in con.execute("select video_id from project_videos where project_id=?", (project_id,))]
    except sqlite3.Error:
        return []


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), dict(value))
        else:
            result[key] = value
    return result


def _user_editable_color_patch(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _USER_EDITABLE_COLOR_FIELDS:
        if key not in patch:
            continue
        value = patch[key]
        if key in {"suggested", "applied"}:
            if isinstance(value, Mapping):
                result[key] = deepcopy(dict(value))
        elif key == "segments":
            if not isinstance(value, Mapping):
                continue
            segments: dict[str, Any] = {}
            for segment_id, segment_patch in value.items():
                if not isinstance(segment_patch, Mapping):
                    continue
                editable: dict[str, Any] = {}
                for field in _USER_EDITABLE_SEGMENT_FIELDS:
                    if field not in segment_patch:
                        continue
                    field_value = segment_patch[field]
                    if field in {"suggested", "applied"}:
                        if isinstance(field_value, Mapping):
                            editable[field] = deepcopy(dict(field_value))
                    else:
                        editable[field] = deepcopy(field_value)
                if editable:
                    segments[str(segment_id)] = editable
            result[key] = segments
        else:
            result[key] = deepcopy(value)
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


__all__ = [
    "ADJUSTMENT_DEFAULTS", "ADJUSTMENT_LIMITS", "COLOR_MODES", "COLOR_STATE_VERSION", "ColorReferenceError", "LUT_MODES",
    "analyze_project_color", "color_state_path", "default_color_state", "effective_color_settings",
    "color_dir", "color_state_for_api", "ensure_reference_frame", "has_color_state", "load_project_color_state", "normalize_color_state", "preview_cache_key",
    "preview_file_path", "reference_file_path",
    "render_project_color_previews", "save_project_color_state", "set_color_reference", "update_color_state",
]
