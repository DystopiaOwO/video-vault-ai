"""Compile the existing reviewed project workflow into a Render Manifest."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .database import project
from .audio_state import (
    effective_project_audio_state,
    effective_project_bgm,
    effective_segment_audio_settings,
    has_audio_state,
    load_audio_state,
    normalize_audio_role,
    normalize_audio_state,
    resolve_audio_state_bgm,
    resolve_legacy_project_bgm,
)
from .bgm_pipeline import BgmPipelineError, validate_bgm_track
from .color_consistency import effective_color_settings, has_color_state, load_project_color_state
from .project import project_dir, project_segments
from .render_profiles import get_render_profile
from .render_settings import load_render_settings
from .visual_timeline import (
    align_visual_timeline_to_segments,
    build_visual_timeline,
    reconcile_visual_timeline_with_segments,
    resolve_visual_runtime_assets,
    validate_visual_timeline,
)
from .visual_compositor import resolve_visual_timeline, stable_visual_hash
from .visual_style import ensure_visual_style_state, validate_materialized_visual_style


ALLOWED_AUDIO_ROLES = {"keep_original", "lower_original", "mute", "keep", "lower", "bgm_only"}


def build_render_manifest(
    cfg: dict,
    db: Path,
    project_id: int,
    profile_id: str | None = None,
    *,
    audio_state_override: dict[str, Any] | None = None,
    storyboard_state_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    folder = project_dir(cfg, project_id)
    plan_path = folder / "project_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"project plan not found: {plan_path}")
    row = project(db, project_id)
    if not row:
        raise ValueError(f"project not found: {project_id}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    settings = load_render_settings(cfg, project_id)
    audio_file_exists = audio_state_override is not None or has_audio_state(cfg, project_id)
    # A legacy project with exactly one BGM has an effective in-memory state.
    # Keep the raw disabled-file state distinct so disabling the new workflow
    # still restores the legacy relation as before.
    if audio_state_override is not None:
        audio_state = normalize_audio_state(audio_state_override)
        active_audio_state = audio_state if audio_state_file_is_enabled(audio_state, True) else None
    elif audio_file_exists:
        audio_state = load_audio_state(cfg, project_id)
        active_audio_state = audio_state if audio_state_file_is_enabled(audio_state, True) else None
    else:
        audio_state = effective_project_audio_state(cfg, project_id, db)
        active_audio_state = audio_state
    if active_audio_state is not None:
        settings = {**settings, "audio": active_audio_state}
    color_state = load_project_color_state(cfg, project_id) if has_color_state(cfg, project_id) else None
    if color_state is not None:
        settings = {
            **settings,
            "color": effective_color_settings(color_state),
            "color_consistency": {
                "schema_version": color_state.get("schema_version", 1),
                "reference_id": (color_state.get("reference") or {}).get("id", ""),
            },
        }
    if profile_id is not None:
        settings = {**settings, "profile_id": profile_id}
    profile = get_render_profile(str(settings["profile_id"]))

    from .storyboard import apply_storyboard_state, load_storyboard, storyboard_render_state
    from .creative_brief import approved_creative_brief, load_creative_brief

    creative_brief = load_creative_brief(db, int(project_id))
    approved_brief = approved_creative_brief(db, int(project_id))
    if approved_brief:
        approved_profile_id = str((approved_brief.get("output") or {}).get("render_profile_id") or "")
        if not approved_profile_id:
            raise ValueError("approved Creative Brief 缺少 render_profile_id")
        if profile_id is not None and str(profile_id) != approved_profile_id:
            raise ValueError("render profile 必須遵循 approved Creative Brief output contract")
        settings = {**settings, "profile_id": approved_profile_id}
        profile_id = approved_profile_id
    profile = get_render_profile(str(settings["profile_id"]))

    raw_segments = project_segments(cfg, project_id, plan, apply_storyboard=False)
    storyboard_state = storyboard_state_override if storyboard_state_override is not None else (load_storyboard(cfg, project_id) or {})
    reviewed_segments = apply_storyboard_state(raw_segments, storyboard_state) if storyboard_state else raw_segments
    included_segments = [segment for segment in _ordered_segments(reviewed_segments) if _included(segment)]
    segments = [
        _manifest_segment(
            row,
            index,
            segment,
            effective_color_settings(color_state, str(segment.get("segment_id"))) if color_state is not None else None,
            audio_state=active_audio_state,
            cfg=cfg,
            project_id=project_id,
        )
        for index, segment in enumerate(included_segments, 1)
    ]
    # Keep the disabled-file marker here so legacy BGM handling can avoid
    # silently reusing the track selected by the disabled new workflow.
    # A legacy seed preserves the old manifest behaviour: its source asset is
    # fingerprinted during approval/preflight, rather than turning a current
    # manifest-hash invalidation into an unrelated early media-probe error.
    bgm = _manifest_bgm(cfg, db, project_id, settings, audio_state, validate_selected_bgm=audio_file_exists or audio_state_override is not None)
    bgm_credits = [
        str(track.get("attribution_text") or track.get("source_url") or "")
        for track in bgm
        if str(track.get("attribution_status") or "unknown") == "required"
    ]
    unresolved_bgm_licenses = [
        int(track.get("track_id") or 0)
        for track in bgm
        if str(track.get("license_status") or "unverified") != "verified"
        or str(track.get("attribution_status") or "unknown") == "unknown"
    ]
    visual_source = plan.get("visual_timeline") or build_visual_timeline(plan.get("groups") or [])
    visual_timeline_input_hash = stable_visual_hash(visual_source) if isinstance(visual_source, Mapping) else ""
    visual_style_state = ensure_visual_style_state(cfg, db, project_id)
    if str(visual_style_state.get("status") or "") == "stale" and visual_style_state.get("approved"):
        raise ValueError("approved Visual Style is stale; re-preview and re-approve before Render: " + str(visual_style_state.get("stale_reason") or "currentity_changed"))
    approved_visual_style = visual_style_state.get("approved") if str(visual_style_state.get("status") or "") == "approved" else None
    visual_plan = align_visual_timeline_to_segments(
        resolve_visual_runtime_assets(
            reconcile_visual_timeline_with_segments(
                visual_source,
                segments,
            ),
            cfg,
        ),
        segments,
    )
    visual_timeline = visual_plan
    visual_timeline = resolve_visual_timeline(
        visual_timeline,
        segments,
        profile,
        require_assets=True,
        chapter_composition=str((approved_visual_style or {}).get("composition") or "standalone"),
    )
    manifest: dict[str, Any] = {
        "schema_version": "2.0",
        "project_id": int(project_id),
        "project_name": str(row["name"]),
        "plan_id": str(plan.get("plan_id") or ""),
        "profile": profile,
        "settings": settings,
        "storyboard_render_state": storyboard_render_state(storyboard_state, raw_segments),
        "visual_timeline": visual_timeline,
        "visual_timeline_input_hash": visual_timeline_input_hash,
        "visual_items": visual_timeline.get("resolved_items", visual_timeline.get("items", [])),
        "segments": segments,
        "bgm": bgm,
        "bgm_credits": bgm_credits,
        "unresolved_bgm_licenses": unresolved_bgm_licenses,
        "expected_duration_seconds": round(float(visual_timeline.get("resolved_duration_seconds") or 0), 6),
        "manifest_hash": "",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # A migrated/legacy project remains render-current until a human approves
    # the new visual contract.  Do not change its historical manifest hash just
    # because an unapproved Creative Brief row now exists.  Once approved, the
    # brief becomes the explicit Render/VID-27 source of truth and therefore is
    # intentionally part of the new manifest identity.
    if approved_brief:
        manifest["creative_brief"] = creative_brief
        manifest["approved_creative_brief"] = approved_brief
    if str(visual_style_state.get("status") or "") == "approved":
        visual_style = visual_style_state.get("approved") or {}
        visual_style_validation = validate_materialized_visual_style(visual_style)
        if not visual_style_validation["ok"]:
            raise ValueError("invalid approved visual style: " + "; ".join(visual_style_validation["errors"]))
        manifest["visual_style"] = visual_style
        manifest["visual_style_hash"] = visual_style.get("resolved_hash", "")
        manifest["visual_timeline"]["visual_style_hash"] = visual_style.get("resolved_hash", "")
        manifest["visual_timeline"]["approved_visual_style"] = visual_style
        manifest["visual_timeline"]["resolution_hash"] = stable_visual_hash(manifest["visual_timeline"])
    manifest["manifest_hash"] = manifest_hash(manifest)
    validation = validate_render_manifest(manifest)
    if validation["errors"]:
        raise ValueError("invalid render manifest: " + "; ".join(validation["errors"]))
    manifest["validation"] = validation
    return manifest


def compile_render_manifest(cfg: dict, db: Path, project_id: int, profile_id: str | None = None) -> dict[str, Any]:
    manifest = build_render_manifest(cfg, db, project_id, profile_id)
    path = project_dir(cfg, project_id) / "render_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
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
            canonical = get_render_profile(str(profile["profile_id"]))
            for field in ("width", "height", "fps", "video_codec", "pixel_format", "audio_codec", "audio_sample_rate", "audio_channels", "color_primaries", "color_transfer", "color_matrix", "color_range", "hdr_intent"):
                if profile.get(field) != canonical[field]:
                    errors.append(f"profile {field} does not match canonical profile")
        except ValueError as exc:
            errors.append(str(exc))

    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("manifest must contain at least one segment")
        segments = []
    ids = [item.get("segment_id") for item in segments if isinstance(item, dict)]
    for index, segment in enumerate(segments, 1):
        if isinstance(segment, dict) and not str(segment.get("segment_id") or "").strip():
            errors.append(f"segment {index}: segment_id is required")
    if len(ids) != len(set(ids)):
        errors.append("segment_id values must be unique")
    orders = [item.get("order") for item in segments if isinstance(item, dict)]
    for index, order in enumerate(orders, 1):
        if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
            errors.append(f"segment {index}: order must be a positive integer")
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
        if not str(segment.get("clip_id") or "").strip():
            errors.append(f"{segment_id}: clip_id is required")
        video_id = segment.get("video_id")
        if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id <= 0:
            errors.append(f"{segment_id}: video_id must be a positive integer")
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
        source_duration = _number(segment.get("source_duration_seconds"), f"{segment_id} source_duration_seconds", errors)
        timeline_duration = _number(segment.get("timeline_duration_seconds"), f"{segment_id} timeline_duration_seconds", errors)
        if source_duration is not None and source_duration <= 0:
            errors.append(f"{segment_id}: source_duration_seconds must be finite and > 0")
        if timeline_duration is not None and timeline_duration <= 0:
            errors.append(f"{segment_id}: timeline_duration_seconds must be finite and > 0")
        if source_duration is not None and start is not None and end is not None and abs(source_duration - (end - start)) > 0.001:
            errors.append(f"{segment_id}: source_duration_seconds does not match source range")
        if timeline_duration is not None and source_duration is not None and speed is not None and speed > 0 and abs(timeline_duration - (source_duration / speed)) > 0.001:
            errors.append(f"{segment_id}: timeline_duration_seconds does not match source duration and speed")
        audio = segment.get("audio") if isinstance(segment.get("audio"), dict) else {}
        role = audio.get("role", segment.get("audio_role"))
        if role not in ALLOWED_AUDIO_ROLES:
            errors.append(f"{segment_id}: invalid audio_role {role!r}")
        if role == "bgm_only" and not manifest.get("bgm"):
            errors.append(f"{segment_id}: bgm_only requires a selected BGM")
        if timeline_duration is not None:
            duration += timeline_duration

    visual_timeline = manifest.get("visual_timeline") if isinstance(manifest.get("visual_timeline"), dict) else {}
    resolved_visual_duration = _number(visual_timeline.get("resolved_duration_seconds"), "visual_timeline.resolved_duration_seconds", errors) if visual_timeline.get("resolved_duration_seconds") is not None else duration
    if resolved_visual_duration is not None and resolved_visual_duration < duration - 0.001:
        errors.append("visual timeline duration cannot be shorter than segment duration")
    expected = _number(manifest.get("expected_duration_seconds"), "expected_duration_seconds", errors)
    if expected is not None and resolved_visual_duration is not None and abs(expected - resolved_visual_duration) > 0.001:
        errors.append("expected_duration_seconds does not match resolved visual timeline duration")
    for track in manifest.get("bgm", []) or []:
        missing = [key for key in ("source_url", "license_name", "attribution_text") if not str(track.get(key) or "").strip()]
        if missing:
            warnings.append(f"BGM {track.get('title', 'untitled')} license incomplete: {', '.join(missing)}")
        if str(track.get("attribution_status") or "unknown") == "unknown":
            warnings.append(f"BGM {track.get('title', 'untitled')} attribution status is unknown")
        if str(track.get("license_status") or "unverified") != "verified":
            warnings.append(f"BGM {track.get('title', 'untitled')} license is not verified")
    color_values = [(manifest.get("settings") or {}).get("color") or {}]
    color_values.extend(segment.get("color") or {} for segment in segments if isinstance(segment, dict))
    for color in color_values:
        mode = str(color.get("mode") or "none")
        if mode not in {"none", "safe_restore", "warm_food", "manual", "dji_lut", "dji_dlog", "dji_dlog_m"}:
            errors.append(f"unsupported color mode: {mode}")
        if mode in {"dji_lut", "dji_dlog", "dji_dlog_m"} and not str(color.get("lut_path") or "").strip():
            errors.append(f"color mode {mode} requires color.lut_path")
        if mode in {"dji_lut", "dji_dlog", "dji_dlog_m"} and str(color.get("lut_path") or "").strip():
            lut_path = Path(str(color["lut_path"])).expanduser().resolve()
            if not lut_path.is_file():
                errors.append(f"color LUT file does not exist: {lut_path}")
        for field, lower, upper in (("exposure", -1.5, 1.0), ("temperature", -30.0, 30.0), ("tint", -20.0, 20.0), ("contrast", 0.85, 1.15), ("saturation", 0.8, 1.2), ("gamma", 0.85, 1.15), ("highlights", -1.0, 1.0), ("shadows", -1.0, 1.0)):
            if field in color:
                number = _number(color.get(field), f"color {field}", errors)
                if number is not None and not lower <= number <= upper:
                    errors.append(f"color {field} must be between {lower} and {upper}")
    visual_validation = validate_visual_timeline(manifest.get("visual_timeline"))
    errors.extend(f"visual timeline: {item}" for item in visual_validation["errors"])
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


def _manifest_segment(
    project_row: Any,
    order: int,
    segment: dict[str, Any],
    color_settings: dict[str, Any] | None = None,
    *,
    audio_state: dict[str, Any] | None = None,
    cfg: dict | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    source_in = round(float(segment.get("start_seconds") or 0), 3)
    source_out = round(float(segment.get("end_seconds") or 0), 3)
    speed = round(float(segment.get("speed") or 1.0), 6)
    if audio_state is not None and cfg is not None and project_id is not None:
        audio = effective_segment_audio_settings(cfg, project_id, segment, state=audio_state)
        audio.pop("legacy", None)
    else:
        role = normalize_audio_role(segment.get("audio_role") or "lower")
        audio = {"role": role}
    role = str(audio.get("role") or "lower")
    legacy_role = {"keep": "keep_original", "lower": "lower_original", "mute": "mute", "bgm_only": "mute"}.get(role, role)
    result = {
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
        "audio_role": legacy_role,
        "scene_role": str(segment.get("scene_role") or ""),
        "story_position": str(segment.get("story_position") or ""),
        "group": str(segment.get("group") or ""),
        "group_id": str(segment.get("storyboard_group_id") or segment.get("group") or ""),
        "user_notes": str(segment.get("user_notes") or ""),
        "title": str(segment.get("title") or ""),
        "suggested_use": str(segment.get("suggested_use") or ""),
        "duplicate_group": str(segment.get("duplicate_group") or ""),
        # Group titles are display metadata.  Use the stable storyboard group
        # identity so renaming a group cannot change the approved render.
        "group_id": str(segment.get("storyboard_group_id") or segment.get("group_id") or ""),
    }
    if audio_state is not None:
        result["audio"] = audio
    if color_settings is not None:
        result["color"] = color_settings
    return result


def _manifest_bgm(
    cfg: dict,
    db: Path,
    project_id: int,
    settings: dict[str, Any],
    audio_state: dict[str, Any] | None = None,
    *,
    validate_selected_bgm: bool = True,
) -> list[dict[str, Any]]:
    rows = resolve_legacy_project_bgm(db, project_id, settings)
    result = []
    selected_id = None
    if audio_state is not None:
        new_workflow_enabled = bool(audio_state.get("enabled", True))
        bgm_state = effective_project_bgm(audio_state) if new_workflow_enabled else None
        selected_id = int(bgm_state["bgm_id"]) if bgm_state is not None else None
        if not new_workflow_enabled:
            # A disabled workflow returns to the project's legacy relation.
            pass
        elif selected_id is None:
            # An enabled audio workflow with no selected BGM explicitly
            # suppresses the legacy project_bgm relation.
            rows = []
        else:
            try:
                selected = resolve_audio_state_bgm(db, audio_state)
            except ValueError as exc:
                raise BgmPipelineError("selected BGM is not attached to the global library") from exc
            if selected is None:
                raise BgmPipelineError("selected BGM is not available")
            if validate_selected_bgm:
                try:
                    validate_bgm_track({"source_path": selected.get("file_path")}, str(cfg.get("ffprobe_path") or "ffprobe"))
                except BgmPipelineError as exc:
                    raise BgmPipelineError("selected BGM file is missing or unreadable") from exc
            rows = [selected]
    for row in rows:
        item = dict(row)
        if audio_state is not None:
            if audio_state.get("enabled", True):
                bgm_state = dict(audio_state.get("bgm") or {})
                if selected_id is None or int(item["id"]) != selected_id:
                    continue
                override = {
                    "gain_db": bgm_state.get("volume_db", -18.0),
                    "start_seconds": bgm_state.get("start_seconds", 0.0),
                    "loop": bgm_state.get("loop", True),
                    "fade_in_seconds": bgm_state.get("fade_in_seconds", 1.5),
                    "fade_out_seconds": bgm_state.get("fade_out_seconds", 2.0),
                }
            else:
                override = {key: item.get(key) for key in ("gain_db", "start_seconds", "loop", "fade_in_seconds", "fade_out_seconds") if key in item}
        else:
            override = {key: item.get(key) for key in ("gain_db", "start_seconds", "loop", "fade_in_seconds", "fade_out_seconds") if key in item}
        result.append({
            "track_id": int(item["id"]),
            "title": str(item.get("title") or ""),
            "source_path": str(item.get("file_path") or ""),
            "source_url": str(item.get("source_url") or ""),
            "license_name": str(item.get("license_name") or ""),
            "license_url": str(item.get("license_url") or ""),
            "license_source_url": str(item.get("license_source_url") or item.get("license_url") or ""),
            "attribution_status": str(item.get("attribution_status") or "unknown"),
            "license_status": str(item.get("license_status") or "unverified"),
            "license_verified_at": str(item.get("license_verified_at") or ""),
            "verification_source": str(item.get("verification_source") or ""),
            "verification_provenance": str(item.get("verification_provenance") or ""),
            "attribution_text": str(item.get("attribution_text") or ""),
            "gain_db": float(override.get("gain_db", settings.get("audio", {}).get("bgm_gain_db", -18.0))),
            "start_seconds": float(override.get("start_seconds", 0.0)),
            "loop": bool(override.get("loop", True)),
            "fade_in_seconds": float(override.get("fade_in_seconds", 1.0)),
            "fade_out_seconds": float(override.get("fade_out_seconds", 2.0)),
            "duration_seconds": float(item.get("duration_seconds") or 0.0),
        })
    return result


def audio_state_file_is_enabled(state: dict[str, Any], file_exists: bool) -> bool:
    return bool(file_exists and state.get("enabled", True))


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


__all__ = ["ALLOWED_AUDIO_ROLES", "build_render_manifest", "compile_render_manifest", "manifest_hash", "validate_render_manifest"]
