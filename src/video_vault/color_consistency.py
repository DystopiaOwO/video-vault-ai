"""Project-level color consistency state, analysis, and preview helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .project import mark_project_needs_review, project_dir
from .render_settings import load_render_settings


COLOR_STATE_VERSION = 1
ADJUSTMENT_DEFAULTS: dict[str, float] = {
    "exposure": 0.0,
    "temperature": 0.0,
    "tint": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gamma": 1.0,
}
ADJUSTMENT_LIMITS: dict[str, tuple[float, float]] = {
    "exposure": (-1.5, 1.0),
    "temperature": (-30.0, 30.0),
    "tint": (-20.0, 20.0),
    "contrast": (0.85, 1.15),
    "saturation": (0.8, 1.2),
    "gamma": (0.85, 1.15),
}
COLOR_MODES = frozenset({"none", "safe_restore", "warm_food", "manual", "dji_lut", "dji_dlog", "dji_dlog_m"})
LUT_MODES = frozenset({"dji_lut", "dji_dlog", "dji_dlog_m"})


def color_state_path(cfg: dict, project_id: int) -> Path:
    return project_dir(cfg, project_id) / "color_consistency.json"


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
        if isinstance(item.get("suggested"), Mapping):
            item["suggested"] = _normalize_adjustment_set(item["suggested"], result["suggested"])
        if isinstance(item.get("applied"), Mapping):
            item["applied"] = _normalize_adjustment_set(item["applied"], result["applied"])
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
        elif isinstance(override.get("applied"), Mapping) and not override.get("locked", False):
            result.update(override["applied"])
    return result


def analyze_project_color(cfg: dict, db: Path, project_id: int, *, force: bool = False) -> dict[str, Any]:
    existing = load_project_color_state(cfg, project_id)
    if existing.get("analysis") and not force:
        return existing
    references = _reference_candidates(db, project_id)
    selected = _keep_or_select_reference(existing, references)
    stats = _reference_luma(cfg, selected) if selected else {"average": 128.0, "highlight_ratio": 0.0, "sampled_frames": 0}
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
        },
        "suggested": suggested,
        "applied": applied,
    })
    save_project_color_state(cfg, db, project_id, state)
    return state


def set_color_reference(cfg: dict, db: Path, project_id: int, reference_id: str) -> dict[str, Any]:
    state = load_project_color_state(cfg, project_id)
    selected = next((item for item in state["references"] if str(item.get("id")) == str(reference_id)), None)
    if not selected:
        raise ValueError(f"找不到色彩基準畫面：{reference_id}")
    stats = _reference_luma(cfg, selected)
    suggested = _suggested_adjustments(cfg, project_id, stats, state)
    suggested.update({"mode": state["suggested"].get("mode", "none"), "lut_path": state["suggested"].get("lut_path", ""), "lut_kind": state["suggested"].get("lut_kind", "")})
    state = normalize_color_state({**state, "reference": selected, "analysis": {**state.get("analysis", {}), "reference": selected, "luma": stats, "basis_text": _basis_text(selected, stats)}, "suggested": suggested})
    save_project_color_state(cfg, db, project_id, state)
    return state


def update_color_state(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any]) -> dict[str, Any]:
    current = load_project_color_state(cfg, project_id)
    updated = _merge(current, dict(patch))
    state = normalize_color_state(updated)
    save_project_color_state(cfg, db, project_id, state)
    return state


def preview_cache_key(source: Path, state: Mapping[str, Any], settings: Mapping[str, Any] | None = None, kind: str = "after") -> str:
    path = source.expanduser().resolve()
    stat = path.stat() if path.exists() else None
    payload = {
        "source": str(path),
        "size": stat.st_size if stat else None,
        "mtime_ns": stat.st_mtime_ns if stat else None,
        "state": normalize_color_state(state),
        "settings": dict(settings or {}),
        "kind": kind,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def render_project_color_previews(cfg: dict, db: Path, project_id: int, *, force: bool = False, seconds: int = 20) -> dict[str, Any]:
    from .color import render_color_preview
    from .database import project_videos

    state = load_project_color_state(cfg, project_id)
    if not state.get("analysis"):
        state = analyze_project_color(cfg, db, project_id)
    out_dir = project_dir(cfg, project_id) / "output" / "color_previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    previews = []
    for video in project_videos(db, project_id):
        source = Path(str(video["current_path"])).expanduser().resolve()
        key = preview_cache_key(source, state, kind="before-after")
        before = out_dir / f"video_{video['id']}_before_{key[:12]}.mp4"
        after = out_dir / f"video_{video['id']}_after_{key[:12]}.mp4"
        metadata_path = out_dir / f"video_{video['id']}_{key[:12]}.json"
        cached = before.is_file() and after.is_file() and metadata_path.is_file()
        if force or not cached:
            render_color_preview(source, before, cfg, color_settings={"mode": "none"}, seconds=seconds)
            render_color_preview(source, after, cfg, color_settings=effective_color_settings(state), seconds=seconds)
            metadata_path.write_text(json.dumps({"cache_key": key, "source": str(source), "reference": state.get("reference", {}), "applied": state.get("applied", {}), "before": str(before), "after": str(after)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            cached = False
        previews.append({"video_id": int(video["id"]), "source_file": str(source), "before": str(before), "after": str(after), "cache_key": key, "cache_hit": cached})
    return {"ok": True, "state": state, "previews": previews, "files": [item["after"] for item in previews]}


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


def _reference_candidates(db: Path, project_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            videos = [dict(row) for row in con.execute("select id, current_path, filename from videos where id in (select video_id from project_videos where project_id=?)", (project_id,))]
            video_map = {int(row["id"]): row for row in videos}
            for row in con.execute("select video_id, start_seconds, end_seconds, title, reason, score from segments where video_id in (select video_id from project_videos where project_id=?)", (project_id,)):
                video = video_map.get(int(row["video_id"]), {})
                start = float(row["start_seconds"] or 0)
                end = float(row["end_seconds"] or start)
                result.append({"id": f"segment:{row['video_id']}:{round(start * 1000)}", "type": "segment", "video_id": int(row["video_id"]), "source_file": str(video.get("current_path") or ""), "timestamp_seconds": round((start + end) / 2, 3), "start_seconds": start, "end_seconds": end, "label": str(row["title"] or row["reason"] or ""), "score": float(row["score"] or 0)})
            for row in con.execute("select video_id, timestamp_seconds, vision_summary, score_usefulness from frames where video_id in (select video_id from project_videos where project_id=?)", (project_id,)):
                video = video_map.get(int(row["video_id"]), {})
                timestamp = float(row["timestamp_seconds"] or 0)
                result.append({"id": f"frame:{row['video_id']}:{round(timestamp * 1000)}", "type": "frame", "video_id": int(row["video_id"]), "source_file": str(video.get("current_path") or ""), "timestamp_seconds": timestamp, "label": str(row["vision_summary"] or ""), "score": float(row["score_usefulness"] or 0)})
    except sqlite3.Error:
        return []
    return sorted(result, key=lambda item: (-float(item.get("score") or 0), int(item.get("video_id") or 0), float(item.get("timestamp_seconds") or 0)))


def _keep_or_select_reference(state: Mapping[str, Any], references: list[dict[str, Any]]) -> dict[str, Any]:
    current = str((state.get("reference") or {}).get("id") or "")
    return next((item for item in references if item["id"] == current), references[0] if references else {})


def _reference_luma(cfg: dict, reference: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from .color import _brightness_stats_at, _brightness_stats
        source = Path(str(reference.get("source_file") or ""))
        if source.is_file():
            timestamp = float(reference.get("timestamp_seconds") or 0)
            return _brightness_stats_at(source, cfg, timestamp)
    except Exception:
        pass
    return {"average": 128.0, "highlight_ratio": 0.0, "sampled_frames": 0}


def _confidence(stats: Mapping[str, Any], has_reference: bool) -> str:
    if not has_reference:
        return "low"
    if int(stats.get("sampled_frames") or 0) and float(stats.get("highlight_ratio") or 0) < 0.3:
        return "medium"
    return "low"


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


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


__all__ = [
    "ADJUSTMENT_DEFAULTS", "ADJUSTMENT_LIMITS", "COLOR_MODES", "COLOR_STATE_VERSION", "LUT_MODES",
    "analyze_project_color", "color_state_path", "default_color_state", "effective_color_settings",
    "has_color_state", "load_project_color_state", "normalize_color_state", "preview_cache_key",
    "render_project_color_previews", "save_project_color_state", "set_color_reference", "update_color_state",
]
