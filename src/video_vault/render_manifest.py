"""Compile the existing reviewed project workflow into a Render Manifest."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .database import project, project_bgm_tracks
from .project import project_dir, project_segments
from .render_profiles import get_render_profile
from .render_settings import load_render_settings


ALLOWED_AUDIO_ROLES = {"keep_original", "lower_original", "mute"}


def compile_render_manifest(cfg: dict, db: Path, project_id: int, profile_id: str | None = None) -> dict[str, Any]:
    folder = project_dir(cfg, project_id)
    plan_path = folder / "project_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"project plan not found: {plan_path}")
    row = project(db, project_id)
    if not row:
        raise ValueError(f"project not found: {project_id}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    settings = load_render_settings(cfg, project_id)
    if profile_id is not None:
        settings = {**settings, "profile_id": profile_id}
    profile = get_render_profile(str(settings["profile_id"]))

    reviewed_segments = project_segments(cfg, project_id, plan)
    included_segments = [segment for segment in _ordered_segments(reviewed_segments) if _included(segment)]
    segments = [_manifest_segment(row, index, segment) for index, segment in enumerate(included_segments, 1)]
    bgm = _manifest_bgm(db, project_id, settings)
    manifest: dict[str, Any] = {
        "schema_version": "2.0",
        "project_id": int(project_id),
        "project_name": str(row["name"]),
        "plan_id": str(plan.get("plan_id") or ""),
        "profile": profile,
        "settings": settings,
        "segments": segments,
        "bgm": bgm,
        "expected_duration_seconds": round(sum(float(item["timeline_duration_seconds"]) for item in segments), 6),
        "manifest_hash": "",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest["manifest_hash"] = manifest_hash(manifest)
    validation = validate_render_manifest(manifest)
    if validation["errors"]:
        raise ValueError("invalid render manifest: " + "; ".join(validation["errors"]))
    manifest["validation"] = validation
    path = folder / "render_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate_render_manifest(manifest: dict[str, Any], check_files: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != "2.0":
        errors.append("schema_version must be 2.0")
    if not isinstance(manifest.get("project_id"), int) or int(manifest.get("project_id", 0)) <= 0:
        errors.append("project_id must be a positive integer")
    profile = manifest.get("profile")
    if not isinstance(profile, dict) or not profile.get("profile_id"):
        errors.append("profile is required")
    else:
        try:
            get_render_profile(str(profile["profile_id"]))
        except ValueError as exc:
            errors.append(str(exc))

    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("manifest must contain at least one segment")
        segments = []
    ids = [item.get("segment_id") for item in segments if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        errors.append("segment_id values must be unique")
    orders = [item.get("order") for item in segments if isinstance(item, dict)]
    if len(orders) != len(set(orders)):
        errors.append("segment order values must be unique")
    if sorted(order for order in orders if isinstance(order, int)) != list(range(1, len(orders) + 1)):
        errors.append("segment order values must be continuous starting at 1")

    duration = 0.0
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            errors.append(f"segment {index} must be an object")
            continue
        segment_id = str(segment.get("segment_id") or f"#{index}")
        source_file = str(segment.get("source_file") or "")
        if not source_file:
            errors.append(f"{segment_id}: source_file is required")
        if check_files and source_file and not Path(source_file).exists():
            errors.append(f"{segment_id}: source_file does not exist: {source_file}")
        start = _number(segment.get("source_in_seconds"), f"{segment_id} source_in_seconds", errors)
        end = _number(segment.get("source_out_seconds"), f"{segment_id} source_out_seconds", errors)
        speed = _number(segment.get("speed"), f"{segment_id} speed", errors)
        if start is not None and start < 0:
            errors.append(f"{segment_id}: source_in_seconds must be >= 0")
        if start is not None and end is not None and end <= start:
            errors.append(f"{segment_id}: source_out_seconds must be greater than source_in_seconds")
        if speed is not None and speed <= 0:
            errors.append(f"{segment_id}: speed must be > 0")
        role = segment.get("audio_role")
        if role not in ALLOWED_AUDIO_ROLES:
            errors.append(f"{segment_id}: invalid audio_role {role!r}")
        duration += float(segment.get("timeline_duration_seconds") or 0)

    expected = _number(manifest.get("expected_duration_seconds"), "expected_duration_seconds", errors)
    if expected is not None and abs(expected - duration) > 0.001:
        errors.append("expected_duration_seconds does not match segment durations")
    for track in manifest.get("bgm", []) or []:
        missing = [key for key in ("source_url", "license_name", "attribution_text") if not str(track.get(key) or "").strip()]
        if missing:
            warnings.append(f"BGM {track.get('title', 'untitled')} license incomplete: {', '.join(missing)}")
    color = (manifest.get("settings") or {}).get("color") or {}
    if color.get("mode") == "dji_lut" and not str(color.get("lut_path") or "").strip():
        errors.append("color mode dji_lut requires color.lut_path")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key not in {"created_at", "manifest_hash", "validation", "warnings"}}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: int(row.get("manual_order") or 0))


def _included(segment: dict[str, Any]) -> bool:
    value = segment.get("include", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _manifest_segment(project_row: Any, order: int, segment: dict[str, Any]) -> dict[str, Any]:
    source_in = round(float(segment.get("start_seconds") or 0), 3)
    source_out = round(float(segment.get("end_seconds") or 0), 3)
    speed = round(float(segment.get("speed") or 1.0), 6)
    return {
        "segment_id": str(segment.get("segment_id") or ""),
        "order": order,
        "clip_id": str(segment.get("clip_id") or ""),
        "video_id": int(segment.get("video_id") or 0),
        "source_file": str(segment.get("source_file") or ""),
        "source_in_seconds": source_in,
        "source_out_seconds": source_out,
        "source_duration_seconds": round(source_out - source_in, 6),
        "speed": speed,
        "timeline_duration_seconds": round((source_out - source_in) / speed, 6) if speed > 0 else 0.0,
        "audio_role": str(segment.get("audio_role") or "lower_original"),
        "scene_role": str(segment.get("scene_role") or ""),
        "story_position": str(segment.get("story_position") or ""),
        "user_notes": str(segment.get("user_notes") or ""),
        "title": str(segment.get("title") or ""),
        "suggested_use": str(segment.get("suggested_use") or ""),
    }


def _manifest_bgm(db: Path, project_id: int, settings: dict[str, Any]) -> list[dict[str, Any]]:
    overrides = {int(item.get("track_id")): item for item in settings.get("bgm", []) if isinstance(item, dict) and item.get("track_id") is not None}
    result = []
    for row in project_bgm_tracks(db, project_id):
        item = dict(row)
        override = overrides.get(int(item["id"]), {})
        result.append({
            "track_id": int(item["id"]),
            "title": str(item.get("title") or ""),
            "source_path": str(item.get("file_path") or ""),
            "source_url": str(item.get("source_url") or ""),
            "license_name": str(item.get("license_name") or ""),
            "attribution_text": str(item.get("attribution_text") or ""),
            "gain_db": float(override.get("gain_db", settings.get("audio", {}).get("bgm_gain_db", -18.0))),
            "loop": bool(override.get("loop", True)),
            "fade_in_seconds": float(override.get("fade_in_seconds", 1.0)),
            "fade_out_seconds": float(override.get("fade_out_seconds", 2.0)),
        })
    return result


def _number(value: Any, field: str, errors: list[str]) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be numeric")
        return None
    if number != number or number in (float("inf"), float("-inf")):
        errors.append(f"{field} must be finite")
        return None
    return number


__all__ = ["ALLOWED_AUDIO_ROLES", "compile_render_manifest", "manifest_hash", "validate_render_manifest"]
