"""Deterministic, text-only StoryInputSnapshot construction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .database import frames, project, project_videos, segments as database_segments
from .project import _read_json, project_dir, project_segments
from .story_profiles import (
    load_project_story_settings,
    resolved_creator_profile,
    story_profile_definition,
)
from .story_context import story_context


STORY_INPUT_SCHEMA_VERSION = 1
STORY_INPUT_PROMPT_VERSION = "project-story-input-v1"
CLIP_AI_SUMMARY_MAX_ITEMS = 3
CLIP_AI_SUMMARY_MAX_UTF8_BYTES = 2048
SEGMENT_AI_SUMMARY_MAX_UTF8_BYTES = 2048


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def story_input_hash(snapshot: Mapping[str, Any]) -> str:
    payload = dict(snapshot)
    payload.pop("input_hash", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _truncate_utf8(value: Any, max_bytes: int) -> str:
    """Bound machine-authored evidence without corrupting UTF-8 text."""

    text = str(value or "").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    budget = max(0, max_bytes - len(suffix_bytes))
    prefix = encoded[:budget]
    while prefix:
        try:
            return prefix.decode("utf-8").rstrip() + suffix
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return suffix if len(suffix_bytes) <= max_bytes else ""


def _bounded_ai_summary(
    values: Any,
    *,
    max_items: int,
    max_utf8_bytes: int,
) -> str:
    """Keep the first distinct AI observations under a deterministic byte cap."""

    items = values if isinstance(values, (list, tuple)) else [values]
    selected: list[str] = []
    seen: set[str] = set()
    for value in items:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        candidate = " / ".join([*selected, text])
        if len(candidate.encode("utf-8")) <= max_utf8_bytes:
            selected.append(text)
        else:
            remaining = max_utf8_bytes - len(" / ".join(selected).encode("utf-8"))
            if selected:
                remaining -= len(" / ".encode("utf-8"))
            truncated = _truncate_utf8(text, remaining)
            if truncated:
                selected.append(truncated)
            break
        if len(selected) >= max_items:
            break
    return " / ".join(selected)


def _segment_visual_summary(row: Mapping[str, Any]) -> str:
    """Resolve only evidence that belongs to this stable segment."""

    for key in ("segment_visual_summary", "vision_summary", "visual_summary", "title", "reason"):
        text = str(row.get(key) or "").strip()
        if text:
            return _truncate_utf8(text, SEGMENT_AI_SUMMARY_MAX_UTF8_BYTES)
    return ""


def _segment_story_context(user_summary: str, ai_visual_summary: str, fallback_activity: str) -> dict[str, Any]:
    """Keep routing provenance without duplicating the segment summary text."""

    context = story_context(user_summary, ai_visual_summary, fallback_activity)
    result = {
        "effective_summary_source": context["effective_summary_source"],
        "activity_source": context["activity_source"],
        "avoided_activities": context["avoided_activities"],
        "guidance_applied": context["guidance_applied"],
    }
    if context["user_summary"]:
        result["user_summary"] = context["user_summary"]
    return result


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


def _effective_segment(
    row: Mapping[str, Any],
    clip_order: int,
    *,
    effective_audio: Mapping[str, Any] | None = None,
    user_audio_decision: str | None = None,
) -> dict[str, Any]:
    segment_id = str(row.get("segment_id") or row.get("segment_uuid") or "").strip()
    if not segment_id:
        return {}
    user_summary = str(row.get("user_summary") or "").strip()
    ai_visual_summary = _segment_visual_summary(row)
    tags = [str(tag).strip() for tag in (row.get("tags") or []) if str(tag).strip()] if isinstance(row.get("tags"), list) else [item.strip() for item in str(row.get("tags") or "").split(",") if item.strip()]
    fallback_activity = " ".join([str(row.get("activity") or ""), str(row.get("group") or ""), *tags]).strip()
    context = _segment_story_context(user_summary, ai_visual_summary, fallback_activity)
    audio = dict(effective_audio or {})
    effective_audio_role = str(audio.get("role") or row.get("audio_role") or "")
    audio_source = str(audio.get("source") or ("legacy" if audio.get("legacy") else "audio_settings.default"))
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
        "visual_summary": ai_visual_summary,
        "action": str(row.get("action") or ""),
        "shot_role": str(row.get("shot_role") or row.get("suggested_use") or ""),
        "technical_quality": _technical_quality(row.get("technical_quality") or row.get("technical_quality_json")),
        "duplicate_group": str(row.get("duplicate_group") or ""),
        "natural_audio_recommendation": str(row.get("natural_audio_recommendation") or "unknown"),
        "audio": {
            "ai_recommendation": str(row.get("natural_audio_recommendation") or "unknown"),
            "user_decision": user_audio_decision,
            "effective_role": effective_audio_role,
            "source": audio_source,
        },
        "effective_audio_role": effective_audio_role,
        "confidence": round(float(row.get("confidence") or row.get("score") or 0), 6),
        "tags": sorted(tags),
        "activity": str(row.get("activity") or row.get("group") or ""),
        "time_of_day": str(row.get("time_of_day") or ""),
        "story_context": context,
        "human_override": {
            "include": bool(row.get("include", row.get("included", True))),
            "manual_order": int(row.get("manual_order") or row.get("order") or 0),
            "scene_role": str(row.get("scene_role") or ""),
            "story_position": str(row.get("story_position") or ""),
            "audio_role": effective_audio_role,
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
    from .audio_state import authoritative_segment_audio_decision, effective_project_audio_state, effective_segment_audio_settings

    audio_state = effective_project_audio_state(dict(cfg), int(project_id), db)
    story_profile = story_profile_definition(str(settings["profile_id"]))
    creator_profile = resolved_creator_profile(cfg, settings)
    plan_path = project_dir(cfg, int(project_id)) / "project_plan.json"
    plan = _read_json(plan_path)
    clips = [dict(row) for row in project_videos(db, int(project_id))]
    video_by_id = {int(row.get("id") or 0): row for row in clips}
    segment_evidence_by_uuid = {
        str(segment["segment_uuid"] or ""): dict(segment)
        for clip in clips
        for segment in database_segments(db, int(clip.get("id") or 0))
        if str(segment["segment_uuid"] or "").strip()
    }
    ai_summary_by_video = {
        int(row.get("id") or 0): _bounded_ai_summary(
            [
                frame["vision_summary"]
                for frame in sorted(
                    frames(db, int(row.get("id") or 0)),
                    key=lambda item: (float(item["timestamp_seconds"] or 0), int(item["id"] or 0)),
                )
            ],
            max_items=CLIP_AI_SUMMARY_MAX_ITEMS,
            max_utf8_bytes=CLIP_AI_SUMMARY_MAX_UTF8_BYTES,
        )
        for row in clips
    }
    clip_order = {int(row["id"]): int(row.get("sort_order") or index) for index, row in enumerate(clips, 1)}
    rows = project_segments(cfg, int(project_id), plan, apply_storyboard=True, db=db)
    effective_segments = []
    for row in rows:
        video = video_by_id.get(int(row.get("video_id") or 0), {})
        segment_id = str(row.get("segment_id") or row.get("segment_uuid") or "")
        segment_evidence = segment_evidence_by_uuid.get(segment_id, {})
        segment_user_summary = str(row.get("user_summary") or "")
        effective_row = {
            **video,
            **segment_evidence,
            **row,
            # Clip guidance remains complete in clips[]. Segment context must
            # not inherit and repeat clip-wide summaries for every segment.
            "user_summary": segment_user_summary,
            "ai_visual_summary": _segment_visual_summary(row),
        }
        effective_audio = effective_segment_audio_settings(dict(cfg), int(project_id), effective_row, state=audio_state)
        user_audio_decision = authoritative_segment_audio_decision(
            dict(cfg), int(project_id), str(effective_row.get("segment_id") or ""), state=audio_state
        )
        item = _effective_segment(
            effective_row,
            clip_order.get(int(row.get("video_id") or 0), 0),
            effective_audio=effective_audio,
            user_audio_decision=user_audio_decision,
        )
        if item:
            effective_segments.append(item)
    effective_segments.sort(key=lambda item: (int(item["clip_order"]), int(item["human_override"]["manual_order"] or 999999), item["segment_uuid"]))
    clips_payload = []
    for row in clips:
        clip_ai_summary = ai_summary_by_video.get(
            int(row.get("id") or 0),
            _bounded_ai_summary(
                str(row.get("ai_visual_summary") or row.get("visual_summary") or ""),
                max_items=CLIP_AI_SUMMARY_MAX_ITEMS,
                max_utf8_bytes=CLIP_AI_SUMMARY_MAX_UTF8_BYTES,
            ),
        )
        user_summary = str(row.get("user_summary") or "")
        clips_payload.append({
            "project_media_uuid": str(row.get("project_media_uuid") or ""),
            "clip_order": clip_order.get(int(row.get("id") or 0), 0),
            "duration_seconds": round(float(row.get("duration_seconds") or 0), 6),
            "category": str(row.get("category_override") or row.get("category") or ""),
            "user_summary": user_summary,
            "ai_visual_summary": clip_ai_summary,
            "effective_summary": user_summary or clip_ai_summary,
            "story_context": story_context(
                user_summary,
                clip_ai_summary,
                str(row.get("category_override") or row.get("category") or ""),
            ),
        })
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
            "contract_version": "story-context-v2",
            "sources": [
                "project_videos.user_summary",
                "frames.vision_summary",
                "segments.title",
                "segments.action",
                "segments.shot_role",
                "segments.tags",
                "segments.technical_quality_json",
                "segment_human_override",
            ],
            "clip_ai_summary_policy": {
                "selection": "first_distinct_timestamp_order",
                "max_items": CLIP_AI_SUMMARY_MAX_ITEMS,
                "max_utf8_bytes": CLIP_AI_SUMMARY_MAX_UTF8_BYTES,
            },
            "segment_ai_summary_policy": {
                "selection": "segment_evidence_priority",
                "max_utf8_bytes": SEGMENT_AI_SUMMARY_MAX_UTF8_BYTES,
            },
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
