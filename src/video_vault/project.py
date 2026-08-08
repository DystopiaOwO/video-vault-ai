from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from pathlib import Path
import json
import math
import os
import re
import shutil
import uuid

from .bgm import recommend_bgm_for_groups
from .database import connect, create_project_row, frames, init_db, project, project_bgm_tracks, project_videos, projects, segments, set_project_status, set_project_videos
from .story_context import story_context
from .project_lifecycle import check_base_revision, current_revision, project_commit
from .duration_budget import apply_duration_budget
from .visual_timeline import build_visual_timeline
from .sampling import estimate_sampling_count, resolved_sampling_policy


def create_project(db: Path, name: str, video_ids: list[int], kind: str = "auto", category: str = "unknown", content_type: str = "diary_montage", platform: str = "YouTube", target_duration_seconds: float = 0) -> int:
    init_db(db)
    project_id = create_project_row(db, name.strip() or "未命名專案", kind, category, content_type, platform, target_duration_seconds)
    set_project_videos(db, project_id, video_ids)
    return project_id


def list_projects(db: Path) -> list[dict]:
    init_db(db)
    return [dict(row) for row in projects(db)]


def sync_project_files(cfg: dict, db: Path, project_id: int) -> list[dict]:
    # ponytail: copy-once project source files; hardlinks can come later if disk usage matters.
    from .perception_runs import perception_states_for_project

    rows = [dict(v) for v in project_videos(db, project_id)]
    perception_states = perception_states_for_project(db, project_id)
    source_dir = project_dir(cfg, project_id) / "source"
    clips_dir = project_dir(cfg, project_id) / "clips"
    result = []
    for order, video in enumerate(rows, 1):
        clip_id = f"clip_{order:03}"
        project_media_id = str(video.get("project_media_uuid") or f"video_{video['id']}")
        storage_id = f"media_{project_media_id}"
        src = Path(video["current_path"])
        dst = src if src.parent.resolve() == source_dir.resolve() else source_dir / f"{storage_id}_{src.name}"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
        stable_clip_dir = clips_dir / storage_id
        display_clip_dir = clips_dir / clip_id
        stable_clip_dir.mkdir(parents=True, exist_ok=True)
        display_clip_dir.mkdir(parents=True, exist_ok=True)
        display_name = str(video.get("filename") or src.name)
        perception_state = perception_states.get(int(video["id"]), {})
        default_sampling_policy = resolved_sampling_policy(cfg)
        current_sampling = perception_state.get("current_sampling_manifest") or {}
        ai_visual_summary = _visual_summary(db, int(video["id"]))
        user_summary = str(video.get("user_summary") or "").strip()
        effective_summary = user_summary or ai_visual_summary
        effective_summary_source = "user" if user_summary else ("ai" if ai_visual_summary else "none")
        data = {
            "clip_id": clip_id,
            "project_media_id": project_media_id,
            "storage_id": storage_id,
            "project_id": project_id,
            "video_id": video["id"],
            "filename": display_name,
            "physical_filename": dst.name,
            "source_path": str(dst),
            "original_source_path": video["current_path"],
            "order": order,
            "included": True,
            "duration_seconds": video["duration_seconds"],
            "detected_category": video["category"],
            "time_of_day": _time_label({**video, "filename": display_name}),
            "status": perception_state.get("current_status") or video.get("status") or "uploaded",
            "segment_count": len(list(segments(db, int(video["id"])))),
            "visual_summary": ai_visual_summary,
            "ai_visual_summary": ai_visual_summary,
            "user_summary": user_summary,
            "user_summary_updated_at": video.get("user_summary_updated_at"),
            "user_summary_migration_state": video.get("user_summary_migration_state") or "none",
            "effective_summary": effective_summary,
            "effective_summary_source": effective_summary_source,
            "analysis_current": bool(perception_state.get("analysis_current")),
            "perception_run": perception_state,
            "sampling": {
                "default_policy": default_sampling_policy,
                "estimated_frame_count": estimate_sampling_count(
                    float(video.get("duration_seconds") or 0),
                    default_sampling_policy,
                ),
                "current": current_sampling,
            },
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        _atomic_write_text(stable_clip_dir / "clip.json", payload)
        # clip_001 remains a display alias for compatibility; stable references use project_media_id.
        _atomic_write_text(display_clip_dir / "clip.json", payload)
        result.append(data)
    return result


def build_project_plan(cfg: dict, db: Path, project_id: int, *, base_revision: int | None = None) -> dict:
    with project_commit(db, project_id, base_revision) as commit:
        old = _read_json(project_dir(cfg, project_id) / "project_plan.json")
        result = _build_project_plan(cfg, db, project_id)
        commit.record_changed(_plan_revision_payload(old) != _plan_revision_payload(result))
        return result


def _plan_revision_payload(plan: dict | None) -> dict:
    """Exclude generated version/timestamp fields from no-op comparison."""
    value = deepcopy(plan or {})
    for key in ("created_at", "plan_id", "version", "parent_plan_id", "created_reason"):
        value.pop(key, None)
    return value


def revise_project(cfg: dict, db: Path, project_id: int, notes: str, *, base_revision: int | None = None) -> dict:
    """Atomically apply revision notes and rebuild the project plan.

    The base revision is checked before any note is written.  File snapshots
    keep the old review/plan state recoverable if the rebuild fails halfway.
    """
    folder = project_dir(cfg, project_id)
    tracked = [
        folder / "project_plan.json",
        folder / "project_script.md",
        folder / "review_status.json",
        folder / "feedback" / "revision_notes.md",
        folder / "plans" / "latest.json",
    ]
    tracked.extend((folder / "plans").glob("*.json") if (folder / "plans").exists() else [])
    tracked.extend((folder / "plans").glob("*.md") if (folder / "plans").exists() else [])
    tracked.extend((folder / "feedback").glob("revision_*.md") if (folder / "feedback").exists() else [])
    snapshots = {path: path.read_bytes() for path in set(tracked) if path.is_file()}
    old_status = None
    row = project(db, project_id)
    if row:
        old_status = str(row["status"] or "")
    text = (notes or "").strip()
    with project_commit(db, project_id, base_revision) as commit:
        old_plan = _read_json(folder / "project_plan.json")
        old_notes = _revision_notes(cfg, project_id)
        try:
            if text:
                save_revision_notes(cfg, project_id, text)
            result = _build_project_plan(cfg, db, project_id)
        except Exception:
            cleanup = set(tracked)
            cleanup.update((folder / "plans").glob("*.json"))
            cleanup.update((folder / "plans").glob("*.md"))
            cleanup.update((folder / "feedback").glob("revision_*.md"))
            for path in cleanup:
                if path not in snapshots and path.is_file():
                    path.unlink(missing_ok=True)
            for path, payload in snapshots.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            if old_status:
                set_project_status(db, project_id, old_status)
            raise
        commit.record_changed(
            old_notes != _revision_notes(cfg, project_id)
            or _plan_revision_payload(old_plan) != _plan_revision_payload(result)
            or old_status != str((project(db, project_id)["status"] if project(db, project_id) else "") or "")
        )
        return result


def _build_project_plan(cfg: dict, db: Path, project_id: int) -> dict:
    init_db(db)
    row = project(db, project_id)
    if not row:
        raise ValueError(f"project not found: {project_id}")
    clips = sync_project_files(cfg, db, project_id)
    by_video_id = {int(c["video_id"]): c for c in clips}
    itinerary = _read_json(project_dir(cfg, project_id) / "plans" / "itinerary.json").get("chapters", [])
    groups: dict[str, dict] = {}
    for video in project_videos(db, project_id):
        video = dict(video)
        clip = by_video_id[int(video["id"])]
        video_segments = [dict(seg) for seg in segments(db, int(video["id"]))]
        if not video_segments:
            chapter = _chapter_for(itinerary, clip["clip_id"]) if itinerary else None
            fallback_activity = chapter["label"] if chapter else ("已感知但無推薦片段" if clip["status"] == "perceived" else "未分析")
            context = story_context(clip["user_summary"], clip["ai_visual_summary"], fallback_activity)
            activity = context["activity"]
            label = activity if chapter or context["guidance_applied"] else f"{clip['time_of_day']} / {activity}"
            group = groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))
            group["clips"].append(_clip_summary(clip))
            group["story_context"].append(_story_context_usage(clip, context))
            continue
        for seg in video_segments:
            chapter = _chapter_for(itinerary, clip["clip_id"]) if itinerary else None
            fallback_activity = chapter["label"] if chapter else _activity(seg.get("tags", ""), video.get("category", ""))
            context = story_context(clip["user_summary"], clip["ai_visual_summary"], fallback_activity)
            activity = context["activity"]
            label = activity if chapter or context["guidance_applied"] else f"{clip['time_of_day']} / {activity}"
            group = groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))
            group["clips"].append(_clip_summary(clip))
            group["story_context"].append(_story_context_usage(clip, context))
            group["segments"].append(
                {
                    "segment_id": str(seg.get("segment_uuid") or ""),
                    "segment_revision": int(seg.get("revision") or 1),
                    "clip_id": clip["clip_id"],
                    "project_media_id": clip["project_media_id"],
                    "video_id": video["id"],
                    "source_file": clip["source_path"],
                    "start_seconds": seg["start_seconds"],
                    "end_seconds": seg["end_seconds"],
                    "title": seg["title"],
                    "suggested_use": seg["suggested_use"],
                    "tags": [tag for tag in (seg.get("tags") or "").split(",") if tag],
                    "score": seg["score"],
                    "story_context": context,
                    "speed": 1.0,
                    "estimated_output_seconds": round(max(0.1, float(seg["end_seconds"] or 0) - float(seg["start_seconds"] or 0)), 3),
                    "include": bool(seg.get("include", True)),
                }
            )
    ordered = sorted(groups.values(), key=lambda g: (int(g.get("order", 999)), _time_rank(g["time_of_day"]), g["activity"], g["label"]))
    for group in ordered:
        group["clips"] = _dedupe(group["clips"])
        group["story_context"] = _dedupe_story_context(group["story_context"])
        group["segments"].sort(key=lambda s: (s["clip_id"], float(s["start_seconds"] or 0)) if itinerary or project_info_is_travel(row) else (-float(s["score"] or 0), s["clip_id"], float(s["start_seconds"] or 0)))
    from .storyboard import load_storyboard

    project_info = dict(row)
    storyboard = load_storyboard(cfg, project_id) or {}
    storyboard_segments = storyboard.get("segments") if isinstance(storyboard, dict) else {}
    effective_segments = {
        str(item.get("segment_id")): item
        for item in project_segments(cfg, project_id, {"groups": ordered}, apply_storyboard=True, db=db)
        if str(item.get("segment_id") or "")
    }
    for group in ordered:
        for segment in group.get("segments", []) or []:
            effective = effective_segments.get(str(segment.get("segment_id") or ""))
            if not effective:
                continue
            for key in ("start_seconds", "end_seconds", "speed", "include", "locked"):
                if key in effective:
                    segment[key] = effective[key]
            speed = max(0.25, float(segment.get("speed") or 1.0))
            segment["estimated_output_seconds"] = round(
                max(
                    0.1,
                    (float(segment.get("end_seconds") or 0) - float(segment.get("start_seconds") or 0)) / speed,
                ),
                3,
            )
    duration_budget = apply_duration_budget(
        ordered,
        float(project_info.get("target_duration_seconds") or 0),
        locked_segments=storyboard_segments if isinstance(storyboard_segments, dict) else {},
    )
    visual_timeline = build_visual_timeline(ordered)
    story_context_usage = _dedupe_story_context(
        [
            context
            for group in ordered
            for context in group.get("story_context", [])
            if context.get("user_summary")
        ]
    )
    pipeline = pipeline_for_project(project_info)
    bgm_recommendations = recommend_bgm_for_groups(cfg, db, project_id, project_info, ordered)
    by_group = {item["group"]: item for item in bgm_recommendations}
    for group in ordered:
        if group["label"] in by_group:
            group["bgm"] = by_group[group["label"]]["track"]
    bgm = [dict(track) for track in project_bgm_tracks(db, project_id)]
    revision_notes = _revision_notes(cfg, project_id)
    plan = {
        "project_id": project_id,
        "name": project_info["name"],
        "category": project_info["category"],
        "content_type": project_info["content_type"],
        "pipeline_id": pipeline.get("pipeline_id", ""),
        "pipeline": pipeline,
        "platform": project_info["platform"],
        "target_duration_seconds": project_info["target_duration_seconds"],
        "status": "needs_review",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "clips": [_clip_summary(c) for c in clips],
        "story_context_usage": story_context_usage,
        "groups": ordered,
        "bgm": bgm,
        "bgm_recommendations": bgm_recommendations,
        "title_cards": _title_cards(project_info, ordered),
        "duration_budget": duration_budget,
        "visual_timeline": visual_timeline,
        "visual_items": visual_timeline["items"],
        "revision_notes": revision_notes,
        "feedback_applied": [
            f"{item['clip_id']} 使用 user_summary 指引故事分組"
            for item in story_context_usage
            if item.get("guidance_applied")
        ],
        "feedback_unresolved": ["已記錄審核備註；自動重排片段留到 segment review 階段。"] if revision_notes else [],
    }
    write_project_files(cfg, plan)
    append_decision(cfg, project_id, "plan_created", f"建立 {plan['content_type']} 故事計畫", "build_project_plan", plan_id=plan.get("plan_id", ""), confidence=1.0)
    write_checkpoint(cfg, project_id, "plan_created", "passed", plan_id=plan.get("plan_id", ""), outputs=["project_plan.json", "project_script.md"])
    mark_project_needs_review(cfg, db, project_id)
    return plan


def project_detail(cfg: dict, db: Path, project_id: int) -> dict:
    init_db(db)
    row = project(db, project_id)
    if not row:
        return {}
    folder = project_dir(cfg, project_id)
    plan = _read_json(folder / "project_plan.json")
    ok, reason = can_project_render(cfg, db, project_id)
    from .color_consistency import color_state_for_api, load_project_color_state
    from .audio_state import audio_state_for_api
    from .storyboard import storyboard_for_api
    from .story_generation import project_story_detail

    public_bgm = []
    for bgm_row in project_bgm_tracks(db, project_id):
        bgm_data = dict(bgm_row)
        public_bgm.append({
            key: bgm_data.get(key)
            for key in (
                "id", "title", "artist", "source_url", "license_name", "license_url",
                "attribution_required", "attribution_text", "mood", "duration_seconds",
                "attribution_status", "license_status", "license_verified_at", "license_source_url",
                "verification_source", "verification_provenance",
            )
        })

    clips = sync_project_files(cfg, db, project_id)
    publishing = any(
        str(clip.get("perception_run", {}).get("current_status") or "") == "publishing"
        for clip in clips
    )
    public_plan = {} if publishing else _public_plan_bgm(plan)
    public_segments = []
    for segment in ([] if publishing else project_segments(cfg, project_id, plan, db=db)):
        public_segment = dict(segment)
        source = public_segment.get("source_file", "")
        public_segment["source_filename"] = Path(str(source)).name if source else ""
        public_segments.append(public_segment)
    return {
        "project": dict(row),
        "project_revision": int(row["project_revision"] or 1),
        "clips": clips,
        "perception_runs": [clip.get("perception_run", {}) for clip in clips],
        "bgm": public_bgm,
        "plan": public_plan,
        "workflow": project_workflow(cfg, db, project_id, plan),
        "segments": public_segments,
        "review": _read_json(folder / "review_status.json"),
        "can_render": ok,
        "render_gate_reason": reason,
        "script": "" if publishing else ((folder / "project_script.md").read_text(encoding="utf-8") if (folder / "project_script.md").exists() else ""),
        "folder": str(folder),
        "color": color_state_for_api(cfg, project_id, load_project_color_state(cfg, project_id)),
        "audio": audio_state_for_api(cfg, project_id, db),
        "storyboard": storyboard_for_api(cfg, db, project_id),
        "story": project_story_detail(cfg, db, project_id),
    }


def project_workflow(cfg: dict, db: Path, project_id: int, plan: dict | None = None) -> dict:
    folder = project_dir(cfg, project_id)
    clips = sync_project_files(cfg, db, project_id)
    plan = plan or _read_json(folder / "project_plan.json")
    review = _read_json(folder / "review_status.json")
    project_segments(cfg, project_id, plan, db=db)
    outputs = folder / "output"
    stages = [
        _stage("import", "匯入素材", bool(clips), [folder / "source"]),
        _stage("perception", "內容感知", bool(clips) and all(c.get("analysis_current") for c in clips), [folder / "clips"]),
        _stage("story", "故事整理", bool(plan.get("groups")), [folder / "project_plan.json", folder / "project_script.md"]),
        _stage("review", "人工審核", review.get("approved_by_user") is True, [folder / "feedback", folder / "review_status.json"]),
        _stage("handoff", "剪輯交接", (outputs / "opencut_handoff").exists() or (outputs / "hyperframes").exists(), [outputs / "opencut_handoff", outputs / "hyperframes"]),
        _stage("render", "正式輸出", any(outputs.glob("**/*.mp4")) if outputs.exists() else False, [outputs]),
    ]
    if (folder / "storyboard.json").exists():
        stages.insert(3, _stage("storyboard", "分鏡審核", review.get("approved_by_user") is True, [folder / "storyboard.json", folder / "cache" / "storyboard"]))
    return {"style": "openmontage_skeleton", "current": next((s["id"] for s in stages if s["status"] != "done"), "done"), "stages": stages}


def _public_plan_bgm(plan: dict) -> dict:
    """Remove internal paths from BGM-only fields while preserving clip data."""
    result = deepcopy(plan)
    result["bgm"] = [_public_bgm_row(item) for item in result.get("bgm", []) if isinstance(item, dict)]
    recommendations = []
    for item in result.get("bgm_recommendations", []) or []:
        if not isinstance(item, dict):
            continue
        public_item = dict(item)
        if isinstance(public_item.get("track"), dict):
            public_item["track"] = _public_bgm_row(public_item["track"])
        recommendations.append(public_item)
    result["bgm_recommendations"] = recommendations
    for group in result.get("groups", []) or []:
        if isinstance(group, dict) and isinstance(group.get("bgm"), dict):
            group["bgm"] = _public_bgm_row(group["bgm"])
    return result


def _public_bgm_row(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "id", "track_id", "title", "artist", "source_url", "license_name", "license_url",
            "attribution_required", "attribution_text", "mood", "duration_seconds",
            "attribution_status", "license_status", "license_verified_at", "license_source_url",
            "verification_source", "verification_provenance",
        )
        if key in row
    }


def project_segments(cfg: dict, project_id: int, plan: dict, *, apply_storyboard: bool = True, db: Path | None = None) -> list[dict]:
    review_rows = _segment_review(cfg, project_id)
    reviews = {str(row.get("segment_id") or ""): row for row in review_rows}
    identity_rows = _project_segment_identity_rows(db, project_id) if db is not None else {}
    rows = []
    for group in plan.get("groups", []):
        for order, seg in enumerate(group.get("segments", []), 1):
            legacy_segment_id = _legacy_segment_id(seg)
            segment_id = str(seg.get("segment_id") or seg.get("segment_uuid") or "")
            if not segment_id and identity_rows:
                segment_id = _resolve_plan_segment_identity(seg, identity_rows)
            if not segment_id:
                segment_id = legacy_segment_id
            review = reviews.get(segment_id) or reviews.get(legacy_segment_id) or {}
            rows.append(
                {
                    **seg,
                    "segment_id": segment_id,
                    "legacy_segment_id": legacy_segment_id,
                    "identity_aliases": [legacy_segment_id] if legacy_segment_id != segment_id else [],
                    "group": group.get("label", ""),
                    "manual_order": len(rows) + 1,
                    "scene_role": _scene_role(seg),
                    "story_position": group.get("activity", ""),
                    "include": bool(seg.get("include", True)),
                    "audio_role": "lower_original",
                    "speed": 1.0,
                    "user_notes": "",
                    "group_order": int(group.get("order", 999)),
                    **review,
                    "segment_id": segment_id,
                }
            )
    ordered = sorted(rows, key=lambda row: (int(row.get("manual_order") or 999999), int(row.get("group_order") or 999), row.get("clip_id", ""), float(row.get("start_seconds") or 0)))
    if apply_storyboard:
        from .storyboard import apply_storyboard_state, load_storyboard

        state = load_storyboard(cfg, project_id)
        if state is not None:
            return apply_storyboard_state(ordered, _state_with_identity_aliases(state, ordered))
    return ordered


def _stage(stage_id: str, label: str, done: bool, artifacts: list[Path]) -> dict:
    return {"id": stage_id, "label": label, "status": "done" if done else "pending", "artifacts": [str(path) for path in artifacts]}


def save_segment_review(cfg: dict, db: Path, project_id: int, rows: list[dict], *, base_revision: int | None = None) -> Path:
    with project_commit(db, project_id, base_revision) as commit:
        before = _segment_review(cfg, project_id)
        path = _save_segment_review(cfg, db, project_id, rows, mark_review=False)
        after = _segment_review(cfg, project_id)
        changed = before != after
        commit.record_changed(changed)
        if changed:
            mark_project_needs_review(cfg, db, project_id)
        return path


def _save_segment_review(cfg: dict, db: Path, project_id: int, rows: list[dict], *, mark_review: bool = True) -> Path:
    allowed = {"segment_id", "include", "user_notes", "manual_order", "scene_role", "story_position", "audio_role", "speed", "start_seconds", "end_seconds"}
    current = {str(row.get("segment_id")): row for row in project_segments(cfg, project_id, _read_json(project_dir(cfg, project_id) / "project_plan.json"), apply_storyboard=False, db=db)}
    videos = {int(row["id"]): dict(row) for row in project_videos(db, project_id)}
    data = []
    for index, row in enumerate(rows, 1):
        segment_id = str(row.get("segment_id") or "")
        if not segment_id:
            continue
        source = current.get(segment_id)
        if source is None:
            raise ValueError(f"找不到片段：{segment_id}")
        cleaned = _clean_segment_review({**row, "manual_order": index}, allowed)
        start = float(cleaned.get("start_seconds", source.get("start_seconds") or 0))
        end = float(cleaned.get("end_seconds", source.get("end_seconds") or 0))
        speed = float(cleaned.get("speed", source.get("speed") or 1.0))
        source_duration = float((videos.get(int(source.get("video_id") or 0)) or {}).get("duration_seconds") or 0)
        if start < 0 or end <= start:
            raise ValueError(f"片段 {segment_id} 的時間範圍無效")
        if source_duration > 0 and end > source_duration + 0.001:
            raise ValueError(f"片段 {segment_id} 的結束時間超過來源長度")
        if not 0.25 <= speed <= 4.0:
            raise ValueError(f"片段 {segment_id} 的速度必須介於 0.25 到 4.0")
        data.append(cleaned)
    path = project_dir(cfg, project_id) / "feedback" / "segment_review.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    append_decision(cfg, project_id, "segment_review", f"更新 {len(data)} 段片段審核", "segment_review", affected_segments=[row.get("segment_id", "") for row in data])
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def update_segment_timing(
    cfg: dict,
    db: Path,
    project_id: int,
    segment_id: str,
    start_seconds: float,
    end_seconds: float,
    speed: float,
    *,
    base_revision: int | None = None,
) -> Path:
    """Patch only one segment's timing without copying storyboard metadata."""
    with project_commit(db, project_id, base_revision) as commit:
        before = _segment_review(cfg, project_id)
        path = _update_segment_timing(cfg, db, project_id, segment_id, start_seconds, end_seconds, speed, mark_review=False)
        after = _segment_review(cfg, project_id)
        changed = before != after
        commit.record_changed(changed)
        if changed:
            mark_project_needs_review(cfg, db, project_id)
        return path


def update_segment_evidence(
    cfg: dict,
    db: Path,
    project_id: int,
    segment_id: str,
    patch: dict,
    *,
    base_revision: int | None = None,
) -> Path:
    """Patch human evidence corrections for one stable segment only."""

    editable = {
        "action",
        "shot_role",
        "technical_quality_issues",
        "duplicate_group",
        "natural_audio_recommendation",
        "include",
        "user_notes",
        "locked",
        "needs_review",
    }
    if not isinstance(patch, dict):
        raise ValueError("evidence patch 必須是物件")
    unknown = sorted(set(patch) - editable)
    if unknown:
        raise ValueError(f"不可修改的 evidence 欄位：{', '.join(unknown)}")
    segment_id = str(segment_id or "")
    if not segment_id:
        raise ValueError("segment_id 不可為空")

    with project_commit(db, project_id, base_revision) as commit:
        plan = _read_json(project_dir(cfg, project_id) / "project_plan.json")
        current_rows = project_segments(cfg, project_id, plan, apply_storyboard=False, db=db)
        source = next((row for row in current_rows if str(row.get("segment_id") or "") == segment_id), None)
        if source is None:
            raise ValueError(f"找不到片段：{segment_id}")
        existing = _segment_review(cfg, project_id)
        target_index = next((index for index, row in enumerate(existing) if str(row.get("segment_id") or "") == segment_id), None)
        created_target = target_index is None
        if target_index is None:
            target_index = len(existing)
            target = {"segment_id": segment_id}
            existing.append(target)
        else:
            target = dict(existing[target_index])
            existing[target_index] = target
        before = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        changed_fields = []
        for key in sorted(editable):
            if key not in patch:
                continue
            value = patch[key]
            if key in {"include", "locked", "needs_review"}:
                value = bool(value)
            elif key == "technical_quality_issues":
                if not isinstance(value, list):
                    raise ValueError("technical_quality_issues 必須是陣列")
                value = [str(item) for item in value]
            else:
                value = str(value or "").strip()
            if target.get(key) != value:
                target[key] = value
                changed_fields.append(key)
        if changed_fields:
            previous_fields = target.get("manual_override_fields") or []
            if not isinstance(previous_fields, list):
                previous_fields = []
            target["manual_override_fields"] = sorted({str(item) for item in previous_fields} | set(changed_fields))
            target["manual_override_source"] = "human"
            target["manual_override_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            target["manual_override_base_revision"] = base_revision
        after = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        changed = before != after
        if created_target and not changed:
            existing.pop()
        commit.record_changed(changed)
        path = project_dir(cfg, project_id) / "feedback" / "segment_review.json"
        _atomic_write_text(path, json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
        if changed:
            append_decision(
                cfg,
                project_id,
                "segment_evidence",
                f"人工修正片段 {segment_id} 的感知證據",
                "human",
                affected_segments=[segment_id],
            )
            mark_project_needs_review(cfg, db, project_id)
        return path


def _update_segment_timing(
    cfg: dict,
    db: Path,
    project_id: int,
    segment_id: str,
    start_seconds: float,
    end_seconds: float,
    speed: float,
    *,
    mark_review: bool = True,
) -> Path:
    """Patch only one segment's timing without copying storyboard metadata."""
    segment_id = str(segment_id or "")
    plan = _read_json(project_dir(cfg, project_id) / "project_plan.json")
    raw_rows = project_segments(cfg, project_id, plan, apply_storyboard=False, db=db)
    source = next((row for row in raw_rows if str(row.get("segment_id")) == segment_id), None)
    if source is None:
        raise ValueError(f"找不到片段：{segment_id}")
    start = float(start_seconds)
    end = float(end_seconds)
    rate = float(speed)
    if not all(math.isfinite(value) for value in (start, end, rate)):
        raise ValueError("片段時間與速度必須是有限數值")
    if start < 0 or end <= start:
        raise ValueError(f"片段 {segment_id} 的時間範圍無效")
    if not 0.25 <= rate <= 4.0:
        raise ValueError(f"片段 {segment_id} 的速度必須介於 0.25 到 4.0")
    videos = {int(row["id"]): dict(row) for row in project_videos(db, project_id)}
    source_duration = float((videos.get(int(source.get("video_id") or 0)) or {}).get("duration_seconds") or 0)
    if source_duration > 0 and end > source_duration + 0.001:
        raise ValueError(f"片段 {segment_id} 的結束時間超過來源長度")

    existing = _segment_review(cfg, project_id)
    target_index = next((index for index, row in enumerate(existing) if str(row.get("segment_id")) == segment_id), None)
    if target_index is None:
        target = {"segment_id": segment_id}
        existing.append(target)
    else:
        target = existing[target_index]
    target["start_seconds"] = round(start, 3)
    target["end_seconds"] = round(end, 3)
    target["speed"] = round(rate, 6)
    path = project_dir(cfg, project_id) / "feedback" / "segment_review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    append_decision(cfg, project_id, "segment_timing", f"更新片段 {segment_id} 時間與速度", "segment_review", affected_segments=[segment_id])
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def _clean_segment_review(row: dict, allowed: set[str]) -> dict:
    data = {key: row[key] for key in allowed if key in row}
    if "start_seconds" in data or "end_seconds" in data:
        start = max(0.0, float(data.get("start_seconds") or 0))
        end = max(start + 0.1, float(data.get("end_seconds") or start + 0.1))
        data["start_seconds"] = round(start, 3)
        data["end_seconds"] = round(end, 3)
    return data


def set_review_status(cfg: dict, db: Path, project_id: int, status: str, notes: str = "", *, base_revision: int | None = None) -> Path:
    if status == "approved":
        return _approve_project(cfg, db, project_id, notes, base_revision=base_revision)
    with project_commit(db, project_id, base_revision) as commit:
        folder = project_dir(cfg, project_id)
        before_review = _review_revision_payload(_read_json(folder / "review_status.json"))
        before_project_status = str((project(db, project_id)["status"] if project(db, project_id) else "") or "")
        before_plan_status = str(_read_json(folder / "project_plan.json").get("status") or "")
        path = _set_review_status(cfg, db, project_id, status, notes)
        after_review = _review_revision_payload(_read_json(folder / "review_status.json"))
        after_project_status = str((project(db, project_id)["status"] if project(db, project_id) else "") or "")
        after_plan_status = str(_read_json(folder / "project_plan.json").get("status") or "")
        commit.record_changed(before_review != after_review or before_project_status != after_project_status or before_plan_status != after_plan_status)
        return path


def _review_revision_payload(review: dict | None) -> dict:
    value = deepcopy(review or {})
    for key in ("updated_at", "approved_at"):
        value.pop(key, None)
    return value


def _set_review_status(cfg: dict, db: Path, project_id: int, status: str, notes: str = "") -> Path:
    if status == "approved":
        return _approve_project(cfg, db, project_id, notes)
    folder = project_dir(cfg, project_id)
    path = folder / "review_status.json"
    data = {"project_id": project_id, "status": status, "approved_by_user": False, "notes": notes, "updated_at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    save_revision_notes(cfg, project_id, notes)
    set_project_status(db, project_id, status)
    plan_path = folder / "project_plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["status"] = status
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    append_decision(cfg, project_id, f"review_{status}", f"使用者將專案標記為 {status}", "user_review", reason=notes)
    if status == "approved":
        write_checkpoint(cfg, project_id, "review_approved", "passed", plan_id=_read_json(plan_path).get("plan_id", ""), inputs=["review_status.json", "project_plan.json"])
    return path


def _approve_project(cfg: dict, db: Path, project_id: int, notes: str = "", *, base_revision: int | None = None) -> Path:
    """Prepare approval outside the lock, then publish it as one commit."""
    init_db(db)
    approval_base_revision = check_base_revision(db, project_id, base_revision)
    folder = project_dir(cfg, project_id)
    storyboard_path = folder / "storyboard.json"
    if not storyboard_path.exists():
        raise ValueError("缺少 storyboard.json，請先明確初始化分鏡後再核准")

    from .approval_snapshot import build_approval_snapshot, publish_approval_snapshot
    from .render_manifest import build_render_manifest

    manifest = build_render_manifest(cfg, db, project_id)
    approval_snapshot = build_approval_snapshot(
        cfg,
        db,
        project_id,
        approved_revision=approval_base_revision + 1,
        manifest=manifest,
    )
    plan_path = folder / "project_plan.json"
    plan = _read_json(plan_path)
    plan["status"] = "approved"
    snapshot_path = folder / "approvals" / f"{approval_snapshot['snapshot_id']}.json"
    now = datetime.now().isoformat(timespec="seconds")
    review = _read_json(folder / "review_status.json")
    review.update({
        "project_id": project_id,
        "status": "approved",
        "approved_by_user": True,
        "notes": notes,
        "updated_at": now,
        "approved_manifest_hash": manifest["manifest_hash"],
        "approved_plan_id": manifest.get("plan_id", ""),
        "approved_project_revision": approval_base_revision + 1,
        "approved_at": now,
        "approval_snapshot_id": approval_snapshot["snapshot_id"],
        "approval_snapshot_hash": approval_snapshot["snapshot_hash"],
        "approval_snapshot_schema_version": approval_snapshot["schema_version"],
        "approval_snapshot_path": str(snapshot_path.relative_to(folder)),
    })
    decision_path = folder / "decisions" / "decision_log.jsonl"
    decision = json.dumps({
        "created_at": now,
        "project_id": project_id,
        "plan_id": plan.get("plan_id", ""),
        "decision_type": "review_approved",
        "decision": "使用者將專案標記為 approved",
        "reason": notes,
        "source": "user_review",
        "confidence": 1.0,
        "affected_segments": [],
    }, ensure_ascii=False) + "\n"
    previous_decisions = decision_path.read_text(encoding="utf-8") if decision_path.is_file() else ""
    checkpoint_path = folder / "checkpoints" / "review_approved.json"
    checkpoint = {
        "checkpoint_id": "review_approved",
        "project_id": project_id,
        "plan_id": plan.get("plan_id", ""),
        "status": "passed",
        "created_at": now,
        "inputs": ["review_status.json", "project_plan.json"],
        "outputs": ["render_manifest.json", str(snapshot_path.relative_to(folder))],
        "warnings": [],
        "errors": [],
    }
    manifest_path = folder / "render_manifest.json"
    review_path = folder / "review_status.json"
    staged = {
        manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        review_path: json.dumps(review, ensure_ascii=False, indent=2),
        plan_path: json.dumps(plan, ensure_ascii=False, indent=2),
        decision_path: previous_decisions + decision,
        checkpoint_path: json.dumps(checkpoint, ensure_ascii=False, indent=2),
    }
    notes_text = (notes or "").strip()
    if notes_text:
        feedback = folder / "feedback"
        stamped = feedback / f"revision_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        staged[feedback / "revision_notes.md"] = notes_text
        staged[stamped] = notes_text

    stage_dir = folder / f".approval-stage-{uuid.uuid4().hex}"
    stage_paths: dict[Path, Path] = {}
    for target, text in staged.items():
        relative = target.relative_to(folder)
        stage_path = stage_dir / relative
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text(text, encoding="utf-8")
        stage_paths[target] = stage_path

    tracked = set(staged) | {snapshot_path}
    old_files = {
        path: (path.read_bytes(), path.stat())
        for path in tracked
        if path.is_file()
    }
    old_row = project(db, project_id)
    old_status = str(old_row["status"] or "") if old_row else ""
    old_revision = approval_base_revision
    published = False
    try:
        with project_commit(db, project_id, approval_base_revision) as commit:
            try:
                publish_approval_snapshot(cfg, approval_snapshot)
                published = True
                for target, stage_path in stage_paths.items():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage_path, target)
                set_project_status(db, project_id, "approved")
                commit.record_changed(True)
            except Exception:
                _restore_approval_files(tracked, old_files)
                _restore_approval_project_row(db, project_id, old_status, old_revision)
                raise
    except Exception:
        if published:
            _restore_approval_files(tracked, old_files)
            _restore_approval_project_row(db, project_id, old_status, old_revision)
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return review_path


def _restore_approval_files(tracked: set[Path], old_files: dict[Path, tuple[bytes, os.stat_result]]) -> None:
    for path in tracked:
        if path in old_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            content, stat_result = old_files[path]
            path.write_bytes(content)
            os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns))
        else:
            path.unlink(missing_ok=True)


def _restore_approval_project_row(db: Path, project_id: int, status: str, revision: int) -> None:
    with connect(db) as con:
        con.execute(
            "update projects set status=?, project_revision=?, updated_at=current_timestamp where id=?",
            (status, revision, project_id),
        )


def save_revision_notes(cfg: dict, project_id: int, notes: str) -> Path | None:
    text = (notes or "").strip()
    if not text:
        return None
    folder = project_dir(cfg, project_id) / "feedback"
    latest = folder / "revision_notes.md"
    stamped = folder / f"revision_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    latest.write_text(text, encoding="utf-8")
    stamped.write_text(text, encoding="utf-8")
    return latest


def mark_project_needs_review(cfg: dict, db: Path, project_id: int, *, base_revision: int | None = None) -> None:
    with project_commit(db, project_id, base_revision) as commit:
        folder = project_dir(cfg, project_id)
        before = (
            str((project(db, project_id)["status"] if project(db, project_id) else "") or ""),
            _review_revision_payload(_read_json(folder / "review_status.json")),
            str(_read_json(folder / "project_plan.json").get("status") or ""),
        )
        _mark_project_needs_review(cfg, db, project_id)
        after = (
            str((project(db, project_id)["status"] if project(db, project_id) else "") or ""),
            _review_revision_payload(_read_json(folder / "review_status.json")),
            str(_read_json(folder / "project_plan.json").get("status") or ""),
        )
        commit.record_changed(before != after)


def _mark_project_needs_review(cfg: dict, db: Path, project_id: int) -> None:
    folder = project_dir(cfg, project_id)
    set_project_status(db, project_id, "needs_review")
    review_path = folder / "review_status.json"
    if review_path.exists():
        review = _read_json(review_path)
        review.update({"status": "needs_review", "approved_by_user": False, "updated_at": datetime.now().isoformat(timespec="seconds")})
        for key in (
            "approved_manifest_hash", "approved_plan_id", "approved_project_revision", "approved_at",
            "approval_snapshot_id", "approval_snapshot_hash", "approval_snapshot_schema_version", "approval_snapshot_path", "story_generation_uuid",
        ):
            review.pop(key, None)
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    plan_path = folder / "project_plan.json"
    if plan_path.exists():
        plan = _read_json(plan_path)
        plan["status"] = "needs_review"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def can_project_render(cfg: dict, db: Path, project_id: int) -> tuple[bool, str]:
    init_db(db)
    row = project(db, project_id)
    if not row:
        return False, f"找不到專案 #{project_id}"
    if row["status"] != "approved":
        return False, f"專案狀態是 {row['status']}，尚未核准"
    clips = sync_project_files(cfg, db, project_id)
    if clips and not all(clip.get("analysis_current") for clip in clips):
        return False, "目前感知 generation 尚未成功發布"
    folder = project_dir(cfg, project_id)
    review_path = folder / "review_status.json"
    if not review_path.exists():
        return False, "缺少 review_status.json"
    review = _read_json(review_path)
    if review.get("approved_by_user") is not True:
        return False, "review_status.json 尚未由使用者核准"
    plan_path = folder / "project_plan.json"
    if not plan_path.exists():
        return False, "缺少 project_plan.json"
    plan = _read_json(plan_path)
    if plan.get("status") != "approved":
        return False, f"project_plan.json 狀態是 {plan.get('status', 'unknown')}，不是 approved"
    storyboard_file = folder / "storyboard.json"
    if not storyboard_file.exists():
        return False, "缺少 storyboard.json"
    try:
        from .storyboard import load_storyboard, validate_storyboard

        storyboard = load_storyboard(cfg, project_id)
        storyboard_validation = validate_storyboard(storyboard or {}, project_segments(cfg, project_id, plan, apply_storyboard=False, db=db))
        if not storyboard_validation["valid"]:
            return False, "Storyboard 無效：" + "; ".join(storyboard_validation["errors"])
    except Exception as exc:
        return False, f"Storyboard 無法建立：{exc}"
    approved_hash = review.get("approved_manifest_hash")
    if not approved_hash:
        return False, "review_status.json 缺少 approved_manifest_hash"
    approved_revision = review.get("approved_project_revision")
    if approved_revision not in (None, "") and int(approved_revision) != current_revision(db, project_id):
        return False, "approved_project_revision 已失效"
    snapshot_id = str(review.get("approval_snapshot_id") or "")
    snapshot_hash = str(review.get("approval_snapshot_hash") or "")
    snapshot_relative_path = str(review.get("approval_snapshot_path") or "")
    if not snapshot_id or not snapshot_hash or not snapshot_relative_path:
        return False, "核准資料為舊版，請重新核准以建立不可變 approval snapshot"
    manifest_path = folder / "render_manifest.json"
    if not manifest_path.exists():
        return False, "缺少 render_manifest.json"
    try:
        from .render_manifest import build_render_manifest, manifest_hash, validate_render_manifest
        from .approval_snapshot import load_approval_snapshot, validate_snapshot

        current_manifest = build_render_manifest(cfg, db, project_id)
        if current_manifest["manifest_hash"] != approved_hash:
            return False, "approved_manifest_hash 已失效"
        snapshot = _read_json(manifest_path)
        if snapshot.get("manifest_hash") != approved_hash or manifest_hash(snapshot) != approved_hash:
            return False, "render_manifest 核准快照已失效"
        snapshot_validation = validate_render_manifest(snapshot)
        if not snapshot_validation["valid"]:
            return False, "render_manifest 核准快照無效：" + "; ".join(snapshot_validation["errors"])
        approval_path = (folder / snapshot_relative_path).resolve()
        approvals_dir = (folder / "approvals").resolve()
        if approvals_dir not in approval_path.parents:
            return False, "approval snapshot 路徑無效"
        approval_snapshot = load_approval_snapshot(approval_path)
        if approval_snapshot.get("snapshot_id") != snapshot_id or approval_snapshot.get("snapshot_hash") != snapshot_hash:
            return False, "approval snapshot 指標不一致"
        immutable_validation = validate_snapshot(approval_snapshot, check_assets=True)
        if not immutable_validation["valid"]:
            return False, "approval snapshot 已失效：" + "; ".join(immutable_validation["errors"])
        if approval_snapshot.get("manifest_hash") != approved_hash:
            return False, "approval snapshot manifest hash 不一致"
    except Exception as exc:
        return False, f"目前 Manifest 無法建立：{exc}"
    return True, "approved"


def assert_project_approved(cfg: dict, db: Path, project_id: int, action: str = "render") -> None:
    ok, reason = can_project_render(cfg, db, project_id)
    if not ok:
        raise PermissionError(f"{action} 被擋下：{reason}")
    report = pre_render_validation(cfg, db, project_id)
    if report["errors"]:
        raise PermissionError(f"{action} 被擋下：pre-render validation failed")


def pre_render_validation(cfg: dict, db: Path, project_id: int) -> dict:
    ok, reason = can_project_render(cfg, db, project_id)
    folder = project_dir(cfg, project_id)
    plan = _read_json(folder / "project_plan.json")
    errors = [] if ok else [reason]
    warnings = []
    for clip in sync_project_files(cfg, db, project_id):
        if not Path(clip["source_path"]).exists():
            errors.append(f"source missing: {clip['source_path']}")
    for seg in project_segments(cfg, project_id, plan, db=db):
        if float(seg.get("end_seconds") or 0) <= float(seg.get("start_seconds") or 0):
            errors.append(f"bad segment range: {seg.get('segment_id')}")
    for track in project_bgm_tracks(db, project_id):
        if not track["source_url"] or not track["license_name"] or not track["attribution_text"]:
            warnings.append(f"BGM license incomplete: {track['title']}")
        if str(track["attribution_status"] or "unknown") == "unknown":
            warnings.append(f"BGM license attribution unresolved: {track['title']}")
        if str(track["license_status"] or "unverified") != "verified":
            warnings.append(f"BGM license not verified: {track['title']}")
    report = {"project_id": project_id, "plan_id": plan.get("plan_id", ""), "status": "failed" if errors else "passed", "created_at": datetime.now().isoformat(timespec="seconds"), "errors": errors, "warnings": warnings}
    out = folder / "validation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "pre_render_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "pre_render_report.md").write_text(_validation_md(report), encoding="utf-8")
    if not errors:
        write_checkpoint(cfg, project_id, "pre_render_validation_passed", "passed", plan_id=plan.get("plan_id", ""), outputs=["validation/pre_render_report.json"])
    return report


def append_decision(cfg: dict, project_id: int, decision_type: str, decision: str, source: str, reason: str = "", plan_id: str = "", confidence: float = 1.0, affected_segments: list[str] | None = None) -> Path:
    path = project_dir(cfg, project_id) / "decisions" / "decision_log.jsonl"
    row = {"created_at": datetime.now().isoformat(timespec="seconds"), "project_id": project_id, "plan_id": plan_id, "decision_type": decision_type, "decision": decision, "reason": reason, "source": source, "confidence": confidence, "affected_segments": affected_segments or []}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def write_checkpoint(cfg: dict, project_id: int, checkpoint_id: str, status: str, plan_id: str = "", inputs: list[str] | None = None, outputs: list[str] | None = None, warnings: list[str] | None = None, errors: list[str] | None = None) -> Path:
    path = project_dir(cfg, project_id) / "checkpoints" / f"{checkpoint_id}.json"
    data = {"checkpoint_id": checkpoint_id, "project_id": project_id, "plan_id": plan_id, "status": status, "created_at": datetime.now().isoformat(timespec="seconds"), "inputs": inputs or [], "outputs": outputs or [], "warnings": warnings or [], "errors": errors or []}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def pipeline_for_project(project_info: dict) -> dict:
    candidates = _pipeline_defs()
    category = str(project_info.get("category", "")).lower()
    content_type = str(project_info.get("content_type", "")).lower()
    for pipe in candidates:
        if pipe.get("pipeline_id", "").startswith(category):
            return pipe
    return next((pipe for pipe in candidates if content_type in pipe.get("content_types", [])), candidates[0] if candidates else {})


def write_project_files(cfg: dict, plan: dict) -> tuple[Path, Path]:
    folder = project_dir(cfg, int(plan["project_id"]))
    plans_dir = folder / "plans"
    version = _next_plan_version(plans_dir, plan["content_type"])
    plan_id = f"{plan['content_type']}_v{version:03d}"
    plan["plan_id"] = plan_id
    plan["version"] = version
    plan["parent_plan_id"] = _latest_plan_id(plans_dir)
    plan["created_reason"] = "revision_notes" if plan.get("revision_notes") else "build_project_plan"
    plan_path = folder / "project_plan.json"
    script_path = folder / "project_script.md"
    version_path = plans_dir / f"{plan_id}.json"
    version_script_path = plans_dir / f"{plan_id}.md"
    latest_path = plans_dir / "latest.json"
    script = project_script(plan)
    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    _atomic_write_text(plan_path, payload)
    _atomic_write_text(script_path, script)
    _atomic_write_text(version_path, payload)
    _atomic_write_text(version_script_path, script)
    _atomic_write_text(latest_path, json.dumps({"plan_id": plan_id, "path": str(version_path), "script_path": str(version_script_path)}, ensure_ascii=False, indent=2))
    return plan_path, script_path


def project_script(plan: dict) -> str:
    segments_list = [seg for group in plan["groups"] for seg in group["segments"]]
    lines = [
        f"# {plan['name']}",
        "",
        "## 剪輯方向",
        f"- 這個專案有 {len(plan['clips'])} 支素材，目前找到 {len(segments_list)} 段可用片段。",
        f"- 建議先做成「{_content_type_label(plan['content_type'])}」，用時間順序當主線，再穿插特寫與氣氛鏡頭。",
        f"- 先不要急著全剪進去，優先挑每組分數最高的片段做第一版。",
        _bgm_line(plan.get("bgm_recommendations", []) or plan.get("bgm", [])),
    ]
    if plan.get("revision_notes"):
        lines += ["", "## 審核備註", plan["revision_notes"]]
        for item in plan.get("feedback_unresolved", []):
            lines.append(f"- 尚未自動處理：{item}")
    if plan.get("story_context_usage"):
        lines += ["", "## 使用者故事脈絡"]
        for item in plan["story_context_usage"]:
            avoided = "、".join(item.get("avoided_activities", []))
            suffix = f"；排除：{avoided}" if avoided else ""
            lines.append(f"- {item['clip_id']}｜{item['user_summary']}｜分組：{item['activity']}（來源：user_summary）{suffix}")
    lines += ["", "## 自動字卡"]
    for card in plan.get("title_cards", []):
        lines.append(f"- {card['where']}｜{card['text']}｜{card['style']}")
    lines += [
        "",
        "## 建議剪輯順序",
    ]
    for group in plan["groups"]:
        if not group["segments"]:
            continue
        lines.append(f"- {group['label']}：先放 {_group_role(group['activity'])}，可用片段 {len(group['segments'])} 段。")
    if not any(group["segments"] for group in plan["groups"]):
        lines.append("- 尚未找到可用片段，請先重跑內容感知或降低推薦門檻。")

    lines += ["", "## 推薦片段"]
    for group in plan["groups"]:
        lines += ["", f"### {group['label']}"]
        if not group["segments"]:
            lines.append(f"- {_empty_group_note(group)}")
        for seg in group["segments"][:6]:
            lines.append(f"- {seg['clip_id']} {_time(seg['start_seconds'])}-{_time(seg['end_seconds'])}｜{_use_label(seg['suggested_use'])}｜分數 {seg['score']}")
        if len(group["segments"]) > 6:
            lines.append(f"- 另外還有 {len(group['segments']) - 6} 段，先不用全放。")
    lines += ["", "## 下一步", "- 先看調色預覽，挑出畫面舒服的片段。", "- 如果分組方向可以，按 OpenCut 匯出，再進 OpenCut 依照上面的順序拼第一版。"]
    return "\n".join(lines)


def _content_type_label(value: str) -> str:
    return {"travel_diary": "旅行日記", "diary_montage": "日常紀錄", "process_montage": "過程剪輯", "highlight": "精華短片"}.get(value, value)


def _bgm_line(bgm_items: list[dict]) -> str:
    if not bgm_items:
        return "- BGM：目前資料庫沒有可套用的音樂，先略過。"
    if "track" not in bgm_items[0]:
        track = bgm_items[0]
        credit = track.get("attribution_text") or track.get("source_url") or "請確認授權資訊"
        return f"- BGM：已套用「{track.get('title', '')}」；YouTube 說明欄署名：{credit}"
    names = "、".join(f"{item['group']}→{item['track'].get('title', '')}" for item in bgm_items[:4])
    return f"- BGM：依內容分組推薦：{names}。"


def _title_cards(project_info: dict, groups: list[dict]) -> list[dict]:
    cards = []
    last_text = ""
    for group in groups:
        if not group.get("segments"):
            continue
        text = _card_text(group)
        if text == last_text:
            continue
        cards.append({"where": f"{group['label']} 第一段前", "text": text, "style": "地點/場景字卡，左下角，1.5 秒"})
        last_text = text
    return cards


def _card_text(group: dict) -> str:
    if group["activity"] == "飲食":
        return f"{group['time_of_day']}｜用餐/咖啡"
    if group["activity"] == "風景":
        return f"{group['time_of_day']}｜路上風景"
    if group["activity"] == "逛街":
        return f"{group['time_of_day']}｜街上散步"
    return group["label"].replace(" / ", " ")


def _group_role(activity: str) -> str:
    return {"飲食": "主體動作", "風景": "場景交代和轉場", "逛街": "移動過程", "特寫": "節奏點或封面候選", "其他": "補畫面"}.get(activity, activity)


def _use_label(value: str) -> str:
    return {
        "B-roll": "補畫面",
        "Shorts": "可當亮點/短影音",
        "Product closeup": "特寫",
        "補畫面": "補畫面",
        "短影音": "可當亮點/短影音",
        "產品特寫": "特寫",
    }.get(value, value)


def _scene_role(seg: dict) -> str:
    tags = {str(tag).lower() for tag in seg.get("tags", [])}
    title = str(seg.get("title", "")).lower()
    if "closeup" in tags or "close" in title:
        return "detail"
    if tags & {"street", "landscape", "travel"}:
        return "establishing_shot"
    if tags & {"hands", "coffee", "matcha", "food", "dripping", "steam"}:
        return "main_action"
    return "transition"


def _empty_group_note(group: dict) -> str:
    if group["activity"] == "已感知但無推薦片段":
        return "已分析，但畫面分數不夠高，先不要放進第一版。"
    return "尚未有可用片段，請先做內容感知。"


def project_dir(cfg: dict, project_id: int) -> Path:
    path = Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}"
    for name in ("source", "clips", "plans", "output", "feedback", "decisions", "checkpoints", "validation"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _group(label: str, time_of_day: str, activity: str, order: int = 999) -> dict:
    return {"label": label, "time_of_day": time_of_day, "activity": activity, "order": order, "clips": [], "segments": [], "story_context": []}


def _story_context_usage(clip: dict, context: dict) -> dict:
    return {
        "clip_id": clip["clip_id"],
        "project_media_id": clip["project_media_id"],
        "video_id": clip["video_id"],
        "filename": clip["filename"],
        "user_summary": context["user_summary"],
        "ai_visual_summary": context["ai_visual_summary"],
        "effective_summary": context["effective_summary"],
        "effective_summary_source": context["effective_summary_source"],
        "activity": context["activity"],
        "activity_source": context["activity_source"],
        "avoided_activities": context["avoided_activities"],
        "guidance_applied": context["guidance_applied"],
    }


def _dedupe_story_context(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        key = item.get("project_media_id") or item.get("clip_id")
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _chapter_for(chapters: list[dict], clip_id: str) -> dict | None:
    return next((chapter for chapter in chapters if clip_id in chapter.get("clip_ids", [])), None)


def project_info_is_travel(row) -> bool:
    return dict(row).get("content_type") == "travel_diary"


def _clip_summary(clip: dict) -> dict:
    return {k: clip[k] for k in (
        "clip_id", "project_media_id", "video_id", "filename", "source_path", "order",
        "duration_seconds", "detected_category", "time_of_day", "status", "segment_count",
        "visual_summary", "ai_visual_summary", "user_summary", "user_summary_updated_at",
        "user_summary_migration_state", "effective_summary", "effective_summary_source",
        "analysis_current", "perception_run",
    )}


def _project_segment_identity_rows(db: Path, project_id: int) -> dict[int, list[dict]]:
    by_video: dict[int, list[dict]] = {}
    for video in project_videos(db, project_id):
        video_id = int(video["id"])
        by_video[video_id] = [dict(row) for row in segments(db, video_id)]
    return by_video


def _resolve_plan_segment_identity(seg: dict, identity_rows: dict[int, list[dict]]) -> str:
    try:
        video_id = int(seg.get("video_id") or 0)
    except (TypeError, ValueError):
        return ""
    candidates = identity_rows.get(video_id, [])
    if not candidates:
        return ""
    ranked = sorted(
        ((_plan_segment_match_score(seg, candidate), candidate) for candidate in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.45:
        return ""
    return str(ranked[0][1].get("segment_uuid") or "")


def _plan_segment_match_score(left: dict, right: dict) -> float:
    left_start = float(left.get("start_seconds") or 0)
    left_end = float(left.get("end_seconds") or left_start)
    right_start = float(right.get("start_seconds") or 0)
    right_end = float(right.get("end_seconds") or right_start)
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    union = max(0.001, max(left_end, right_end) - min(left_start, right_start))
    iou = overlap / union
    left_duration = max(0.001, left_end - left_start)
    right_duration = max(0.001, right_end - right_start)
    midpoint_scale = max(1.0, left_duration, right_duration)
    midpoint_similarity = max(0.0, 1.0 - abs((left_start + left_end) / 2 - (right_start + right_end) / 2) / midpoint_scale)
    return 0.8 * iou + 0.2 * midpoint_similarity


def _legacy_segment_id(seg: dict) -> str:
    return f"{seg.get('clip_id', 'clip')}_{int(float(seg.get('start_seconds') or 0) * 1000):08d}"


def _state_with_identity_aliases(state: dict, rows: list[dict]) -> dict:
    result = deepcopy(state)
    configured = result.get("segments")
    if not isinstance(configured, dict):
        return result
    for row in rows:
        stable_id = str(row.get("segment_id") or "")
        legacy_id = str(row.get("legacy_segment_id") or "")
        if stable_id and legacy_id and stable_id not in configured and legacy_id in configured:
            configured[stable_id] = deepcopy(configured[legacy_id])
    return result


def _visual_summary(db: Path, video_id: int) -> str:
    summaries = []
    for frame in frames(db, video_id):
        text = str(frame["vision_summary"] or "").strip()
        if text and text not in summaries:
            summaries.append(text)
        if len(summaries) >= 3:
            break
    return " / ".join(summaries)


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in items:
        identity = item.get("project_media_id") or item["clip_id"]
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _time_label(video: dict) -> str:
    match = re.match(r"\d{8}_(\d{2})\d{4}_", video.get("filename", ""))
    hour = int(match.group(1)) if match else None
    if hour is None:
        return "未定時間"
    if 5 <= hour <= 10:
        return "上午"
    if 11 <= hour <= 14:
        return "中午"
    if 15 <= hour <= 17:
        return "下午"
    if 18 <= hour <= 23:
        return "晚上"
    return "深夜"


def _time_rank(label: str) -> int:
    return {"深夜": 0, "上午": 1, "中午": 2, "下午": 3, "晚上": 4, "未定時間": 9}.get(label, 9)


def _activity(tags: str, category: str) -> str:
    words = {word.strip().lower() for word in f"{tags},{category}".split(",") if word.strip()}
    if words & {"coffee", "matcha", "food", "dripping", "steam", "hands"}:
        return "飲食"
    if words & {"landscape", "travel", "nature", "view", "beach", "mountain"}:
        return "風景"
    if words & {"street", "city", "walking", "shop", "shopping"}:
        return "逛街"
    if words & {"closeup"}:
        return "特寫"
    return "其他"


def _time(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeError):
            pass
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _validation_md(report: dict) -> str:
    lines = [f"# Pre-render validation: {report['status']}", "", f"- project_id: {report['project_id']}", f"- plan_id: {report.get('plan_id', '')}", "", "## Errors"]
    lines += [f"- {item}" for item in report["errors"]] or ["- none"]
    lines += ["", "## Warnings"]
    lines += [f"- {item}" for item in report["warnings"]] or ["- none"]
    return "\n".join(lines)


def _pipeline_defs() -> list[dict]:
    root = Path(__file__).resolve().parents[2] / "pipeline_defs"
    return [_parse_pipeline(path) for path in sorted(root.glob("*.yaml"))]


def _parse_pipeline(path: Path) -> dict:
    data: dict[str, object] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            key, _, value = line.partition(":")
            current = key.strip()
            data[current] = value.strip() or []
        elif line.strip().startswith("-") and current:
            data.setdefault(current, [])
            if isinstance(data[current], list):
                data[current].append(line.strip()[1:].strip())
    return data


def _revision_notes(cfg: dict, project_id: int) -> str:
    path = project_dir(cfg, project_id) / "feedback" / "revision_notes.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _segment_review(cfg: dict, project_id: int) -> list[dict]:
    folder = project_dir(cfg, project_id)
    path = folder / "feedback" / "segment_review.json"
    if not path.exists():
        path = folder / "segment_review.json"
    data = _read_json(path)
    return data if isinstance(data, list) else []


def _next_plan_version(plans_dir: Path, content_type: str) -> int:
    prefix = f"{content_type}_v"
    versions = []
    for path in plans_dir.glob(f"{prefix}[0-9][0-9][0-9].json"):
        try:
            versions.append(int(path.stem.removeprefix(prefix)))
        except ValueError:
            pass
    return max(versions, default=0) + 1


def _latest_plan_id(plans_dir: Path) -> str:
    latest = _read_json(plans_dir / "latest.json")
    return str(latest.get("plan_id") or "")
