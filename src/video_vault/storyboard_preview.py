"""Short, on-demand storyboard previews built from the effective render state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .audio_preview import render_project_audio_preview
from .project import project_segments
from .storyboard import (
    apply_storyboard_state,
    load_storyboard,
    normalize_storyboard,
    storyboard_preview_dir,
    validate_storyboard,
)


class StoryboardPreviewError(ValueError):
    pass


def render_storyboard_preview(
    cfg: dict,
    db: Path,
    project_id: int,
    *,
    mode: str,
    segment_id: str | None = None,
    duration_seconds: float = 8.0,
    timeline_start_seconds: float = 0.0,
    storyboard_state: Mapping[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    persisted = load_storyboard(cfg, project_id)
    state = normalize_storyboard(storyboard_state) if storyboard_state is not None else persisted
    if state is None:
        raise StoryboardPreviewError("尚未建立分鏡")
    plan_path = Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}" / "project_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise StoryboardPreviewError(f"無法讀取故事計畫：{exc}") from exc
    raw_rows = project_segments(cfg, project_id, plan, apply_storyboard=False)
    validation = validate_storyboard(state, raw_rows)
    if not validation["valid"]:
        raise StoryboardPreviewError("Storyboard 無效：" + "；".join(validation["errors"]))
    rows = [row for row in apply_storyboard_state(raw_rows, state) if _included(row)]
    if not rows:
        raise StoryboardPreviewError("目前沒有可預覽片段")
    mode = str(mode or "range")
    if mode == "segment":
        windows = [("segment", *_segment_window(rows, state, str(segment_id or "")))]
    elif mode == "transition":
        windows = _transition_windows(rows, str(segment_id or ""))
    elif mode == "range":
        requested = float(duration_seconds)
        if requested not in {5.0, 8.0, 12.0}:
            raise StoryboardPreviewError("分鏡預覽長度只能選 5、8 或 12 秒")
        total = _total_duration(rows)
        start = max(0.0, min(float(timeline_start_seconds), max(0.0, total - 0.001)))
        windows = [("range", start, min(requested, total - start))]
    else:
        raise StoryboardPreviewError("不支援的分鏡預覽模式")

    rendered: list[dict[str, Any]] = []
    for kind, start, duration in windows:
        if duration <= 0:
            continue
        result = render_project_audio_preview(
            cfg,
            db,
            project_id,
            timeline_start_seconds=start,
            duration_seconds=max(0.1, min(20.0, duration)),
            storyboard_state_override=state,
            force=force,
            output_dir=storyboard_preview_dir(cfg, project_id),
        )
        rendered.append({
            **result,
            "kind": kind,
            "timeline_start_seconds": start,
            "duration_seconds": result.get("duration_seconds", duration),
        })
    if not rendered:
        raise StoryboardPreviewError("目前沒有可預覽的時間範圍")
    first = rendered[0]
    return {
        "ok": True,
        "mode": mode,
        "requested_segment_id": segment_id or "",
        "file": first.get("file"),
        "cache_hit": all(bool(item.get("cache_hit")) for item in rendered),
        "timeline_start_seconds": first.get("timeline_start_seconds", 0),
        "duration_seconds": first.get("duration_seconds", 0),
        "previews": rendered if mode == "transition" else [],
    }


def _segment_window(rows: list[dict[str, Any]], state: Mapping[str, Any], segment_id: str) -> tuple[float, float]:
    cursor = 0.0
    for row in rows:
        duration = _timeline_duration(row)
        if str(row.get("segment_id")) == segment_id:
            requested = min(5.0, duration)
            ratio = float((state.get("segments", {}).get(segment_id) or {}).get("thumbnail_time_ratio", 0.5))
            center = duration * ratio
            return cursor + max(0.0, min(max(0.0, duration - requested), center - requested / 2.0)), requested
        cursor += duration
    raise StoryboardPreviewError("找不到指定片段")


def _transition_windows(rows: list[dict[str, Any]], segment_id: str) -> list[tuple[str, float, float]]:
    index = next((index for index, row in enumerate(rows) if str(row.get("segment_id")) == segment_id), None)
    if index is None:
        raise StoryboardPreviewError("找不到指定片段")
    cursor = sum(_timeline_duration(row) for row in rows[:index])
    current_duration = _timeline_duration(rows[index])
    if len(rows) == 1:
        return [("outgoing", cursor, min(4.0, current_duration))]
    windows: list[tuple[str, float, float]] = []
    if index > 0:
        previous_duration = _timeline_duration(rows[index - 1])
        previous_tail = min(2.0, previous_duration)
        current_head = min(2.0, current_duration)
        windows.append(("incoming", cursor - previous_tail, previous_tail + current_head))
    if index + 1 < len(rows):
        current_tail = min(2.0, current_duration)
        next_head = min(2.0, _timeline_duration(rows[index + 1]))
        windows.append(("outgoing", cursor + current_duration - current_tail, current_tail + next_head))
    return windows


def _timeline_duration(row: Mapping[str, Any]) -> float:
    start = float(row.get("start_seconds") or 0.0)
    end = float(row.get("end_seconds") or start)
    speed = max(0.01, float(row.get("speed") or 1.0))
    return max(0.0, (end - start) / speed)


def _included(row: Mapping[str, Any]) -> bool:
    value = row.get("include", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _total_duration(rows: list[dict[str, Any]]) -> float:
    return sum(_timeline_duration(row) for row in rows)


def storyboard_preview_path(cfg: dict, project_id: int, filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not name.endswith(".mp4"):
        raise ValueError("invalid storyboard preview filename")
    path = (storyboard_preview_dir(cfg, project_id) / name).resolve()
    root = storyboard_preview_dir(cfg, project_id).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(path)
    return path


__all__ = ["StoryboardPreviewError", "render_storyboard_preview", "storyboard_preview_path"]
