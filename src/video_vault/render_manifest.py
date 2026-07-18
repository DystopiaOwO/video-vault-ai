"""Compile reviewed project inputs into the shared RenderManifest contract."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .render_profiles import get_render_profile, validate_render_profile
from .render_types import BgmSettings, ColorSettings, RenderKind, RenderManifest, RenderSegment, RenderSettings, to_dict


def compile_manifest(
    project_plan: Mapping[str, Any] | str | Path,
    segment_review: Mapping[str, Any] | list[Mapping[str, Any]] | str | Path | None = None,
    render_settings: Mapping[str, Any] | str | Path | None = None,
    bgm: Mapping[str, Any] | None = None,
    color: Mapping[str, Any] | None = None,
    profile: str | None = None,
) -> RenderManifest:
    """Compile a plan and optional review/settings files into one immutable manifest."""

    plan = _load_mapping(project_plan)
    review = _load_review(segment_review)
    settings_input = _load_mapping(render_settings) if render_settings is not None else {}
    resolved_profile = profile or str(settings_input.get("profile") or "preview_1080p30")
    validate_render_profile(resolved_profile)
    selected_bgm = dict(bgm or settings_input.get("bgm") or _first_bgm(plan.get("bgm")))
    selected_color = dict(color or settings_input.get("color") or {})
    settings = _render_settings(settings_input, resolved_profile, selected_bgm, selected_color)

    raw_segments = _plan_segments(plan)
    clips = {str(item.get("clip_id")): item for item in plan.get("clips", []) if isinstance(item, Mapping)}
    reviewed = {str(item.get("segment_id")): item for item in review if item.get("segment_id")}
    records: list[RenderSegment] = []
    for fallback_order, raw in enumerate(raw_segments, 1):
        segment_id = str(raw.get("segment_id") or raw.get("id") or f"segment_{fallback_order:03}")
        override = reviewed.get(segment_id, {})
        merged = {**raw, **override}
        include = _bool(merged.get("include", merged.get("included", True)), True)
        if not include:
            continue
        source_file = str(merged.get("source_file") or merged.get("source_path") or "")
        start_is_ms = "source_in_ms" in merged
        end_is_ms = "source_out_ms" in merged
        start_ms = _milliseconds(merged.get("source_in_ms", merged.get("start_seconds", 0)), is_milliseconds=start_is_ms)
        end_ms = _milliseconds(merged.get("source_out_ms", merged.get("end_seconds", 0)), is_milliseconds=end_is_ms)
        if end_ms < start_ms:
            raise ValueError(f"Segment {segment_id} has end before start")
        speed = float(merged.get("speed", 1.0) or 1.0)
        if speed <= 0:
            raise ValueError(f"Segment {segment_id} speed must be positive")
        source_duration_ms = _source_duration_ms(merged, clips)
        timeline_duration_ms = round((end_ms - start_ms) / speed)
        records.append(RenderSegment(
            segment_id=segment_id, source_file=source_file, source_in_ms=start_ms, source_out_ms=end_ms,
            manual_order=int(merged.get("manual_order", merged.get("order", fallback_order)) or fallback_order),
            include=True, speed=speed, audio_role=str(merged.get("audio_role", "keep_original")),
            scene_role=str(merged.get("scene_role", merged.get("activity", "")) or ""),
            title=str(merged.get("title", "") or ""), timeline_duration_ms=timeline_duration_ms,
            source_duration_ms=source_duration_ms, overlay=dict(merged.get("overlay") or {}),
        ))
    records.sort(key=lambda item: (item.manual_order, item.segment_id))
    timeline_cursor = 0
    positioned: list[RenderSegment] = []
    for record in records:
        positioned.append(replace(record, timeline_start_ms=timeline_cursor))
        timeline_cursor += record.timeline_duration_ms

    project_id = str(plan.get("project_id", ""))
    plan_id = str(plan.get("plan_id") or plan.get("id") or "")
    manifest = RenderManifest(
        plan_id=plan_id, project_id=project_id, render_kind=_render_kind(settings_input.get("kind")),
        profile=resolved_profile, settings=settings, segments=positioned, timeline_duration_ms=timeline_cursor,
        bgm=BgmSettings(**_bgm_values(selected_bgm)), color=ColorSettings(**_color_values(selected_color)),
        overlays=list(plan.get("title_cards") or plan.get("overlays") or []),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return replace(manifest, manifest_hash=manifest_hash(manifest))


def compile_manifest_from_files(project_plan_path: str | Path, *, segment_review_path: str | Path | None = None, render_settings_path: str | Path | None = None, **kwargs: Any) -> RenderManifest:
    return compile_manifest(project_plan_path, segment_review_path, render_settings_path, **kwargs)


def manifest_hash(manifest: RenderManifest) -> str:
    payload = to_dict(manifest)
    payload.pop("manifest_hash", None)
    payload.pop("created_at", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_to_dict(manifest: RenderManifest) -> dict[str, Any]:
    return to_dict(manifest)


def _plan_segments(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if isinstance(plan.get("segments"), list):
        return [item for item in plan["segments"] if isinstance(item, Mapping)]
    result: list[Mapping[str, Any]] = []
    for group in plan.get("groups", []):
        if isinstance(group, Mapping):
            result.extend(item for item in group.get("segments", []) if isinstance(item, Mapping))
    return result


def _render_settings(data: Mapping[str, Any], profile: str, bgm: Mapping[str, Any], color: Mapping[str, Any]) -> RenderSettings:
    kind = _render_kind(data.get("kind"))
    encoder = str(data.get("encoder") or get_render_profile(profile).video_encoder)
    return RenderSettings(kind=kind, profile=profile, encoder=encoder, transition=str(data.get("transition", "cut")),
        overlay_enabled=_bool(data.get("overlay_enabled", bool(data.get("overlays"))), False),
        audio_role=str(data.get("audio_role", "keep_original")), audio_crossfade_ms=int(data.get("audio_crossfade_ms", 80) or 0),
        bgm=BgmSettings(**_bgm_values(bgm)), color=ColorSettings(**_color_values(color)))


def _render_kind(value: Any) -> RenderKind:
    try:
        return RenderKind(str(value or RenderKind.ROUGH_PREVIEW.value))
    except ValueError as exc:
        raise ValueError(f"Unknown render kind: {value}") from exc


def _bgm_values(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: data.get(key, default) for key, default in {
        "enabled": False, "source_file": None, "track_id": None, "volume_db": -24.0,
        "fade_in_ms": 0, "fade_out_ms": 0, "loop": True, "attribution": "", "license_name": "", "source_url": "",
    }.items()}


def _color_values(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: data.get(key, default) for key, default in {
        "mode": "none", "lut_path": None, "decision": "", "reference_clip_id": None,
        "brightness": 0.0, "saturation": 1.0, "gamma": 1.0,
    }.items()}


def _first_bgm(value: Any) -> Mapping[str, Any]:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], Mapping) else {}


def _load_mapping(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8"))


def _load_review(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_text(encoding="utf-8"))
    if isinstance(value, Mapping):
        value = value.get("segments", value.get("items", []))
    return list(value or [])


def _source_duration_ms(segment: Mapping[str, Any], clips: Mapping[str, Mapping[str, Any]]) -> int | None:
    value = segment.get("source_duration_ms")
    if value is None:
        clip = clips.get(str(segment.get("clip_id", "")), {})
        value = clip.get("duration_seconds")
    return _milliseconds(value, is_milliseconds="source_duration_ms" in segment) if value is not None else None


def _milliseconds(value: Any, *, is_milliseconds: bool = False) -> int:
    if value is None:
        return 0
    number = float(value)
    return round(number if is_milliseconds else number * 1000)


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "null", "none"}
    return bool(value)


__all__ = ["compile_manifest", "compile_manifest_from_files", "manifest_hash", "manifest_to_dict"]
