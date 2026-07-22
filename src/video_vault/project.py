from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from pathlib import Path
import json
import math
import re
import shutil

from .bgm import recommend_bgm_for_groups
from .database import create_project_row, frames, init_db, project, project_bgm_tracks, project_videos, projects, segments, set_project_status, set_project_videos


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
    rows = [dict(v) for v in project_videos(db, project_id)]
    source_dir = project_dir(cfg, project_id) / "source"
    clips_dir = project_dir(cfg, project_id) / "clips"
    result = []
    for order, video in enumerate(rows, 1):
        clip_id = f"clip_{order:03}"
        src = Path(video["current_path"])
        dst = src if src.parent.resolve() == source_dir.resolve() else source_dir / f"{clip_id}_{src.name}"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
        clip_dir = clips_dir / clip_id
        clip_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "clip_id": clip_id,
            "project_id": project_id,
            "video_id": video["id"],
            "filename": dst.name,
            "source_path": str(dst),
            "original_source_path": video["current_path"],
            "order": order,
            "included": True,
            "duration_seconds": video["duration_seconds"],
            "detected_category": video["category"],
            "time_of_day": _time_label(video),
            "status": video.get("status") or "uploaded",
            "segment_count": len(list(segments(db, int(video["id"])))),
            "visual_summary": _visual_summary(db, int(video["id"])),
        }
        (clip_dir / "clip.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result.append(data)
    return result


def build_project_plan(cfg: dict, db: Path, project_id: int) -> dict:
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
            activity = chapter["label"] if chapter else ("已感知但無推薦片段" if clip["status"] == "perceived" else "未分析")
            label = activity if chapter else f"{clip['time_of_day']} / {activity}"
            groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))["clips"].append(_clip_summary(clip))
            continue
        for seg in video_segments:
            chapter = _chapter_for(itinerary, clip["clip_id"]) if itinerary else None
            activity = chapter["label"] if chapter else _activity(seg.get("tags", ""), video.get("category", ""))
            label = activity if chapter else f"{clip['time_of_day']} / {activity}"
            group = groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))
            group["clips"].append(_clip_summary(clip))
            group["segments"].append(
                {
                    "clip_id": clip["clip_id"],
                    "video_id": video["id"],
                    "source_file": clip["source_path"],
                    "start_seconds": seg["start_seconds"],
                    "end_seconds": seg["end_seconds"],
                    "title": seg["title"],
                    "suggested_use": seg["suggested_use"],
                    "tags": [tag for tag in (seg.get("tags") or "").split(",") if tag],
                    "score": seg["score"],
                }
            )
    ordered = sorted(groups.values(), key=lambda g: (int(g.get("order", 999)), _time_rank(g["time_of_day"]), g["activity"], g["label"]))
    for group in ordered:
        group["clips"] = _dedupe(group["clips"])
        group["segments"].sort(key=lambda s: (s["clip_id"], float(s["start_seconds"] or 0)) if itinerary or project_info_is_travel(row) else (-float(s["score"] or 0), s["clip_id"], float(s["start_seconds"] or 0)))
    project_info = dict(row)
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
        "groups": ordered,
        "bgm": bgm,
        "bgm_recommendations": bgm_recommendations,
        "title_cards": _title_cards(project_info, ordered),
        "revision_notes": revision_notes,
        "feedback_applied": [],
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

    public_bgm = []
    for bgm_row in project_bgm_tracks(db, project_id):
        bgm_data = dict(bgm_row)
        public_bgm.append({
            key: bgm_data.get(key)
            for key in (
                "id", "title", "artist", "source_url", "license_name", "license_url",
                "attribution_required", "attribution_text", "mood", "duration_seconds",
            )
        })

    public_plan = _public_plan_bgm(plan)
    public_segments = []
    for segment in project_segments(cfg, project_id, plan):
        public_segment = dict(segment)
        source = public_segment.get("source_file", "")
        public_segment["source_filename"] = Path(str(source)).name if source else ""
        public_segments.append(public_segment)
    return {
        "project": dict(row),
        "clips": sync_project_files(cfg, db, project_id),
        "bgm": public_bgm,
        "plan": public_plan,
        "workflow": project_workflow(cfg, db, project_id, plan),
        "segments": public_segments,
        "review": _read_json(folder / "review_status.json"),
        "can_render": ok,
        "render_gate_reason": reason,
        "script": (folder / "project_script.md").read_text(encoding="utf-8") if (folder / "project_script.md").exists() else "",
        "folder": str(folder),
        "color": color_state_for_api(cfg, project_id, load_project_color_state(cfg, project_id)),
        "audio": audio_state_for_api(cfg, project_id, db),
        "storyboard": storyboard_for_api(cfg, db, project_id),
    }


def project_workflow(cfg: dict, db: Path, project_id: int, plan: dict | None = None) -> dict:
    folder = project_dir(cfg, project_id)
    clips = sync_project_files(cfg, db, project_id)
    plan = plan or _read_json(folder / "project_plan.json")
    review = _read_json(folder / "review_status.json")
    segments = project_segments(cfg, project_id, plan)
    outputs = folder / "output"
    stages = [
        _stage("import", "匯入素材", bool(clips), [folder / "source"]),
        _stage("perception", "內容感知", any(c.get("segment_count", 0) for c in clips), [folder / "clips"]),
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
        )
        if key in row
    }


def project_segments(cfg: dict, project_id: int, plan: dict, *, apply_storyboard: bool = True) -> list[dict]:
    reviews = {row.get("segment_id"): row for row in _segment_review(cfg, project_id)}
    rows = []
    for group in plan.get("groups", []):
        for order, seg in enumerate(group.get("segments", []), 1):
            segment_id = f"{seg.get('clip_id', 'clip')}_{int(float(seg.get('start_seconds') or 0) * 1000):08d}"
            rows.append(
                {
                    **seg,
                    "segment_id": segment_id,
                    "group": group.get("label", ""),
                    "manual_order": len(rows) + 1,
                    "scene_role": _scene_role(seg),
                    "story_position": group.get("activity", ""),
                    "include": True,
                    "audio_role": "lower_original",
                    "speed": 1.0,
                    "user_notes": "",
                    "group_order": int(group.get("order", 999)),
                    **reviews.get(segment_id, {}),
                }
            )
    ordered = sorted(rows, key=lambda row: (int(row.get("manual_order") or 999999), int(row.get("group_order") or 999), row.get("clip_id", ""), float(row.get("start_seconds") or 0)))
    if apply_storyboard:
        from .storyboard import apply_storyboard_state, load_storyboard

        state = load_storyboard(cfg, project_id)
        if state is not None:
            return apply_storyboard_state(ordered, state)
    return ordered


def _stage(stage_id: str, label: str, done: bool, artifacts: list[Path]) -> dict:
    return {"id": stage_id, "label": label, "status": "done" if done else "pending", "artifacts": [str(path) for path in artifacts]}


def save_segment_review(cfg: dict, db: Path, project_id: int, rows: list[dict]) -> Path:
    allowed = {"segment_id", "include", "user_notes", "manual_order", "scene_role", "story_position", "audio_role", "speed", "start_seconds", "end_seconds"}
    current = {str(row.get("segment_id")): row for row in project_segments(cfg, project_id, _read_json(project_dir(cfg, project_id) / "project_plan.json"), apply_storyboard=False)}
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
) -> Path:
    """Patch only one segment's timing without copying storyboard metadata."""
    segment_id = str(segment_id or "")
    plan = _read_json(project_dir(cfg, project_id) / "project_plan.json")
    raw_rows = project_segments(cfg, project_id, plan, apply_storyboard=False)
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


def set_review_status(cfg: dict, db: Path, project_id: int, status: str, notes: str = "") -> Path:
    folder = project_dir(cfg, project_id)
    path = folder / "review_status.json"
    snapshot = {}
    if status == "approved":
        from .storyboard import ensure_storyboard

        ensure_storyboard(cfg, db, project_id)
        from .render_manifest import compile_render_manifest

        manifest = compile_render_manifest(cfg, db, project_id)
        snapshot = {
            "approved_manifest_hash": manifest["manifest_hash"],
            "approved_plan_id": manifest.get("plan_id", ""),
            "approved_at": datetime.now().isoformat(timespec="seconds"),
        }
    data = {"project_id": project_id, "status": status, "approved_by_user": status == "approved", "notes": notes, "updated_at": datetime.now().isoformat(timespec="seconds"), **snapshot}
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


def mark_project_needs_review(cfg: dict, db: Path, project_id: int) -> None:
    folder = project_dir(cfg, project_id)
    set_project_status(db, project_id, "needs_review")
    review_path = folder / "review_status.json"
    if review_path.exists():
        review = _read_json(review_path)
        review.update({"status": "needs_review", "approved_by_user": False, "updated_at": datetime.now().isoformat(timespec="seconds")})
        for key in ("approved_manifest_hash", "approved_plan_id", "approved_at"):
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
        storyboard_validation = validate_storyboard(storyboard or {}, project_segments(cfg, project_id, plan, apply_storyboard=False))
        if not storyboard_validation["valid"]:
            return False, "Storyboard 無效：" + "; ".join(storyboard_validation["errors"])
    except Exception as exc:
        return False, f"Storyboard 無法建立：{exc}"
    approved_hash = review.get("approved_manifest_hash")
    if not approved_hash:
        return False, "review_status.json 缺少 approved_manifest_hash"
    manifest_path = folder / "render_manifest.json"
    if not manifest_path.exists():
        return False, "缺少 render_manifest.json"
    try:
        from .render_manifest import build_render_manifest, manifest_hash, validate_render_manifest

        current_manifest = build_render_manifest(cfg, db, project_id)
        if current_manifest["manifest_hash"] != approved_hash:
            return False, "approved_manifest_hash 已失效"
        snapshot = _read_json(manifest_path)
        if snapshot.get("manifest_hash") != approved_hash or manifest_hash(snapshot) != approved_hash:
            return False, "render_manifest 核准快照已失效"
        snapshot_validation = validate_render_manifest(snapshot)
        if not snapshot_validation["valid"]:
            return False, "render_manifest 核准快照無效：" + "; ".join(snapshot_validation["errors"])
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
    for seg in project_segments(cfg, project_id, plan):
        if float(seg.get("end_seconds") or 0) <= float(seg.get("start_seconds") or 0):
            errors.append(f"bad segment range: {seg.get('segment_id')}")
    for track in project_bgm_tracks(db, project_id):
        if not track["source_url"] or not track["license_name"] or not track["attribution_text"]:
            warnings.append(f"BGM license incomplete: {track['title']}")
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
    plan_path.write_text(payload, encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    version_path.write_text(payload, encoding="utf-8")
    version_script_path.write_text(script, encoding="utf-8")
    latest_path.write_text(json.dumps({"plan_id": plan_id, "path": str(version_path), "script_path": str(version_script_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan_path, script_path


def project_script(plan: dict) -> str:
    segments = [seg for group in plan["groups"] for seg in group["segments"]]
    lines = [
        f"# {plan['name']}",
        "",
        "## 剪輯方向",
        f"- 這個專案有 {len(plan['clips'])} 支素材，目前找到 {len(segments)} 段可用片段。",
        f"- 建議先做成「{_content_type_label(plan['content_type'])}」，用時間順序當主線，再穿插特寫與氣氛鏡頭。",
        f"- 先不要急著全剪進去，優先挑每組分數最高的片段做第一版。",
        _bgm_line(plan.get("bgm_recommendations", []) or plan.get("bgm", [])),
    ]
    if plan.get("revision_notes"):
        lines += ["", "## 審核備註", plan["revision_notes"]]
        for item in plan.get("feedback_unresolved", []):
            lines.append(f"- 尚未自動處理：{item}")
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
    return {"label": label, "time_of_day": time_of_day, "activity": activity, "order": order, "clips": [], "segments": []}


def _chapter_for(chapters: list[dict], clip_id: str) -> dict | None:
    return next((chapter for chapter in chapters if clip_id in chapter.get("clip_ids", [])), None)


def project_info_is_travel(row) -> bool:
    return dict(row).get("content_type") == "travel_diary"


def _clip_summary(clip: dict) -> dict:
    return {k: clip[k] for k in ("clip_id", "video_id", "filename", "source_path", "order", "duration_seconds", "detected_category", "time_of_day", "status", "segment_count", "visual_summary")}


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
        if item["clip_id"] not in seen:
            seen.add(item["clip_id"])
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
