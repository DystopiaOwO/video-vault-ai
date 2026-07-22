"""Short, on-demand storyboard previews built from the normal render path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .audio_preview import render_project_audio_preview
from .project import project_segments
from .storyboard import load_storyboard, storyboard_preview_dir


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
    force: bool = False,
) -> dict[str, Any]:
    state = load_storyboard(cfg, project_id)
    if state is None:
        raise StoryboardPreviewError("尚未建立分鏡")
    plan_path = Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}" / "project_plan.json"
    import json

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    rows = project_segments(cfg, project_id, plan)
    if not rows:
        raise StoryboardPreviewError("目前沒有可預覽片段")
    mode = str(mode or "range")
    if mode == "segment":
        start, duration = _segment_window(rows, state, str(segment_id or ""))
    elif mode == "transition":
        start, duration = _transition_window(rows, str(segment_id or ""))
    elif mode == "range":
        requested = float(duration_seconds)
        if requested not in {5.0, 8.0, 12.0}:
            raise StoryboardPreviewError("分鏡預覽長度只能選 5、8 或 12 秒")
        start, duration = 0.0, min(requested, _total_duration(rows))
    else:
        raise StoryboardPreviewError("不支援的分鏡預覽模式")
    result = render_project_audio_preview(
        cfg,
        db,
        project_id,
        timeline_start_seconds=start,
        duration_seconds=max(0.1, min(20.0, duration)),
        force=force,
        output_dir=storyboard_preview_dir(cfg, project_id),
    )
    result.update({"mode": mode, "requested_segment_id": segment_id or "", "timeline_start_seconds": start})
    return result


def _segment_window(rows: list[dict[str, Any]], state: dict[str, Any], segment_id: str) -> tuple[float, float]:
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


def _transition_window(rows: list[dict[str, Any]], segment_id: str) -> tuple[float, float]:
    index = next((index for index, row in enumerate(rows) if str(row.get("segment_id")) == segment_id), None)
    if index is None:
        raise StoryboardPreviewError("找不到指定片段")
    cursor = sum(_timeline_duration(row) for row in rows[:index])
    current = rows[index]
    current_duration = min(3.0, _timeline_duration(current))
    before = min(2.0, _timeline_duration(rows[index - 1])) if index > 0 else 0.0
    after = min(2.0, _timeline_duration(rows[index + 1])) if index + 1 < len(rows) else 0.0
    return cursor - before, before + current_duration + after


def _timeline_duration(row: dict[str, Any]) -> float:
    start = float(row.get("start_seconds") or 0.0)
    end = float(row.get("end_seconds") or start)
    speed = max(0.01, float(row.get("speed") or 1.0))
    return max(0.0, (end - start) / speed)


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
