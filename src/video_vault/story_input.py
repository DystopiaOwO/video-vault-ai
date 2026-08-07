"""Deterministic, text-only StoryInputSnapshot construction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .database import frames, project, project_videos
from .project import _read_json, project_dir, project_segments
from .story_profiles import (
    load_project_story_settings,
    resolved_creator_profile,
    story_profile_definition,
)
from .story_context import story_context


STORY_INPUT_SCHEMA_VERSION = 1
STORY_INPUT_PROMPT_VERSION = "project-story-input-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def story_input_hash(snapshot: Mapping[str, Any]) -> str:
    payload = dict(snapshot)
    payload.pop("input_hash", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _technical_quality(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): value[key] for key in sorted(value)}
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _effective_segment(row: Mapping[str, Any], clip_order: int) -> dict[str, Any]:
    segment_id = str(row.get("segment_id") or row.get("segment_uuid") or "").strip()
    if not segment_id:
        return {}
    user_summary = str(row.get("user_summary") or "").strip()
    ai_visual_summary = str(row.get("ai_visual_summary") or row.get("visual_summary") or "").strip()
    context = story_context(user_summary, ai_visual_summary, str(row.get("activity") or row.get("group") or ""))
    return {
        "segment_uuid": segment_id,
        "project_media_uuid": str(row.get("project_media_id") or row.get("project_media_uuid") or ""),
        "clip_id": str(row.get("clip_id") or ""),
        "clip_order": int(row.get("clip_order") or row.get("order") or clip_order or 0),
        "source_start_seconds": round(float(row.get("start_seconds") or 0), 6),
        "source_end_seconds": round(float(row.get("end_seconds") or 0), 6),
        "source_duration_seconds": round(max(0.0, float(row.get("end_seconds") or 0) - float(row.get("start_seconds") or 0)), 6),
        "speed": round(float(row.get("speed") or 1.0), 6),
        "timeline_duration_seconds": round(float(row.get("estimated_output_seconds") or row.get("timeline_duration_seconds") or 0), 6),
        "title": str(row.get("title") or ""),
        "action": str(row.get("action") or ""),
        "shot_role": str(row.get("shot_role") or row.get("suggested_use") or ""),
        "technical_quality": _technical_quality(row.get("technical_quality") or row.get("technical_quality_json")),
        "duplicate_group": str(row.get("duplicate_group") or ""),
        "natural_audio_recommendation": str(row.get("natural_audio_recommendation") or "unknown"),
        "confidence": round(float(row.get("confidence") or row.get("score") or 0), 6),
        "tags": sorted(str(tag).strip() for tag in (row.get("tags") or []) if str(tag).strip()) if isinstance(row.get("tags"), list) else sorted(str(row.get("tags") or "").split(",")),
        "activity": str(row.get("activity") or row.get("group") or ""),
        "time_of_day": str(row.get("time_of_day") or ""),
        "story_context": context,
        "human_override": {
            "include": bool(row.get("include", row.get("included", True))),
            "manual_order": int(row.get("manual_order") or row.get("order") or 0),
            "scene_role": str(row.get("scene_role") or ""),
            "story_position": str(row.get("story_position") or ""),
            "audio_role": str(row.get("audio_role") or ""),
            "user_notes": str(row.get("user_notes") or row.get("notes") or ""),
            "locked": bool(row.get("locked", False)),
        },
    }


def validate_story_input_snapshot(snapshot: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(snapshot.get("schema_version") or 0) != STORY_INPUT_SCHEMA_VERSION:
        errors.append("story input schema version 不支援")
    if not str(snapshot.get("input_hash") or ""):
        errors.append("缺少 input_hash")
    if story_input_hash(snapshot) != snapshot.get("input_hash"):
        errors.append("input_hash 與內容不一致")
    if not str(snapshot.get("project_id") or ""):
        errors.append("缺少 project_id")
    segments = list(snapshot.get("segments") or [])
    ids = [str(item.get("segment_uuid") or "") for item in segments]
    if not ids or any(not item for item in ids):
        errors.append("StoryInputSnapshot 必須包含穩定 segment UUID")
    if len(ids) != len(set(ids)):
        errors.append("StoryInputSnapshot 含重複 segment UUID")
    if list(snapshot.get("ordered_segment_uuids") or []) != ids:
        errors.append("ordered_segment_uuids 與 segments 順序不一致")
    for item in segments:
        if any(key in item for key in ("source_file", "source_path", "frame_path", "image_url", "image_base64", "frame_bytes")):
            errors.append(f"segment {item.get('segment_uuid')} 含禁止的路徑或影像資料")
    return errors


def build_story_input_snapshot(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    project_row = project(db, int(project_id))
    if not project_row:
        raise ValueError(f"project not found: {project_id}")
    project_data = dict(project_row)
    settings = load_project_story_settings(cfg, db, int(project_id))
    story_profile = story_profile_definition(str(settings["profile_id"]))
    creator_profile = resolved_creator_profile(cfg, settings)
    plan_path = project_dir(cfg, int(project_id)) / "project_plan.json"
    plan = _read_json(plan_path)
    clips = [dict(row) for row in project_videos(db, int(project_id))]
    video_by_id = {int(row.get("id") or 0): row for row in clips}
    ai_summary_by_video = {
        int(row.get("id") or 0): " / ".join(
            str(frame["vision_summary"] or "").strip()
            for frame in frames(db, int(row.get("id") or 0))
            if str(frame["vision_summary"] or "").strip()
        )
        for row in clips
    }
    clip_order = {int(row["id"]): int(row.get("sort_order") or index) for index, row in enumerate(clips, 1)}
    rows = project_segments(cfg, int(project_id), plan, apply_storyboard=True, db=db)
    effective_segments = []
    for row in rows:
        video = video_by_id.get(int(row.get("video_id") or 0), {})
        item = _effective_segment({**video, "ai_visual_summary": ai_summary_by_video.get(int(row.get("video_id") or 0), ""), **row}, clip_order.get(int(row.get("video_id") or 0), 0))
        if item:
            effective_segments.append(item)
    effective_segments.sort(key=lambda item: (int(item["clip_order"]), int(item["human_override"]["manual_order"] or 999999), item["segment_uuid"]))
    clips_payload = [
        {
            "project_media_uuid": str(row.get("project_media_uuid") or ""),
            "clip_order": clip_order.get(int(row.get("id") or 0), 0),
            "duration_seconds": round(float(row.get("duration_seconds") or 0), 6),
            "category": str(row.get("category_override") or row.get("category") or ""),
            "user_summary": str(row.get("user_summary") or ""),
            "ai_visual_summary": ai_summary_by_video.get(int(row.get("id") or 0), str(row.get("ai_visual_summary") or row.get("visual_summary") or "")),
            "effective_summary": str(row.get("user_summary") or row.get("project_summary") or row.get("visual_summary") or ""),
            "story_context": story_context(
                str(row.get("user_summary") or ""),
                str(row.get("ai_visual_summary") or row.get("visual_summary") or ""),
                str(row.get("category_override") or row.get("category") or ""),
            ),
        }
        for row in clips
    ]
    snapshot: dict[str, Any] = {
        "schema_version": STORY_INPUT_SCHEMA_VERSION,
        "prompt_version": STORY_INPUT_PROMPT_VERSION,
        "project_id": int(project_id),
        "project_identity": {"project_id": int(project_id), "name": str(project_data.get("name") or "")},
        "project_revision": int(project_data.get("project_revision") or 1),
        "content_type": str(project_data.get("content_type") or "diary_montage"),
        "creator_profile": creator_profile,
        "creator_profile_version": int(creator_profile.get("profile_version") or 1),
        "story_profile": story_profile,
        "story_profile_id": str(settings["profile_id"]),
        "story_profile_version": int(settings.get("profile_version") or story_profile.get("profile_version") or 1),
        "project_intent": str(settings.get("project_intent") or ""),
        "itinerary": str(settings.get("itinerary") or ""),
        "desired_sequence": list(settings.get("desired_sequence") or []),
        "desired_pacing": str(settings.get("desired_pacing") or ""),
        "must_keep": list(settings.get("must_keep") or []),
        "exclude_guidance": list(settings.get("exclude_guidance") or []),
        "story_context_provenance": {
            "contract_version": "story-context-v1",
            "sources": ["project_videos.user_summary", "frames.vision_summary", "segment.tags"],
            "filename_semantics": False,
        },
        "clips": sorted(clips_payload, key=lambda item: (int(item["clip_order"]), item["project_media_uuid"])),
        "segments": effective_segments,
        "ordered_segment_uuids": [item["segment_uuid"] for item in effective_segments],
    }
    snapshot["input_hash"] = story_input_hash(snapshot)
    errors = validate_story_input_snapshot(snapshot)
    if errors:
        raise ValueError("；".join(errors))
    return snapshot


__all__ = [
    "STORY_INPUT_SCHEMA_VERSION",
    "STORY_INPUT_PROMPT_VERSION",
    "build_story_input_snapshot",
    "story_input_hash",
    "validate_story_input_snapshot",
]
