from __future__ import annotations

from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise SystemExit(f"missing patch target: {label}")
    return text.replace(old, new, 1)


def replace_block(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"missing block target: {label}")
    return updated


def patch_database() -> None:
    path = Path("src/video_vault/database.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "  proxy_path text,\n  status text default 'new'\n",
        "  proxy_path text,\n  user_summary text default '',\n  user_summary_updated_at text,\n  status text default 'new'\n",
        "videos user summary schema",
    )
    text = replace_once(
        text,
        "  summary_override text,\n  analysis_status text,\n",
        "  summary_override text,\n  user_summary text default '',\n  user_summary_updated_at text,\n  summary_migration_state text default 'none',\n  analysis_status text,\n",
        "project media user summary schema",
    )
    text = replace_once(
        text,
        '''        _ensure_columns(
            con,
            "segments",
''',
        '''        _ensure_columns(
            con,
            "videos",
            {
                "user_summary": "text default ''",
                "user_summary_updated_at": "text",
            },
        )
        _ensure_columns(
            con,
            "segments",
''',
        "videos migration columns",
    )
    text = replace_once(
        text,
        '''                "summary_override": "text",
                "analysis_status": "text",
''',
        '''                "summary_override": "text",
                "user_summary": "text default ''",
                "user_summary_updated_at": "text",
                "summary_migration_state": "text default 'none'",
                "analysis_status": "text",
''',
        "project migration columns",
    )
    text = replace_once(
        text,
        "        _backfill_project_media_snapshots(con)\n",
        "        _backfill_project_media_snapshots(con)\n        _migrate_legacy_summary_ownership(con)\n",
        "legacy summary migration call",
    )
    text = replace_block(
        text,
        r"def _backfill_project_media_snapshots\(con: sqlite3.Connection\) -> None:.*?\n\ndef _legacy_segment_uuid",
        '''def _backfill_project_media_snapshots(con: sqlite3.Connection) -> None:
    con.execute(
        """update project_videos
        set display_name=coalesce(nullif(display_name, ''), (select filename from videos where id=project_videos.video_id)),
            category_override=coalesce(nullif(category_override, ''), (select category from videos where id=project_videos.video_id), 'unknown'),
            analysis_status=coalesce(nullif(analysis_status, ''), (select status from videos where id=project_videos.video_id), 'new'),
            user_summary=coalesce(user_summary, ''),
            summary_migration_state=coalesce(nullif(summary_migration_state, ''), 'none'),
            perception_revision=coalesce(perception_revision, 0)"""
    )


def _migrate_legacy_summary_ownership(con: sqlite3.Connection) -> None:
    """Conservatively recover user-authored text from legacy summary_override.

    #37 used summary_override both as an AI snapshot and as the editable field.
    We migrate only values that clearly differ from the first AI observation, or
    values saved before frames existed. Identical repeated observations remain
    flagged for review instead of being guessed as user-authored.
    """
    rows = con.execute(
        """select project_id, video_id, summary_override, user_summary,
                  coalesce(summary_migration_state, 'none') as migration_state
        from project_videos
        where coalesce(summary_override, '')<>''
          and coalesce(user_summary, '')=''
          and coalesce(summary_migration_state, 'none')='none'"""
    ).fetchall()
    for row in rows:
        legacy = str(row["summary_override"] or "").strip()
        frame_summaries = [
            str(frame["vision_summary"] or "").strip()
            for frame in con.execute(
                """select vision_summary from frames
                where video_id=? and coalesce(vision_summary, '')<>''
                order by timestamp_seconds, id""",
                (int(row["video_id"]),),
            ).fetchall()
        ]
        if not frame_summaries or legacy != frame_summaries[0]:
            con.execute(
                """update project_videos
                set user_summary=?, user_summary_updated_at=coalesce(user_summary_updated_at, current_timestamp),
                    summary_migration_state='migrated'
                where project_id=? and video_id=?""",
                (legacy, int(row["project_id"]), int(row["video_id"])),
            )
        elif len(set(frame_summaries)) == 1:
            con.execute(
                """update project_videos set summary_migration_state='review'
                where project_id=? and video_id=?""",
                (int(row["project_id"]), int(row["video_id"])),
            )
        else:
            con.execute(
                """update project_videos set summary_migration_state='legacy_ai_snapshot'
                where project_id=? and video_id=?""",
                (int(row["project_id"]), int(row["video_id"])),
            )


def _legacy_segment_uuid''',
        "legacy summary migration",
    )
    text = replace_block(
        text,
        r"def update_project_media_summary\(db: Path, project_id: int, video_id: int, summary: str\) -> bool:.*?\n\ndef update_video_summary",
        '''def update_project_media_summary(db: Path, project_id: int, video_id: int, summary: str) -> bool:
    init_db(db)
    with connect(db) as con:
        row = con.execute(
            "select 1 from project_videos where project_id=? and video_id=?",
            (int(project_id), int(video_id)),
        ).fetchone()
        if not row:
            return False
        con.execute(
            """update project_videos
            set user_summary=?, user_summary_updated_at=current_timestamp,
                summary_migration_state='native'
            where project_id=? and video_id=?""",
            (str(summary).strip(), int(project_id), int(video_id)),
        )
        con.execute("update projects set updated_at=current_timestamp where id=?", (int(project_id),))
        return True


def update_video_summary''',
        "project user summary update",
    )
    text = replace_block(
        text,
        r"def update_video_summary\(db: Path, video_id: int, summary: str, project_id: int \| None = None\) -> bool:.*?\n\ndef update_project_media_metadata",
        '''def update_video_summary(db: Path, video_id: int, summary: str, project_id: int | None = None) -> bool:
    """Save user-authored context without mutating AI frame perception."""
    init_db(db)
    if project_id is not None:
        return update_project_media_summary(db, int(project_id), int(video_id), summary)
    with connect(db) as con:
        owners = [
            int(row["project_id"])
            for row in con.execute(
                "select project_id from project_videos where video_id=? order by project_id",
                (int(video_id),),
            ).fetchall()
        ]
        if len(owners) > 1:
            return False
        if len(owners) == 1:
            con.execute(
                """update project_videos
                set user_summary=?, user_summary_updated_at=current_timestamp,
                    summary_migration_state='native'
                where project_id=? and video_id=?""",
                (str(summary).strip(), owners[0], int(video_id)),
            )
            con.execute("update projects set updated_at=current_timestamp where id=?", (owners[0],))
            return True
        exists = con.execute("select 1 from videos where id=?", (int(video_id),)).fetchone()
        if not exists:
            return False
        con.execute(
            "update videos set user_summary=?, user_summary_updated_at=current_timestamp where id=?",
            (str(summary).strip(), int(video_id)),
        )
        return True


def update_project_media_metadata''',
        "legacy user summary update",
    )
    text = replace_once(
        text,
        '''                """select filename, category, status,
                    coalesce((select vision_summary from frames where frames.video_id=videos.id order by timestamp_seconds, id limit 1), '') as summary
                from videos where id=?""",
''',
        '''                """select filename, category, status,
                    coalesce(user_summary, '') as user_summary,
                    user_summary_updated_at
                from videos where id=?""",
''',
        "project media source snapshot",
    )
    text = replace_once(
        text,
        '''                """insert into project_videos(
                    project_id, video_id, project_media_uuid, display_name, category_override,
                    summary_override, analysis_status, perception_revision, sort_order
                ) values(?, ?, ?, ?, ?, ?, ?, 0, ?)
''',
        '''                """insert into project_videos(
                    project_id, video_id, project_media_uuid, display_name, category_override,
                    summary_override, user_summary, user_summary_updated_at, summary_migration_state,
                    analysis_status, perception_revision, sort_order
                ) values(?, ?, ?, ?, ?, '', ?, ?, 'none', ?, 0, ?)
''',
        "project media insert columns",
    )
    text = replace_once(
        text,
        '''                    str(snapshot["category"] or "unknown"),
                    str(snapshot["summary"] or ""),
                    str(snapshot["status"] or "new"),
                    order,
''',
        '''                    str(snapshot["category"] or "unknown"),
                    str(snapshot["user_summary"] or ""),
                    snapshot["user_summary_updated_at"],
                    str(snapshot["status"] or "new"),
                    order,
''',
        "project media insert values",
    )
    text = replace_once(
        text,
        '''              pv.summary_override as project_summary,
              pv.analysis_status as project_analysis_status,
''',
        '''              coalesce(nullif(pv.user_summary, ''), (
                select coalesce(vision_summary, '') from frames
                where frames.video_id=pv.video_id
                order by timestamp_seconds, id limit 1
              ), '') as project_summary,
              coalesce(pv.user_summary, '') as user_summary,
              pv.user_summary_updated_at,
              coalesce(pv.summary_migration_state, 'none') as user_summary_migration_state,
              pv.summary_override as legacy_summary_override,
              pv.analysis_status as project_analysis_status,
''',
        "project media summary projection",
    )
    path.write_text(text, encoding="utf-8")


def patch_project() -> None:
    path = Path("src/video_vault/project.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from .database import create_project_row, frames, init_db, project, project_bgm_tracks, project_videos, projects, segments, set_project_status, set_project_videos\n",
        "from .database import create_project_row, frames, init_db, project, project_bgm_tracks, project_videos, projects, segments, set_project_status, set_project_videos\nfrom .story_context import story_context\n",
        "story context import",
    )
    text = replace_once(
        text,
        '''        project_summary = video.get("project_summary")
        visual_summary = str(project_summary) if project_summary is not None else _visual_summary(db, int(video["id"]))
        data = {
''',
        '''        ai_visual_summary = _visual_summary(db, int(video["id"]))
        user_summary = str(video.get("user_summary") or "").strip()
        effective_summary = user_summary or ai_visual_summary
        effective_summary_source = "user" if user_summary else ("ai" if ai_visual_summary else "none")
        data = {
''',
        "clip summary contract",
    )
    text = replace_once(
        text,
        '''            "visual_summary": visual_summary,
            "analysis_current": bool(perception_state.get("analysis_current")),
''',
        '''            "visual_summary": ai_visual_summary,
            "ai_visual_summary": ai_visual_summary,
            "user_summary": user_summary,
            "user_summary_updated_at": video.get("user_summary_updated_at"),
            "user_summary_migration_state": video.get("user_summary_migration_state") or "none",
            "effective_summary": effective_summary,
            "effective_summary_source": effective_summary_source,
            "analysis_current": bool(perception_state.get("analysis_current")),
''',
        "clip provenance fields",
    )
    text = replace_once(
        text,
        '''        payload = json.dumps(data, ensure_ascii=False, indent=2)
        (stable_clip_dir / "clip.json").write_text(payload, encoding="utf-8")
        # clip_001 remains a display alias for compatibility; stable references use project_media_id.
        (display_clip_dir / "clip.json").write_text(payload, encoding="utf-8")
''',
        '''        payload = json.dumps(data, ensure_ascii=False, indent=2)
        _atomic_write_text(stable_clip_dir / "clip.json", payload)
        # clip_001 remains a display alias for compatibility; stable references use project_media_id.
        _atomic_write_text(display_clip_dir / "clip.json", payload)
''',
        "clip atomic writes",
    )
    text = replace_block(
        text,
        r"        if not video_segments:.*?            continue\n        for seg in video_segments:.*?            \)\n    ordered =",
        '''        if not video_segments:
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
                }
            )
    ordered =''',
        "story grouping",
    )
    text = replace_once(
        text,
        '''        group["clips"] = _dedupe(group["clips"])
        group["segments"].sort''',
        '''        group["clips"] = _dedupe(group["clips"])
        group["story_context"] = _dedupe_story_context(group["story_context"])
        group["segments"].sort''',
        "dedupe story context",
    )
    text = replace_once(
        text,
        '''        "clips": [_clip_summary(c) for c in clips],
        "groups": ordered,
''',
        '''        "clips": [_clip_summary(c) for c in clips],
        "story_context_usage": [
            _story_context_usage(c, story_context(c["user_summary"], c["ai_visual_summary"], "其他"))
            for c in clips if c.get("user_summary")
        ],
        "groups": ordered,
''',
        "plan story context usage",
    )
    text = replace_once(
        text,
        '''        "feedback_applied": [],
''',
        '''        "feedback_applied": [
            f"{item['clip_id']} 使用 user_summary 指引故事分組"
            for item in [
                _story_context_usage(c, story_context(c["user_summary"], c["ai_visual_summary"], "其他"))
                for c in clips if c.get("user_summary")
            ]
        ],
''',
        "plan feedback provenance",
    )
    text = replace_once(
        text,
        '''    if plan.get("revision_notes"):
        lines += ["", "## 審核備註", plan["revision_notes"]]
        for item in plan.get("feedback_unresolved", []):
            lines.append(f"- 尚未自動處理：{item}")
    lines += ["", "## 自動字卡"]
''',
        '''    if plan.get("revision_notes"):
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
''',
        "script user context",
    )
    text = replace_once(
        text,
        '''def _group(label: str, time_of_day: str, activity: str, order: int = 999) -> dict:
    return {"label": label, "time_of_day": time_of_day, "activity": activity, "order": order, "clips": [], "segments": []}
''',
        '''def _group(label: str, time_of_day: str, activity: str, order: int = 999) -> dict:
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
''',
        "story context helpers",
    )
    text = replace_once(
        text,
        '''def _clip_summary(clip: dict) -> dict:
    return {k: clip[k] for k in ("clip_id", "project_media_id", "video_id", "filename", "source_path", "order", "duration_seconds", "detected_category", "time_of_day", "status", "segment_count", "visual_summary", "analysis_current", "perception_run")}
''',
        '''def _clip_summary(clip: dict) -> dict:
    return {k: clip[k] for k in (
        "clip_id", "project_media_id", "video_id", "filename", "source_path", "order",
        "duration_seconds", "detected_category", "time_of_day", "status", "segment_count",
        "visual_summary", "ai_visual_summary", "user_summary", "user_summary_updated_at",
        "user_summary_migration_state", "effective_summary", "effective_summary_source",
        "analysis_current", "perception_run",
    )}
''',
        "clip summary explicit fields",
    )
    path.write_text(text, encoding="utf-8")


def patch_ui() -> None:
    path = Path("src/video_vault/ui.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''                ok = update_clip_summary(cfg, db, project_id, int(data.get("video_id", 0)), str(data.get("summary", "")))
                self._json({"ok": ok})
''',
        '''                user_summary = data.get("user_summary", data.get("summary", ""))
                ok = update_clip_summary(cfg, db, project_id, int(data.get("video_id", 0)), str(user_summary))
                self._json({"ok": ok, "plan_rebuilt": ok})
''',
        "clip summary API",
    )
    text = replace_once(
        text,
        '''    ok = update_video_summary(db, video_id, summary.strip(), project_id=project_id)
    if ok:
        mark_project_needs_review(cfg, db, project_id)
    return ok
''',
        '''    ok = update_video_summary(db, video_id, summary.strip(), project_id=project_id)
    if ok:
        build_project_plan(cfg, db, project_id)
    return ok
''',
        "rebuild story after user summary",
    )
    path.write_text(text, encoding="utf-8")


def patch_frontend_api() -> None:
    path = Path("web/src/api.ts")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''  visual_summary: string;
};
''',
        '''  visual_summary: string;
  ai_visual_summary: string;
  user_summary: string;
  user_summary_updated_at?: string | null;
  user_summary_migration_state?: string;
  effective_summary: string;
  effective_summary_source: "user" | "ai" | "none";
};
''',
        "clip summary types",
    )
    text = replace_once(
        text,
        '''  saveClipSummary: (projectId: number, videoId: number, summary: string) =>
    json<{ ok: boolean }>("/api/project/clip-summary", post({ project_id: projectId, video_id: videoId, summary })),
''',
        '''  saveClipSummary: (projectId: number, videoId: number, userSummary: string) =>
    json<{ ok: boolean; plan_rebuilt?: boolean }>("/api/project/clip-summary", post({ project_id: projectId, video_id: videoId, user_summary: userSummary })),
''',
        "clip summary API payload",
    )
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_database()
    patch_project()
    patch_ui()
    patch_frontend_api()
