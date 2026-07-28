"""Deterministic FFmpeg composition for approved visual timeline items."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .visual_timeline import (
    SUPPORTED_ANIMATION_IDS,
    SUPPORTED_STYLE_IDS,
    VISUAL_TIMELINE_SCHEMA_VERSION,
    validate_visual_timeline,
)


class VisualRenderError(ValueError):
    pass


@dataclass(frozen=True)
class VisualFilter:
    expression: str
    text_files: tuple[Path, ...]
    item_ids: tuple[str, ...]
    temp_dir: Path


def prepare_visual_filter(
    manifest: Mapping[str, Any],
    work_dir: Path,
) -> VisualFilter | None:
    items = [
        dict(item)
        for item in manifest.get("visual_items") or []
        if isinstance(item, Mapping)
    ]
    if not items:
        return None
    validation = validate_visual_timeline({
        "schema_version": VISUAL_TIMELINE_SCHEMA_VERSION,
        "items": items,
    })
    if not validation["valid"]:
        raise VisualRenderError(
            "approved visual timeline is invalid: " + "; ".join(validation["errors"])
        )

    timeline_duration = sum(
        float(item.get("timeline_duration_seconds") or 0)
        for item in manifest.get("segments") or []
        if isinstance(item, Mapping)
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    text_root = Path(tempfile.mkdtemp(prefix="video-vault-visual-")).resolve()
    filters: list[str] = []
    text_files: list[Path] = []
    item_ids: list[str] = []
    try:
        for index, item in enumerate(items, 1):
            stable_id = str(item["stable_id"])
            start = float(item["start_seconds"])
            duration = float(item["duration_seconds"])
            end = start + duration
            if end > timeline_duration + 0.001:
                raise VisualRenderError(
                    f"visual item {stable_id} exceeds the approved media timeline"
                )
            font = _font_asset(item)
            text_path = text_root / f"visual-{index:03d}-{_safe_id(stable_id)}.txt"
            text_path.write_text(str(item["text"]), encoding="utf-8")
            text_files.append(text_path)
            item_ids.append(stable_id)
            filters.append(_drawtext_filter(item, font, text_path, start, end))
    except Exception:
        for path in text_files:
            path.unlink(missing_ok=True)
        text_root.rmdir()
        raise
    return VisualFilter(
        expression=",".join(filters),
        text_files=tuple(text_files),
        item_ids=tuple(item_ids),
        temp_dir=text_root,
    )


def cleanup_visual_filter(value: VisualFilter | None) -> None:
    if value is None:
        return
    for path in value.text_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        value.temp_dir.rmdir()
    except OSError:
        pass


def _font_asset(item: Mapping[str, Any]) -> Path:
    for asset in item.get("runtime_assets") or []:
        if isinstance(asset, Mapping):
            kind = str(asset.get("kind") or "font")
            source = str(asset.get("path") or asset.get("source_path") or "")
        else:
            kind = "font"
            source = str(asset or "")
        if kind != "font" or not source:
            continue
        path = Path(source).expanduser().resolve()
        if path.is_file() and not path.is_symlink():
            return path
    raise VisualRenderError(
        f"visual item {item.get('stable_id', '')} has no approved font asset"
    )


def _drawtext_filter(
    item: Mapping[str, Any],
    font: Path,
    text_path: Path,
    start: float,
    end: float,
) -> str:
    style_id = str(item.get("style_id") or "")
    animation_id = str(item.get("animation_id") or "")
    if style_id not in SUPPORTED_STYLE_IDS:
        raise VisualRenderError(f"unsupported visual style: {style_id}")
    if animation_id not in SUPPORTED_ANIMATION_IDS:
        raise VisualRenderError(f"unsupported visual animation: {animation_id}")
    fade = min(0.25, max(0.05, (end - start) / 3))
    alpha = (
        f"if(lt(t\\,{start + fade:.6f})\\,"
        f"(t-{start:.6f})/{fade:.6f}\\,"
        f"if(lt(t\\,{end - fade:.6f})\\,1\\,"
        f"({end:.6f}-t)/{fade:.6f}))"
    )
    return ":".join([
        "drawtext=" + f"fontfile='{_escape_filter_path(font)}'",
        f"textfile='{_escape_filter_path(text_path)}'",
        "reload=0",
        "x=w*0.06",
        "y=h-text_h-h*0.08",
        "fontsize=h*0.045",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.55",
        "boxborderw=h*0.018",
        f"alpha='{alpha}'",
        f"enable='between(t\\,{start:.6f}\\,{end:.6f})'",
    ])


def _escape_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "'\\''")


def _safe_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe[:80] or "item"


__all__ = [
    "VisualFilter",
    "VisualRenderError",
    "cleanup_visual_filter",
    "prepare_visual_filter",
]
