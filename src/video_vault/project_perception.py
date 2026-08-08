from __future__ import annotations

from pathlib import Path
import json

from .audio_perception import AudioPerceptionError, analyze_audio_file
from .analyzer.multi_frame import (
    MultiFrameValidationError,
    build_frame_windows,
    provider_capability,
    update_window_evidence_segment_uuid,
)
from .analyzer.vision_pipeline import AnalysisCancelled, analyze_frame_manifest, analyze_frame_windows, provider_from_config
from .database import project_ids_for_video, project_videos, segments as db_segments
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
    set_run_audio_perception,
    set_run_window_manifest,
    set_run_window_results,
    set_run_window_validation,
    set_run_output_path,
    set_run_provider_contract,
    set_run_sampling_manifest,
    set_run_segment_uuid_mapping,
    snapshot_metadata_paths,
    validate_run_inputs,
)
from .sampling import (
    build_sampling_plan,
    dedupe_visual_samples,
    sampling_contract_hash,
)
from .planner import draft_plan, perceive_output, video_dir, write_plan_files
from .project import build_project_plan, project_dir
from .project_lifecycle import ProjectRevisionConflict, current_revision
from .segment_state_migration import migrate_segment_state_for_video
from .project_media import ensure_project_media_ownership


class ProjectMediaOwnershipError(RuntimeError):
    """Raised when legacy shared media cannot be safely perceived in one project."""


def run_project_perception(
    cfg: dict,
    db: Path,
    project_id: int,
    video: dict,
    progress=None,
    should_cancel=None,
    base_revision: int | None = None,
    sampling_override: dict | None = None,
) -> dict:
    """Run one project perception generation and publish it coherently.

    Frame extraction and AI results stay inside the run directory. Published DB
    rows and project metadata are only switched after validation. If migration,
    artifact generation, or plan rebuild fails, the prior published state is
    restored while the newest run remains persistently failed/cancelled.
    """
    _raise_if_cancelled(should_cancel)
    linked_project_ids = project_ids_for_video(db, int(video["id"]))
    if len(linked_project_ids) > 1:
        raise ProjectMediaOwnershipError(
            "此素材仍被多個專案共用；為避免感知結果污染其他專案，請先建立專案專屬素材後再感知"
        )
    ensure_project_media_ownership(cfg, db, project_id)
    if project_id not in linked_project_ids:
        linked_project_ids.append(project_id)
    captured_revisions = {int(item): current_revision(db, int(item)) for item in linked_project_ids}
    if base_revision is not None and int(base_revision) != captured_revisions[int(project_id)]:
        raise ProjectRevisionConflict(project_id, int(base_revision), captured_revisions[int(project_id)])
    run = create_perception_run(
        db,
        cfg,
        project_id,
        video,
        sampling_override=sampling_override,
    )
    run_uuid = str(run["run_uuid"])
    staging = run_staging_dir(cfg, run_uuid)
    published_snapshot: dict | None = None
    metadata_snapshot: list[dict] = []
    published = False
    try:
        _raise_if_cancelled(should_cancel)
        frame_dir = staging / "frames"
        sampling = build_sampling_plan(
            Path(video["current_path"]),
            float(video.get("duration_seconds") or 0),
            cfg,
            sampling_override,
        )
        samples = list(sampling["samples"])
        extract_cfg = {
            **cfg,
            "_frame_timestamps": [
                float(sample["timestamp_seconds"]) for sample in samples
            ],
        }
        frame_paths = extract_frames(Path(video["current_path"]), frame_dir, extract_cfg)
        if len(frame_paths) != len(samples):
            raise RuntimeError(
                f"adaptive frame extraction count mismatch: expected {len(samples)}, got {len(frame_paths)}"
            )
        frame_paths, samples, visual_dedupe = dedupe_visual_samples(
            frame_paths,
            samples,
            cfg,
            sampling["policy"],
        )
        sampling["samples"] = samples
        sampling["visual_dedupe"] = visual_dedupe
        sampling["estimated_vision_calls"] = len(samples)
        sampling["sample_reason_counts"] = _sample_reason_counts(samples)
        sampling["contract_hash"] = sampling_contract_hash(
            run["input_snapshot"]["source"],
            sampling["policy"],
            samples,
        )
        set_run_sampling_manifest(db, run_uuid, sampling)
        manifest = build_frame_manifest(frame_paths, cfg, samples)
        set_run_frame_manifest(db, run_uuid, manifest)
        errors = validate_run_inputs(
            analysis_run(db, run_uuid),
            video,
            cfg,
            manifest,
            sampling_override,
        )
        if errors:
            raise RuntimeError("; ".join(errors))
        _raise_if_cancelled(should_cancel)

        multi_frame_config = ((cfg.get("perception") or {}).get("multi_frame") or {})
        multi_frame_enabled = bool(
            multi_frame_config.get("enabled", "sampling" in cfg)
        )
        max_images = 0
        if multi_frame_enabled:
            max_images = int(provider_capability(provider_from_config(cfg), cfg).get("maximum_images") or 0)
        windows = build_frame_windows(
            manifest,
            float(video.get("duration_seconds") or 0),
            max_frames=max_images if multi_frame_enabled and max_images >= 3 else 5,
        )
        set_run_window_manifest(db, run_uuid, windows)
        sampling["estimated_vision_calls"] = len(windows) if len(manifest) >= 3 else len(manifest)
        set_run_sampling_manifest(db, run_uuid, sampling)
        analysis_cfg = {**cfg, "_sampling_policy": sampling["policy"]}
        # Configs created by the current loader contain the sampling policy and
        # opt into Phase 2. Small legacy/test configs without that policy keep
        # the old single-frame contract for backward compatibility.
        if multi_frame_enabled:
            result = analyze_frame_windows(
                video,
                analysis_cfg,
                manifest,
                progress,
                should_cancel=should_cancel,
                duration_seconds=float(video.get("duration_seconds") or 0),
                evidence_root=staging / "evidence",
                windows=windows,
            )
        else:
            result = analyze_frame_manifest(
                video,
                analysis_cfg,
                manifest,
                progress,
                should_cancel=should_cancel,
            )
            result.setdefault("window_manifest", windows)
            result.setdefault("window_results", [])
            result.setdefault("window_validation", {"status": "skipped", "reason": "legacy config"})
        if multi_frame_enabled and (result.get("window_validation") or {}).get("status") == "blocked":
            raise MultiFrameValidationError(
                "multi-frame evidence validation blocked: "
                + ", ".join((result.get("window_validation") or {}).get("needs_review_reasons") or [])
            )
        set_run_window_results(db, run_uuid, result.get("window_results") or [])
        set_run_window_validation(db, run_uuid, result.get("window_validation") or {})
        if result.get("multi_frame_contract"):
            set_run_provider_contract(db, run_uuid, result["multi_frame_contract"])
        audio_perception = _run_local_audio_perception(cfg, video, run, result.get("segments") or [])
        set_run_audio_perception(db, run_uuid, audio_perception)
        sampling["actual_vision_calls"] = int(result.get("vision_calls") or 0)
        sampling["cache_hits"] = int(result.get("cache_hits") or 0)
        set_run_sampling_manifest(db, run_uuid, sampling)
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
        (staging / "rollback_snapshot.json").write_text(
            json.dumps(
                {"live": published_snapshot, "metadata": metadata_snapshot},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        migration = publish_staged_results(
            db,
            run_uuid,
            result["frames"],
            result["segments"],
        )
        published = True
        published_segments = sorted(
            (dict(row) for row in db_segments(db, int(video["id"]))),
            key=lambda row: (float(row.get("start_seconds") or 0), int(row.get("id") or 0)),
        )
        segment_by_window: dict[str, dict] = {}
        for row in published_segments:
            window_uuid = str(row.get("window_uuid") or "")
            if window_uuid and window_uuid not in segment_by_window:
                segment_by_window[window_uuid] = row
        for window_result in result.get("window_results") or []:
            window_uuid = str(window_result.get("window_uuid") or "")
            published_row = segment_by_window.get(window_uuid)
            if not published_row:
                raise RuntimeError(f"published segment mapping is missing: {window_uuid}")
            stable_uuid = str(published_row.get("segment_uuid") or "")
            if not stable_uuid:
                raise RuntimeError(f"published segment has no stable identity: {window_uuid}")
            window_result["segment_uuid"] = stable_uuid
            window_result["publish_status"] = "published"
            update_window_evidence_segment_uuid(staging / "evidence", window_uuid, stable_uuid)
        for segment_result in result.get("segments") or []:
            window_uuid = str(segment_result.get("window_uuid") or "")
            published_row = segment_by_window.get(window_uuid)
            if published_row:
                segment_result["segment_uuid"] = str(published_row.get("segment_uuid") or "")
        _attach_published_audio_segment_ids(audio_perception, published_segments)
        set_run_audio_perception(db, run_uuid, audio_perception)
        set_run_segment_uuid_mapping(
            db,
            run_uuid,
            {
                str(item.get("window_uuid") or ""): str(item.get("segment_uuid") or "")
                for item in result.get("window_results") or []
                if item.get("segment_uuid")
            },
        )
        set_run_window_results(db, run_uuid, result.get("window_results") or [])
        result_path = staging / "result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        set_run_output_path(db, run_uuid, result_path)
        _raise_if_cancelled(should_cancel)

        project_migrations = migrate_segment_state_for_video(
            cfg,
            db,
            int(video["id"]),
            migration,
            project_id=project_id,
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
            "sampling": sampling,
            "audio_perception": audio_perception,
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


def _run_local_audio_perception(cfg: dict, video: dict, run: dict, visual_segments: list[dict]) -> dict:
    audio_config = ((cfg.get("perception") or {}).get("audio") or {})
    if not bool(audio_config.get("enabled", False)):
        return {
            "schema_version": "audio-perception-v1",
            "status": "disabled",
            "provider": "local",
            "model": "pcm-features-v1",
            "audit": {
                "local_only": True,
                "transcription_requested": False,
                "cloud_audio_requested": False,
                "user_decisions_overridden": False,
            },
            "candidates": [],
        }
    try:
        return analyze_audio_file(
            Path(video["current_path"]),
            cfg,
            video_id=int(video["id"]),
            duration_seconds=float(video.get("duration_seconds") or 0),
            source_fingerprint=(run.get("input_snapshot") or {}).get("source") if isinstance(run.get("input_snapshot"), dict) else None,
            visual_segments=visual_segments,
        )
    except AudioPerceptionError as exc:
        return {
            "schema_version": "audio-perception-v1",
            "status": "failed",
            "provider": "local",
            "model": "pcm-features-v1",
            "error": str(exc),
            "audit": {
                "local_only": True,
                "transcription_requested": False,
                "cloud_audio_requested": False,
                "user_decisions_overridden": False,
            },
            "candidates": [],
        }


def _attach_published_audio_segment_ids(audio_perception: dict, published_segments: list[dict]) -> None:
    """Replace pending visual-window identities with the published stable UUID."""

    for candidate in audio_perception.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        start = float(candidate.get("start_seconds") or 0)
        end = float(candidate.get("end_seconds") or start)
        best = None
        for segment in published_segments:
            overlap = max(
                0.0,
                min(end, float(segment.get("end_seconds") or 0))
                - max(start, float(segment.get("start_seconds") or 0)),
            )
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, segment)
        if best and best[1].get("segment_uuid"):
            candidate["segment_uuid"] = str(best[1]["segment_uuid"])
            candidate["segment_identity_source"] = "visual_segment_uuid"


def _sample_reason_counts(samples: list[dict]) -> dict[str, int]:
    result = {"baseline": 0, "scene": 0, "motion": 0, "boundary": 0}
    for sample in samples:
        for reason in set(sample.get("reasons") or []):
            result[str(reason)] = int(result.get(str(reason), 0)) + 1
    return result


def _metadata_paths(cfg: dict, project_ids: list[int], video_id: int) -> list[Path]:
    paths = [video_dir(cfg, video_id)]
    for project_id in project_ids:
        project_root = project_dir(cfg, project_id)
        paths.extend(
            [
                project_root / "project_plan.json",
                project_root / "project_script.md",
                project_root / "review_status.json",
                project_root / "feedback" / "segment_review.json",
                project_root / "segment_review.json",
                project_root / "storyboard.json",
                project_root / "audio_settings.json",
                project_root / "color_consistency.json",
                project_root / "plans",
                project_root / "clips",
                project_root / "decisions",
                project_root / "checkpoints",
            ]
        )
    return paths
