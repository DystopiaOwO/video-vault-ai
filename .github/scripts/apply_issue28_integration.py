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
    if count == 0:
        if replacement.strip() in text:
            return text
        raise SystemExit(f"missing block target: {label}")
    return updated


def patch_ui() -> None:
    path = Path("src/video_vault/ui.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from .project import build_project_plan, can_project_render, create_project, list_projects, mark_project_needs_review, project_detail, project_dir, save_revision_notes, save_segment_review, set_review_status, sync_project_files, update_segment_timing\n",
        "from .project import build_project_plan, can_project_render, create_project, list_projects, mark_project_needs_review, project_detail, project_dir, save_revision_notes, save_segment_review, set_review_status, sync_project_files, update_segment_timing\nfrom .project_perception import run_project_perception\nfrom .perception_runs import PerceptionCancelled, ensure_perception_schema, perception_jobs, recover_interrupted_perception_runs\n",
        "ui imports",
    )
    text = replace_once(
        text,
        "    init_db(db)\n    render_manager = RenderJobManager(cfg, db)\n",
        "    init_db(db)\n    ensure_perception_schema(db)\n    recover_interrupted_perception_runs(db)\n    render_manager = RenderJobManager(cfg, db)\n",
        "run_ui recovery",
    )
    text = replace_once(
        text,
        "                self._json(project_jobs(project_id, render_manager) if project_id else project_jobs(0, render_manager))\n",
        "                self._json(project_jobs(project_id, render_manager, db) if project_id else project_jobs(0, render_manager, db))\n",
        "api jobs",
    )
    text = replace_once(
        text,
        "    jobs = project_jobs(project_id, render_manager) if project_id else []\n",
        "    jobs = project_jobs(project_id, render_manager, db) if project_id else []\n",
        "classic jobs",
    )
    text = replace_block(
        text,
        r"def project_jobs\(project_id: int, render_manager: RenderJobManager \| None = None\) -> list\[dict\]:.*?\n\ndef stop_project_jobs",
        '''def project_jobs(project_id: int, render_manager: RenderJobManager | None = None, db: Path | None = None) -> list[dict]:
    with JOBS_LOCK:
        legacy = [
            dict(job, project_id=pid, legacy_job_key=name)
            for (pid, name), job in JOBS.items()
            if project_id == 0 or pid == project_id
        ]
    persistent = []
    if db is not None and project_id:
        persistent.extend(perception_jobs(db, project_id))
    if render_manager is not None:
        persistent.extend(
            dict(job, kind="正式輸出")
            for job in render_manager.list(None if project_id == 0 else project_id)
        )
    if any(job.get("kind") == "內容感知" for job in persistent):
        legacy = [job for job in legacy if job.get("legacy_job_key") != "analyze"]
    return legacy + persistent


def stop_project_jobs''',
        "project_jobs",
    )
    text = replace_block(
        text,
        r"def _analyze_job\(cfg: dict, db: Path, project_id: int, force: bool\) -> None:.*?\n\ndef _analyze_video_job",
        '''def _analyze_job(cfg: dict, db: Path, project_id: int, force: bool) -> None:
    try:
        rows = [dict(row) for row in project_videos(db, project_id)]
        todo = [row for row in rows if force or row.get("status") != "perceived"]
        _set_job(project_id, "analyze", status="running", message="正在準備內容感知", done=0, total=len(todo), percent=0)
        processed = []
        for index, video in enumerate(todo, 1):
            if _job_stopped(project_id, "analyze"):
                return
            _set_job(project_id, "analyze", message=f"正在分析 {video.get('filename', '')}", done=index - 1)
            result = run_project_perception(
                cfg,
                db,
                project_id,
                video,
                _analyze_progress(project_id, video.get("filename", ""), index - 1, max(len(todo), 1)),
                should_cancel=lambda: _job_stopped(project_id, "analyze"),
            )
            processed.extend(result.get("processed", []))
            _set_job(project_id, "analyze", message=f"已完成 {video.get('filename', '')}", done=index)
        if todo:
            build_project_plan(cfg, db, project_id)
        _set_job(project_id, "analyze", status="done", message=f"內容感知完成：{len(processed)} 支", processed=processed, percent=100)
    except PerceptionCancelled:
        _set_job(project_id, "analyze", status="stopped", message="內容感知已由使用者停止")
    except Exception as exc:
        _set_job(project_id, "analyze", status="failed", message=f"內容感知失敗：{exc}")


def _analyze_video_job''',
        "bulk analyze job",
    )
    text = replace_block(
        text,
        r"def _analyze_video_job\(cfg: dict, db: Path, project_id: int, video_id: int\) -> None:.*?\n\ndef _analyze_progress",
        '''def _analyze_video_job(cfg: dict, db: Path, project_id: int, video_id: int) -> None:
    try:
        video = next((dict(v) for v in project_videos(db, project_id) if int(v["id"]) == video_id), None)
        if not video:
            _set_job(project_id, "analyze", status="failed", message="找不到這支素材", percent=100)
            return
        _set_job(project_id, "analyze", status="running", message=f"正在分析 {video.get('filename', '')}", done=0, total=1, percent=0)
        analyze_project_video(
            cfg,
            db,
            project_id,
            video_id,
            _analyze_progress(project_id, video.get("filename", ""), 0, 1),
            should_cancel=lambda: _job_stopped(project_id, "analyze"),
        )
        _set_job(project_id, "analyze", status="done", message=f"單支素材感知完成：{video.get('filename', '')}", done=1, total=1, percent=100)
    except PerceptionCancelled:
        _set_job(project_id, "analyze", status="stopped", message="單支素材感知已由使用者停止")
    except Exception as exc:
        _set_job(project_id, "analyze", status="failed", message=f"單支素材感知失敗：{exc}")


def _analyze_progress''',
        "single analyze job",
    )
    text = replace_block(
        text,
        r"def analyze_project\(cfg: dict, db: Path, project_id: int, force: bool = False\) -> dict:.*?\n\ndef analyze_project_video",
        '''def analyze_project(cfg: dict, db: Path, project_id: int, force: bool = False) -> dict:
    processed = []
    for row in project_videos(db, project_id):
        video = dict(row)
        if not force and video.get("status") == "perceived":
            continue
        result = run_project_perception(cfg, db, project_id, video)
        processed.extend(result.get("processed", []))
    if processed:
        build_project_plan(cfg, db, project_id)
    return {"ok": True, "processed": processed}


def analyze_project_video''',
        "analyze_project",
    )
    text = replace_block(
        text,
        r"def analyze_project_video\(cfg: dict, db: Path, project_id: int, video_id: int, progress=None\) -> dict:.*?\n\ndef update_clip_summary",
        '''def analyze_project_video(cfg: dict, db: Path, project_id: int, video_id: int, progress=None, should_cancel=None) -> dict:
    video = next((dict(v) for v in project_videos(db, project_id) if int(v["id"]) == video_id), None)
    if not video:
        return {"ok": False, "error": "video not found in project"}
    result = run_project_perception(cfg, db, project_id, video, progress, should_cancel)
    return {"ok": True, **result}


def update_clip_summary''',
        "analyze_project_video",
    )
    text = replace_once(
        text,
        "    ok = update_video_summary(db, video_id, summary.strip())\n",
        "    ok = update_video_summary(db, video_id, summary.strip(), project_id=project_id)\n",
        "project summary ownership",
    )
    path.write_text(text, encoding="utf-8")


def patch_project() -> None:
    path = Path("src/video_vault/project.py")
    text = path.read_text(encoding="utf-8")
    text = replace_block(
        text,
        r"def sync_project_files\(cfg: dict, db: Path, project_id: int\) -> list\[dict\]:.*?\n\ndef build_project_plan",
        '''def sync_project_files(cfg: dict, db: Path, project_id: int) -> list[dict]:
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
        project_summary = video.get("project_summary")
        visual_summary = str(project_summary) if project_summary is not None else _visual_summary(db, int(video["id"]))
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
            "visual_summary": visual_summary,
            "analysis_current": bool(perception_state.get("analysis_current")),
            "perception_run": perception_state,
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        (stable_clip_dir / "clip.json").write_text(payload, encoding="utf-8")
        # clip_001 remains a display alias for compatibility; stable references use project_media_id.
        (display_clip_dir / "clip.json").write_text(payload, encoding="utf-8")
        result.append(data)
    return result


def build_project_plan''',
        "sync_project_files",
    )
    text = replace_once(
        text,
        "    public_plan = _public_plan_bgm(plan)\n    public_segments = []\n",
        "    public_plan = _public_plan_bgm(plan)\n    clips = sync_project_files(cfg, db, project_id)\n    public_segments = []\n",
        "project detail clips",
    )
    text = replace_once(
        text,
        '        "clips": sync_project_files(cfg, db, project_id),\n        "bgm": public_bgm,\n',
        '        "clips": clips,\n        "perception_runs": [clip.get("perception_run", {}) for clip in clips],\n        "bgm": public_bgm,\n',
        "project detail run metadata",
    )
    text = replace_once(
        text,
        '        _stage("perception", "內容感知", any(c.get("segment_count", 0) for c in clips), [folder / "clips"]),\n',
        '        _stage("perception", "內容感知", bool(clips) and all(c.get("analysis_current") for c in clips), [folder / "clips"]),\n',
        "workflow current generation",
    )
    text = replace_once(
        text,
        '    return {k: clip[k] for k in ("clip_id", "project_media_id", "video_id", "filename", "source_path", "order", "duration_seconds", "detected_category", "time_of_day", "status", "segment_count", "visual_summary")}\n',
        '    return {k: clip[k] for k in ("clip_id", "project_media_id", "video_id", "filename", "source_path", "order", "duration_seconds", "detected_category", "time_of_day", "status", "segment_count", "visual_summary", "analysis_current", "perception_run")}\n',
        "clip summary run metadata",
    )
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_ui()
    patch_project()
