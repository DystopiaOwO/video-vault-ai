"""Project-level storyboard state, thumbnails, and short review previews."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

STORYBOARD_SCHEMA_VERSION = 1
THUMBNAIL_CONTRACT_VERSION = 1
THUMBNAIL_RATIOS = (0.25, 0.5, 0.75)


def storyboard_path(cfg: dict, project_id: int) -> Path:
    from .project import project_dir

    return project_dir(cfg, project_id) / "storyboard.json"


def storyboard_cache_dir(cfg: dict, project_id: int) -> Path:
    from .project import project_dir

    path = project_dir(cfg, project_id) / "cache" / "storyboard"
    path.mkdir(parents=True, exist_ok=True)
    return path


def storyboard_preview_dir(cfg: dict, project_id: int) -> Path:
    from .project import project_dir

    path = project_dir(cfg, project_id) / "output" / "storyboard_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_storyboard() -> dict[str, Any]:
    return {"schema_version": STORYBOARD_SCHEMA_VERSION, "groups": [], "segments": {}}


def load_storyboard(cfg: dict, project_id: int) -> dict[str, Any] | None:
    path = storyboard_path(cfg, project_id)
    if not path.is_file():
        return None
    try:
        return normalize_storyboard(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def normalize_storyboard(state: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(state or {})
    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for index, raw in enumerate(source.get("groups") or [], 1):
        if not isinstance(raw, Mapping):
            continue
        group_id = str(raw.get("group_id") or f"group_{index:03d}")
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        groups.append({
            "group_id": group_id,
            "title": str(raw.get("title") or group_id),
            "category": str(raw.get("category") or "custom"),
            "order": _positive_int(raw.get("order"), index),
        })
    groups.sort(key=lambda item: (item["order"], item["group_id"]))
    for index, group in enumerate(groups, 1):
        group["order"] = index

    segments: dict[str, dict[str, Any]] = {}
    for segment_id, raw in (source.get("segments") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        key = str(segment_id)
        segments[key] = {
            "group_id": str(raw.get("group_id") or ""),
            "order": _positive_int(raw.get("order"), 1),
            "included": bool(raw.get("included", True)),
            "locked": bool(raw.get("locked", False)),
            "thumbnail_time_ratio": _ratio(raw.get("thumbnail_time_ratio", 0.5)),
            "notes": str(raw.get("notes") or ""),
        }
    return {"schema_version": STORYBOARD_SCHEMA_VERSION, "groups": groups, "segments": segments}


def generate_storyboard(cfg: dict, db: Path, project_id: int, *, force: bool = False) -> dict[str, Any]:
    from .project import _read_json, project, project_segments

    plan = _read_json(Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}" / "project_plan.json")
    existing = load_storyboard(cfg, project_id) or default_storyboard()
    rows = project_segments(cfg, project_id, plan, apply_storyboard=False)
    project_row = dict(project(db, project_id) or {})
    groups = _suggest_groups(plan, project_row, rows)
    old_groups = {item["group_id"]: item for item in existing.get("groups", [])}
    merged_groups: list[dict[str, Any]] = []
    for group in groups:
        merged_groups.append({**group, **old_groups.get(group["group_id"], {})})
    for group in existing.get("groups", []):
        if group["group_id"] not in {item["group_id"] for item in merged_groups}:
            merged_groups.append(group)

    old_segments = existing.get("segments", {})
    segment_state: dict[str, dict[str, Any]] = {}
    group_ids = {item["group_id"] for item in merged_groups}
    counters: dict[str, int] = {}
    for row in rows:
        segment_id = str(row["segment_id"])
        suggested_group = _group_id_for_row(row, groups)
        previous = old_segments.get(segment_id)
        if isinstance(previous, Mapping):
            item = dict(previous)
            if item.get("group_id") not in group_ids:
                item["group_id"] = suggested_group
        else:
            item = {}
        group_id = str(item.get("group_id") or suggested_group)
        if group_id not in group_ids:
            group_id = suggested_group
        counters[group_id] = counters.get(group_id, 0) + 1
        segment_state[segment_id] = {
            "group_id": group_id,
            "order": _positive_int(item.get("order"), counters[group_id]),
            "included": bool(item.get("included", row.get("include", True))),
            "locked": bool(item.get("locked", False)),
            "thumbnail_time_ratio": _ratio(item.get("thumbnail_time_ratio", 0.5)),
            "notes": str(item.get("notes", row.get("user_notes", "")) or ""),
        }

    state = normalize_storyboard({"groups": merged_groups, "segments": segment_state})
    save_storyboard(cfg, db, project_id, state, mark_review=True)
    thumbnail_errors: list[str] = []
    for row in rows:
        try:
            generate_thumbnail(cfg, db, project_id, str(row["segment_id"]), float(segment_state[str(row["segment_id"])]["thumbnail_time_ratio"]))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            thumbnail_errors.append(f"{row['segment_id']}: {exc}")
    from .project import append_decision

    append_decision(cfg, project_id, "storyboard_generated", "建立分鏡分組與順序", "storyboard", reason="；".join(thumbnail_errors), affected_segments=list(segment_state))
    return state


def ensure_storyboard(cfg: dict, db: Path, project_id: int) -> dict[str, Any]:
    state = load_storyboard(cfg, project_id)
    if state is None:
        return generate_storyboard(cfg, db, project_id, force=False) if _has_plan(cfg, project_id) else default_storyboard()
    return state


def save_storyboard(cfg: dict, db: Path, project_id: int, state: Mapping[str, Any], *, mark_review: bool = True) -> Path:
    from .project import mark_project_needs_review

    normalized = normalize_storyboard(state)
    path = storyboard_path(cfg, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def update_storyboard(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any]) -> dict[str, Any]:
    current = load_storyboard(cfg, project_id) or generate_storyboard(cfg, db, project_id, force=False)
    incoming = patch.get("state") if isinstance(patch.get("state"), Mapping) else patch
    updated = normalize_storyboard({
        "groups": incoming.get("groups", current["groups"]),
        "segments": incoming.get("segments", current["segments"]),
    })
    rows = _raw_project_segments(cfg, db, project_id)
    validation = validate_storyboard(updated, rows)
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    save_storyboard(cfg, db, project_id, updated, mark_review=True)
    return updated


def apply_storyboard_state(rows: list[dict[str, Any]], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups = {str(item["group_id"]): item for item in state.get("groups", []) if isinstance(item, Mapping)}
    configured = state.get("segments") or {}
    ranked: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for fallback_index, row in enumerate(rows, 1):
        item = dict(row)
        entry = configured.get(str(item["segment_id"])) if isinstance(configured, Mapping) else None
        if isinstance(entry, Mapping):
            group_id = str(entry.get("group_id") or "")
            group = groups.get(group_id, {})
            item["include"] = bool(entry.get("included", item.get("include", True)))
            item["storyboard_group_id"] = group_id
            item["storyboard_order"] = _positive_int(entry.get("order"), fallback_index)
            item["storyboard_locked"] = bool(entry.get("locked", False))
            item["storyboard_notes"] = str(entry.get("notes") or "")
            item["thumbnail_time_ratio"] = _ratio(entry.get("thumbnail_time_ratio", 0.5))
            item["group"] = str(group.get("title") or item.get("group") or "")
            item["group_order"] = _positive_int(group.get("order"), item.get("group_order", 999))
        ranked.append(((int(item.get("group_order") or 999), int(item.get("storyboard_order") or fallback_index), str(item["segment_id"])), item))
    ranked.sort(key=lambda pair: pair[0])
    result: list[dict[str, Any]] = []
    for order, (_, item) in enumerate(ranked, 1):
        item["manual_order"] = order
        result.append(item)
    return result


def validate_storyboard(state: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    groups = list(state.get("groups") or [])
    group_ids = [str(item.get("group_id")) for item in groups if isinstance(item, Mapping)]
    if len(group_ids) != len(set(group_ids)):
        errors.append("Storyboard 群組 ID 重複")
    group_orders = [int(item.get("order", 0)) for item in groups if isinstance(item, Mapping)]
    if len(group_orders) != len(set(group_orders)):
        errors.append("Storyboard 群組順序重複")
    configured = state.get("segments") or {}
    row_ids = {str(row.get("segment_id")) for row in rows}
    included = 0
    orders: list[tuple[str, int]] = []
    for segment_id in row_ids:
        item = configured.get(segment_id)
        if not isinstance(item, Mapping):
            errors.append(f"Storyboard 缺少片段：{segment_id}")
            continue
        group_id = str(item.get("group_id") or "")
        if group_id not in group_ids:
            errors.append(f"Storyboard 片段群組不存在：{segment_id}")
        order = int(item.get("order", 0))
        if order <= 0:
            errors.append(f"Storyboard 片段順序無效：{segment_id}")
        orders.append((group_id, order))
        if bool(item.get("included", True)):
            included += 1
    if len(orders) != len(set(orders)):
        errors.append("Storyboard 群組內片段順序重複")
    if row_ids and included == 0:
        errors.append("Storyboard 至少需要一個 included segment")
    return {"valid": not errors, "errors": errors, "warnings": []}


def storyboard_for_api(cfg: dict, db: Path, project_id: int) -> dict[str, Any]:
    state = load_storyboard(cfg, project_id) or default_storyboard()
    rows = _raw_project_segments(cfg, db, project_id)
    result = json.loads(json.dumps(state, ensure_ascii=False))
    for segment_id, item in result["segments"].items():
        thumbnail = thumbnail_path_for_state(cfg, db, project_id, segment_id, item.get("thumbnail_time_ratio", 0.5))
        if thumbnail and thumbnail.is_file():
            item["thumbnail_url"] = f"/api/project/storyboard-thumbnail-file?project_id={project_id}&file={thumbnail.name}"
    result["summary"] = storyboard_summary(rows, state)
    result["validation"] = validate_storyboard(state, rows)
    result["exists"] = storyboard_path(cfg, project_id).is_file()
    return result


def storyboard_summary(rows: list[dict[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
    configured = state.get("segments") or {}
    included_rows = [row for row in rows if bool((configured.get(str(row["segment_id"])) or {}).get("included", row.get("include", True)))]
    excluded_rows = [row for row in rows if row not in included_rows]
    duration = lambda row: max(0.0, float(row.get("end_seconds") or 0) - float(row.get("start_seconds") or 0)) / max(0.01, float(row.get("speed") or 1.0))
    group_summary: dict[str, dict[str, Any]] = {}
    for row in included_rows:
        item = configured.get(str(row["segment_id"])) or {}
        group_id = str(item.get("group_id") or "")
        group_summary.setdefault(group_id, {"group_id": group_id, "count": 0, "duration_seconds": 0.0})
        group_summary[group_id]["count"] += 1
        group_summary[group_id]["duration_seconds"] += duration(row)
    roles: dict[str, int] = {}
    for row in included_rows:
        role = str(row.get("audio_role") or "lower_original")
        roles[role] = roles.get(role, 0) + 1
    return {
        "total_segments": len(rows),
        "included_segments": len(included_rows),
        "excluded_segments": len(excluded_rows),
        "estimated_duration_seconds": round(sum(duration(row) for row in included_rows), 3),
        "groups": list(group_summary.values()),
        "audio_roles": roles,
    }


def generate_thumbnail(cfg: dict, db: Path, project_id: int, segment_id: str, ratio: float = 0.5, *, force: bool = False) -> dict[str, Any]:
    from .project import _read_json, project_segments

    ratio = _ratio(ratio)
    rows = project_segments(cfg, project_id, _read_json(_project_plan(cfg, project_id)))
    row = next((item for item in rows if str(item["segment_id"]) == str(segment_id)), None)
    if not row:
        raise ValueError("找不到指定片段")
    source = Path(str(row.get("source_file") or "")).expanduser().resolve()
    if not source.is_file():
        raise ValueError("片段來源不存在")
    path = _thumbnail_path(cfg, project_id, source, float(row["start_seconds"]), float(row["end_seconds"]), ratio)
    if path.is_file() and path.stat().st_size > 0 and not force:
        return {"file": path.name, "cache_hit": True, "ratio": ratio}
    duration = max(0.1, float(row["end_seconds"]) - float(row["start_seconds"]))
    timestamp = float(row["start_seconds"]) + duration * ratio
    partial = path.with_name(f".{path.stem}.partial.jpg")
    command = [str(cfg.get("ffmpeg_path") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{timestamp:.6f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(partial)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode != 0 or not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError((result.stderr or result.stdout or "FFmpeg thumbnail failed").strip())
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return {"file": path.name, "cache_hit": False, "ratio": ratio}


def thumbnail_path_for_state(cfg: dict, db: Path, project_id: int, segment_id: str, ratio: float) -> Path | None:
    try:
        from .project import _read_json, project_segments

        row = next((item for item in project_segments(cfg, project_id, _read_json(_project_plan(cfg, project_id))) if str(item["segment_id"]) == str(segment_id)), None)
        if not row:
            return None
        source = Path(str(row.get("source_file") or "")).expanduser().resolve()
        return _thumbnail_path(cfg, project_id, source, float(row["start_seconds"]), float(row["end_seconds"]), _ratio(ratio))
    except (OSError, TypeError, ValueError):
        return None


def storyboard_thumbnail_path(cfg: dict, project_id: int, filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not re.fullmatch(r"[0-9a-f]{64}\.jpg", name):
        raise ValueError("invalid storyboard thumbnail filename")
    path = (storyboard_cache_dir(cfg, project_id) / name).resolve()
    root = storyboard_cache_dir(cfg, project_id).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(path)
    return path


def _thumbnail_path(cfg: dict, project_id: int, source: Path, start: float, end: float, ratio: float) -> Path:
    stat = source.stat()
    payload = {
        "contract_version": THUMBNAIL_CONTRACT_VERSION,
        "source_path": str(source),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_sha256": _sha256(source),
        "start_seconds": round(start, 6),
        "end_seconds": round(end, 6),
        "thumbnail_ratio": ratio,
    }
    key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return storyboard_cache_dir(cfg, project_id) / f"{key}.jpg"


def _suggest_groups(plan: Mapping[str, Any], project_row: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_groups = list(plan.get("groups") or [])
    if not source_groups:
        source_groups = [{"label": "未分類", "activity": "未分類", "order": 1}]
    result = []
    for index, group in enumerate(source_groups, 1):
        title = str(group.get("label") or group.get("activity") or f"分組 {index}")
        result.append({"group_id": _stable_group_id(title, index), "title": title, "category": _group_category(project_row, group), "order": index})
    return result


def _group_id_for_row(row: Mapping[str, Any], groups: list[dict[str, Any]]) -> str:
    title = str(row.get("group") or "")
    return next((item["group_id"] for item in groups if item["title"] == title), groups[0]["group_id"])


def _stable_group_id(title: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "group"
    return f"group_{index:03d}_{slug[:32]}"


def _group_category(project_row: Mapping[str, Any], group: Mapping[str, Any]) -> str:
    if str(project_row.get("content_type")) == "travel_diary":
        return "time_of_day" if str(group.get("time_of_day") or "") else "activity"
    return "activity"


def _raw_project_segments(cfg: dict, db: Path, project_id: int) -> list[dict[str, Any]]:
    from .project import _read_json, project_segments

    return project_segments(cfg, project_id, _read_json(_project_plan(cfg, project_id)), apply_storyboard=False)


def _project_plan(cfg: dict, project_id: int) -> Path:
    from .project import project_dir

    return project_dir(cfg, project_id) / "project_plan.json"


def _has_plan(cfg: dict, project_id: int) -> bool:
    return _project_plan(cfg, project_id).is_file()


def _positive_int(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return number if number > 0 else fallback


def _ratio(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    if number not in THUMBNAIL_RATIOS:
        raise ValueError("代表畫格位置只能是 25%、50% 或 75%")
    return number


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "STORYBOARD_SCHEMA_VERSION", "THUMBNAIL_CONTRACT_VERSION", "apply_storyboard_state", "default_storyboard",
    "ensure_storyboard", "generate_storyboard", "generate_thumbnail", "load_storyboard", "normalize_storyboard",
    "save_storyboard", "storyboard_for_api", "storyboard_path", "storyboard_preview_dir", "storyboard_summary",
    "storyboard_thumbnail_path", "update_storyboard", "validate_storyboard",
]
