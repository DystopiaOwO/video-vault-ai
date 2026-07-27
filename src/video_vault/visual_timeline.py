"""Versioned visual overlay contract shared by draft and formal manifests."""

from __future__ import annotations

import math
from typing import Any, Mapping


VISUAL_TIMELINE_SCHEMA_VERSION = 1
VISUAL_TIMELINE_CONTRACT_VERSION = "visual-timeline-v1"
VISUAL_TYPES = {"intro", "outro", "chapter_card", "lower_third"}


def build_visual_timeline(groups: list[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    timeline_start = 0.0
    for group_index, group in enumerate(groups, 1):
        segments = [item for item in group.get("segments", []) or [] if bool(item.get("include", True))]
        if not segments:
            continue
        group_id = str(group.get("label") or f"group_{group_index:03d}")
        first_duration = max(0.5, float(segments[0].get("estimated_output_seconds") or 1.5))
        duration = round(min(1.5, first_duration), 3)
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
    return {"valid": not errors, "errors": errors}


__all__ = [
    "VISUAL_TIMELINE_CONTRACT_VERSION", "VISUAL_TIMELINE_SCHEMA_VERSION",
    "build_visual_timeline", "validate_visual_timeline",
]
