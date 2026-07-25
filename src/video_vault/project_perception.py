from __future__ import annotations

from pathlib import Path
import json

from .analyzer.vision_pipeline import AnalysisCancelled, analyze_frame_manifest
from .database import project_ids_for_video, project_videos
from .ffmpeg_tools import extract_frames
from .naming import rename_after_perception
from .perception_runs import (
    PerceptionCancelled,
    analysis_run,
    build_frame_manifest,
    capture_live_results,
    create_perception_run,
    finalize_perception_run,
    mark_perception_run_terminal,
    publish_staged_results,
    restore_live_results,
    restore_metadata_paths,
    run_staging_dir,
    set_run_frame_manifest,
    set_run_output_path,
    snapshot_metadata_paths,
    validate_run_inputs,
)
from .planner import draft_plan, perceive_output, video_dir, write_plan_files
from .project import build_project_plan, project_dir
from .project_lifecycle import ProjectRevisionConflict, current_revision
from .segment_state_migration import migrate_segment_state_for_video


def run_project_perception(
    cfg: dict,
    db: Path,
    project_id: int,
    video: dict,
    progress=None,
    should_cancel=None,
    base_revision: int | None = None,
) -> dict:
    """Run one project perception generation and publish it coherently.

    Frame extraction and AI results stay inside the run directory. Published DB
    rows and project metadata are only switched after validation. If migration,
    artifact generation, or plan rebuild fails, the prior published state is
    restored while the newest run remains persistently failed/cancelled.
    """
    _raise_if_cancelled(should_cancel)
    linked_project_ids = project_ids_for_video(db, int(video["id"]))
    if project_id not in linked_project_ids:
        linked_project_ids.append(project_id)
    captured_revisions = {int(item): current_revision(db, int(item)) for item in linked_project_ids}
    if base_revision is not None and int(base_revision) != captured_revisions[int(project_id)]:
        raise ProjectRevisionConflict(project_id, int(base_revision), captured_revisions[int(project_id)])
    run = create_perception_run(db, cfg, project_id, video)
    run_uuid = str(run["run_uuid"])
    staging = run_staging_dir(cfg, run_uuid)
    published_snapshot: dict | None = None
    metadata_snapshot: list[dict] = []
    published = False
    try:
        _raise_if_cancelled(should_cancel)
        frame_dir = staging / "frames"
        frame_paths = extract_frames(Path(video["current_path"]), frame_dir, cfg)
        manifest = build_frame_manifest(frame_paths, cfg)
        set_run_frame_manifest(db, run_uuid, manifest)
        errors = validate_run_inputs(analysis_run(db, run_uuid), video, cfg, manifest)
        if errors:
            raise RuntimeError("; ".join(errors))
        _raise_if_cancelled(should_cancel)

        result = analyze_frame_manifest(
            video,
            cfg,
            manifest,
            progress,
            should_cancel=should_cancel,
        )
        result_path = staging / "result.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        set_run_output_path(db, run_uuid, result_path)
        _raise_if_cancelled(should_cancel)

        for linked_project_id in linked_project_ids:
            if current_revision(db, linked_project_id) != captured_revisions[linked_project_id]:
                raise PerceptionCancelled("專案版本已更新，這次感知結果不再發布")

        published_snapshot = capture_live_results(db, int(video["id"]))
        metadata_snapshot = snapshot_metadata_paths(
            _metadata_paths(cfg, linked_project_ids, int(video["id"])),
            staging / "rollback_metadata",
        )
        migration = publish_staged_results(
            db,
            run_uuid,
            result["frames"],
            result["segments"],
        )
        published = True
        _raise_if_cancelled(should_cancel)

        project_migrations = migrate_segment_state_for_video(
            cfg,
            db,
            int(video["id"]),
            migration,
        )
        current_video = next(
            (
                dict(row)
                for row in project_videos(db, project_id)
                if int(row["id"]) == int(video["id"])
            ),
            None,
        )
        if current_video is None:
            raise RuntimeError("project media disappeared before publish completion")
        current_video = rename_after_perception(cfg, db, current_video)
        perceive_output(cfg, db, current_video)
        write_plan_files(cfg, draft_plan(cfg, db, current_video))
        for linked_project_id in linked_project_ids:
            build_project_plan(cfg, db, linked_project_id, base_revision=captured_revisions[linked_project_id])
        completed = finalize_perception_run(db, run_uuid)
        return {
            **result,
            "run": completed,
            "segment_identity_migration": migration,
            "project_segment_state_migrations": project_migrations,
            "processed": [
                {
                    "id": int(current_video["id"]),
                    "filename": str(current_video.get("filename") or ""),
                }
            ],
        }
    except (AnalysisCancelled, PerceptionCancelled) as exc:
        if published and published_snapshot is not None:
            restore_live_results(db, published_snapshot, run_uuid, "cancelled", str(exc))
            restore_metadata_paths(metadata_snapshot)
        else:
            mark_perception_run_terminal(db, run_uuid, "cancelled", str(exc))
        raise PerceptionCancelled(str(exc)) from exc
    except Exception as exc:
        if published and published_snapshot is not None:
            restore_live_results(db, published_snapshot, run_uuid, "failed", str(exc))
            restore_metadata_paths(metadata_snapshot)
        else:
            mark_perception_run_terminal(db, run_uuid, "failed", str(exc))
        raise


def _raise_if_cancelled(should_cancel) -> None:
    if should_cancel and should_cancel():
        raise PerceptionCancelled("perception cancelled by user")


def _metadata_paths(cfg: dict, project_ids: list[int], video_id: int) -> list[Path]:
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
