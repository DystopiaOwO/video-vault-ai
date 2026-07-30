"""Versioned visual overlay contract shared by draft and formal manifests."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any, Mapping


VISUAL_TIMELINE_SCHEMA_VERSION = 1
VISUAL_TIMELINE_CONTRACT_VERSION = "visual-timeline-v1"
VISUAL_TYPES = {"intro", "outro", "chapter_card", "lower_third"}
# Keep the versioned contract in sync with the formal compositor. Unknown
# styles/animations still fail closed during approval and render.
SUPPORTED_STYLE_IDS = {"location-lower-left", "title-center", "lower-third"}
SUPPORTED_ANIMATION_IDS = {"static", "fade-in-out"}


def build_visual_timeline(groups: list[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    timeline_start = 0.0
    for group_index, group in enumerate(groups, 1):
        segments = [item for item in group.get("segments", []) or [] if bool(item.get("include", True))]
        if not segments:
            continue
        group_id = str(group.get("group_id") or group.get("label") or f"group_{group_index:03d}")
        # Generated chapter cards use a stable 1.5s presentation window. A
        # manually authored shorter card is still preserved by reconciliation.
        duration = 1.5
        items.append({
            "stable_id": f"chapter-card-{group_index:03d}",
            "type": "chapter_card",
            "start_seconds": round(timeline_start, 3),
            "duration_seconds": duration,
            "text": str(group.get("label") or ""),
            "style_id": "location-lower-left",
            "style_version": 1,
            "animation_id": "fade-in-out",
            "font": {"family": "system-ui", "weight": 600},
            "runtime_assets": [],
            "group_id": group_id,
        })
        timeline_start += sum(float(item.get("estimated_output_seconds") or 0) for item in segments)
    return {
        "schema_version": VISUAL_TIMELINE_SCHEMA_VERSION,
        "contract_version": VISUAL_TIMELINE_CONTRACT_VERSION,
        "items": items,
    }


def validate_visual_timeline(value: Mapping[str, Any] | None) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return {"valid": False, "errors": ["visual_timeline must be an object"]}
    if int(value.get("schema_version") or 0) != VISUAL_TIMELINE_SCHEMA_VERSION:
        errors.append("unsupported visual_timeline schema_version")
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("visual_timeline.items must be a list")
        items = []
    seen: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, Mapping):
            errors.append(f"visual item {index} must be an object")
            continue
        stable_id = str(item.get("stable_id") or "")
        if not stable_id or stable_id in seen:
            errors.append(f"visual item {index}: stable_id must be unique and non-empty")
        seen.add(stable_id)
        if str(item.get("type") or "") not in VISUAL_TYPES:
            errors.append(f"visual item {index}: unsupported type")
        for key in ("start_seconds", "duration_seconds"):
            try:
                number = float(item.get(key))
            except (TypeError, ValueError):
                errors.append(f"visual item {index}: {key} must be numeric")
                continue
            if not math.isfinite(number) or number < 0 or (key == "duration_seconds" and number <= 0):
                errors.append(f"visual item {index}: {key} must be finite and positive where required")
        if not str(item.get("text") or "").strip():
            errors.append(f"visual item {index}: text is required")
        if not str(item.get("style_id") or "") or not str(item.get("animation_id") or ""):
            errors.append(f"visual item {index}: style_id and animation_id are required")
        elif str(item.get("style_id")) not in SUPPORTED_STYLE_IDS:
            errors.append(f"visual item {index}: unsupported style_id")
        elif str(item.get("animation_id")) not in SUPPORTED_ANIMATION_IDS:
            errors.append(f"visual item {index}: unsupported animation_id")
    return {"valid": not errors, "errors": errors}


def resolve_visual_runtime_assets(
    value: Mapping[str, Any],
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Pin a concrete font file into every generated visual item."""

    result = deepcopy(dict(value))
    items = result.get("items")
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        assets = item.get("runtime_assets")
        if isinstance(assets, list) and assets:
            continue
        font_path = resolve_default_font_path(cfg)
        item["runtime_assets"] = [{
            "kind": "font",
            "path": str(font_path),
            "asset_id": f"font:{font_path.name}",
        }]
    return result


def align_visual_timeline_to_segments(
    value: Mapping[str, Any],
    segments: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Align chapter items to the approved, speed-adjusted media timeline."""

    result = deepcopy(dict(value))
    items = result.get("items")
    if not isinstance(items, list):
        return result
    starts: dict[str, float] = {}
    durations: dict[str, float] = {}
    cursor = 0.0
    for segment in segments:
        group = str(segment.get("group_id") or segment.get("group") or "")
        duration = max(0.0, float(segment.get("timeline_duration_seconds") or 0))
        starts.setdefault(group, cursor)
        durations[group] = durations.get(group, 0.0) + duration
        cursor += duration
    for item in items:
        if not isinstance(item, dict):
            continue
        group = str(item.get("group_id") or "")
        if group in starts:
            item["start_seconds"] = round(starts[group], 6)
            if str(item.get("type") or "") != "chapter_card":
                item["duration_seconds"] = round(
                    min(float(item.get("duration_seconds") or 0), durations[group]),
                    6,
                )
        else:
            start = max(0.0, float(item.get("start_seconds") or 0))
            item["start_seconds"] = round(start, 6)
            item["duration_seconds"] = round(
                min(float(item.get("duration_seconds") or 0), max(0.0, cursor - start)),
                6,
            )
    return result


def reconcile_visual_timeline_with_segments(
    value: Mapping[str, Any],
    segments: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Map chapter cards to the ordered storyboard groups that reach output."""

    result = deepcopy(dict(value))
    items = result.get("items")
    if not isinstance(items, list):
        return result
    groups: list[dict[str, Any]] = []
    group_indexes: dict[str, int] = {}
    for segment in segments:
        group_id = str(segment.get("group_id") or segment.get("group") or "")
        title = str(segment.get("group") or group_id)
        if group_id not in group_indexes:
            group_indexes[group_id] = len(groups)
            groups.append({"group_id": group_id, "label": title, "segments": []})
        groups[group_indexes[group_id]]["segments"].append({
            "include": True,
            "estimated_output_seconds": float(segment.get("timeline_duration_seconds") or 0),
        })

    generated_chapters = list(build_visual_timeline(groups)["items"])
    chapter_index = 0
    reconciled: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("type") or "") != "chapter_card":
            reconciled.append(item)
            continue
        if chapter_index >= len(generated_chapters):
            continue
        target = generated_chapters[chapter_index]
        updated = dict(item)
        updated["group_id"] = target["group_id"]
        updated["text"] = target["text"]
        updated["start_seconds"] = target["start_seconds"]
        # Reconcile the chapter to the current included group, but preserve a
        # shorter user-authored duration instead of expanding it silently.
        requested_duration = float(updated.get("duration_seconds") or target["duration_seconds"])
        updated["duration_seconds"] = round(min(requested_duration, float(target["duration_seconds"])), 6)
        reconciled.append(updated)
        chapter_index += 1
    reconciled.extend(generated_chapters[chapter_index:])
    result["items"] = reconciled
    return result


def resolve_default_font_path(cfg: Mapping[str, Any]) -> Path:
    render = cfg.get("render") if isinstance(cfg.get("render"), Mapping) else {}
    configured = str(cfg.get("visual_font_path") or render.get("visual_font_path") or "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([
        Path("C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ])
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    if configured:
        raise ValueError(f"visual font file does not exist: {configured}")
    raise ValueError("no supported visual font was found; configure render.visual_font_path")


__all__ = [
    "SUPPORTED_ANIMATION_IDS",
    "SUPPORTED_STYLE_IDS",
    "VISUAL_TIMELINE_CONTRACT_VERSION",
    "VISUAL_TIMELINE_SCHEMA_VERSION",
    "align_visual_timeline_to_segments",
    "build_visual_timeline",
    "reconcile_visual_timeline_with_segments",
    "resolve_default_font_path",
    "resolve_visual_runtime_assets",
    "validate_visual_timeline",
]
