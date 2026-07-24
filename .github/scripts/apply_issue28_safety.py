from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise SystemExit(f"missing patch target: {label}")
    return text.replace(old, new, 1)


def patch_perception_runs() -> None:
    path = Path("src/video_vault/perception_runs.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace("import math\n", "")
    text = replace_once(
        text,
        '''        con.execute(
            """update project_videos
            set current_analysis_run_uuid=?, analysis_generation=?, analysis_status='analyzing'
            where video_id=?""",
            (run_uuid, generation, video_id),
        )
    return analysis_run(db, run_uuid)
''',
        '''        con.execute(
            """update project_videos
            set current_analysis_run_uuid=?, analysis_generation=?, analysis_status='analyzing'
            where video_id=?""",
            (run_uuid, generation, video_id),
        )
        con.execute(
            """update projects
            set status='needs_review', updated_at=current_timestamp
            where id in (select project_id from project_videos where video_id=?)""",
            (video_id,),
        )
    return analysis_run(db, run_uuid)
''',
        "invalidate linked projects",
    )
    text = replace_once(
        text,
        '''            """select * from analysis_runs
            where project_id=?
            order by id desc limit ?""",
            (int(project_id), int(limit)),
''',
        '''            """select distinct analysis_runs.* from analysis_runs
            where project_id=?
               or video_id in (select video_id from project_videos where project_id=?)
            order by id desc limit ?""",
            (int(project_id), int(project_id), int(limit)),
''',
        "shared run jobs",
    )
    path.write_text(text, encoding="utf-8")


def patch_project_perception() -> None:
    path = Path("src/video_vault/project_perception.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from .database import project_videos\n",
        "from .database import project_ids_for_video, project_videos\n",
        "linked project import",
    )
    text = replace_once(
        text,
        "from .project import build_project_plan, project_dir\n",
        "from .project import build_project_plan, mark_project_needs_review, project_dir\n",
        "review invalidation import",
    )
    text = replace_once(
        text,
        '''    run = create_perception_run(db, cfg, project_id, video)
    run_uuid = str(run["run_uuid"])
    staging = run_staging_dir(cfg, run_uuid)
''',
        '''    run = create_perception_run(db, cfg, project_id, video)
    run_uuid = str(run["run_uuid"])
    linked_project_ids = project_ids_for_video(db, int(video["id"]))
    if project_id not in linked_project_ids:
        linked_project_ids.append(project_id)
    for linked_project_id in linked_project_ids:
        mark_project_needs_review(cfg, db, linked_project_id)
    staging = run_staging_dir(cfg, run_uuid)
''',
        "linked project invalidation",
    )
    text = replace_once(
        text,
        '''        metadata_snapshot = snapshot_metadata_paths(
            _metadata_paths(cfg, project_id, int(video["id"])),
            staging / "rollback_metadata",
        )
''',
        '''        metadata_snapshot = snapshot_metadata_paths(
            _metadata_paths(cfg, linked_project_ids, int(video["id"])),
            staging / "rollback_metadata",
        )
''',
        "linked metadata snapshot",
    )
    text = replace_once(
        text,
        '''        write_plan_files(cfg, draft_plan(cfg, db, current_video))
        build_project_plan(cfg, db, project_id)
        completed = finalize_perception_run(db, run_uuid)
''',
        '''        write_plan_files(cfg, draft_plan(cfg, db, current_video))
        for linked_project_id in linked_project_ids:
            build_project_plan(cfg, db, linked_project_id)
        completed = finalize_perception_run(db, run_uuid)
''',
        "rebuild linked plans",
    )
    text = replace_once(
        text,
        '''def _metadata_paths(cfg: dict, project_id: int, video_id: int) -> list[Path]:
    project_root = project_dir(cfg, project_id)
    return [
        video_dir(cfg, video_id),
        project_root / "project_plan.json",
        project_root / "project_script.md",
        project_root / "review_status.json",
        project_root / "plans",
        project_root / "clips",
        project_root / "decisions",
        project_root / "checkpoints",
    ]
''',
        '''def _metadata_paths(cfg: dict, project_ids: list[int], video_id: int) -> list[Path]:
    paths = [video_dir(cfg, video_id)]
    for project_id in project_ids:
        project_root = project_dir(cfg, project_id)
        paths.extend(
            [
                project_root / "project_plan.json",
                project_root / "project_script.md",
                project_root / "review_status.json",
                project_root / "plans",
                project_root / "clips",
                project_root / "decisions",
                project_root / "checkpoints",
            ]
        )
    return paths
''',
        "linked metadata paths",
    )
    path.write_text(text, encoding="utf-8")


def patch_project() -> None:
    path = Path("src/video_vault/project.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''    public_plan = _public_plan_bgm(plan)
    clips = sync_project_files(cfg, db, project_id)
    public_segments = []
    for segment in project_segments(cfg, project_id, plan, db=db):
''',
        '''    clips = sync_project_files(cfg, db, project_id)
    publishing = any(
        str(clip.get("perception_run", {}).get("current_status") or "") == "publishing"
        for clip in clips
    )
    public_plan = {} if publishing else _public_plan_bgm(plan)
    public_segments = []
    for segment in ([] if publishing else project_segments(cfg, project_id, plan, db=db)):
''',
        "hide publishing plan",
    )
    text = replace_once(
        text,
        '''        "script": (folder / "project_script.md").read_text(encoding="utf-8") if (folder / "project_script.md").exists() else "",
''',
        '''        "script": "" if publishing else ((folder / "project_script.md").read_text(encoding="utf-8") if (folder / "project_script.md").exists() else ""),
''',
        "hide publishing script",
    )
    text = replace_once(
        text,
        '''    if row["status"] != "approved":
        return False, f"專案狀態是 {row['status']}，尚未核准"
    folder = project_dir(cfg, project_id)
''',
        '''    if row["status"] != "approved":
        return False, f"專案狀態是 {row['status']}，尚未核准"
    clips = sync_project_files(cfg, db, project_id)
    if clips and not all(clip.get("analysis_current") for clip in clips):
        return False, "目前感知 generation 尚未成功發布"
    folder = project_dir(cfg, project_id)
''',
        "render current generation gate",
    )
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_perception_runs()
    patch_project_perception()
    patch_project()
