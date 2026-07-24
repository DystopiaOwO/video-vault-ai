from __future__ import annotations

from pathlib import Path
import json

import pytest

from video_vault.database import (
    add_frame,
    connect,
    frames,
    init_db,
    project,
    project_videos,
    replace_segments,
    segments,
    set_project_status,
    set_video_status,
    upsert_video,
)
from video_vault.perception_runs import (
    PerceptionCancelled,
    analysis_run,
    build_frame_manifest,
    capture_live_results,
    create_perception_run,
    finalize_perception_run,
    mark_perception_run_terminal,
    perception_jobs,
    perception_states_for_project,
    publish_staged_results,
    recover_interrupted_perception_runs,
    restore_live_results,
    run_staging_dir,
    set_run_frame_manifest,
    validate_run_inputs,
)
from video_vault.project import create_project, project_workflow
from video_vault.project_perception import run_project_perception
import video_vault.project_perception as project_perception


def _cfg(root: Path, *, interval: int = 5) -> dict:
    return {
        "library_root": str(root),
        "frame_interval_seconds": interval,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "ai": {"provider": "mock", "model": "mock-v1"},
    }


def _segment(title: str, start: float = 0, end: float = 5) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "segment_type": "key_action",
        "title": title,
        "reason": title,
        "tags": ["coffee", "hands"],
        "score": 0.9,
        "suggested_use": "main",
    }


def _project_video(tmp_path: Path, *, name: str = "A") -> tuple[Path, int, int, dict, dict]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source-v1")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 10,
            "status": "uploaded",
        },
    )
    project_id = create_project(db, name, [video_id])
    video = dict(project_videos(db, project_id)[0])
    return db, project_id, video_id, video, _cfg(tmp_path)


def _frame_results(label: str, paths: list[Path]) -> list[dict]:
    return [
        {
            "frame_path": str(path),
            "timestamp_seconds": index * 5,
            "summary": f"{label}-{index}",
            "tags": [label, "coffee"],
            "visual_quality_score": 0.8,
            "usefulness_score": 0.9,
        }
        for index, path in enumerate(paths)
    ]


def _publish_success(
    db: Path,
    cfg: dict,
    project_id: int,
    video: dict,
    label: str,
) -> dict:
    run = create_perception_run(db, cfg, project_id, video)
    frame_dir = run_staging_dir(cfg, run["run_uuid"]) / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    paths = [frame_dir / "frame_00000.jpg", frame_dir / "frame_00001.jpg"]
    for index, path in enumerate(paths):
        path.write_bytes(f"{label}-{index}".encode())
    manifest = build_frame_manifest(paths, cfg)
    set_run_frame_manifest(db, run["run_uuid"], manifest)
    assert validate_run_inputs(analysis_run(db, run["run_uuid"]), video, cfg, manifest) == []
    publish_staged_results(
        db,
        run["run_uuid"],
        _frame_results(label, paths),
        [_segment(label)],
    )
    return finalize_perception_run(db, run["run_uuid"])


def test_failed_newest_generation_keeps_last_successful_results_and_later_success_supersedes(tmp_path):
    db, project_id, video_id, video, cfg = _project_video(tmp_path)
    first = _publish_success(db, cfg, project_id, video, "first")
    assert frames(db, video_id)[0]["vision_summary"] == "first-0"
    assert segments(db, video_id)[0]["title"] == "first"

    second = create_perception_run(db, cfg, project_id, dict(project_videos(db, project_id)[0]))
    state = perception_states_for_project(db, project_id)[video_id]
    assert state["analysis_current"] is False
    assert state["stale_fallback_available"] is True
    assert state["last_successful_analysis_run_uuid"] == first["run_uuid"]

    mark_perception_run_terminal(db, second["run_uuid"], "failed", "provider unavailable")
    assert frames(db, video_id)[0]["vision_summary"] == "first-0"
    assert segments(db, video_id)[0]["title"] == "first"
    failed_state = perception_states_for_project(db, project_id)[video_id]
    assert failed_state["current_status"] == "failed"
    assert failed_state["analysis_current"] is False
    assert failed_state["stale_fallback_available"] is True

    third = _publish_success(db, cfg, project_id, dict(project_videos(db, project_id)[0]), "third")
    current = perception_states_for_project(db, project_id)[video_id]
    assert current["analysis_current"] is True
    assert current["current_analysis_run_uuid"] == third["run_uuid"]
    assert current["last_successful_analysis_run_uuid"] == third["run_uuid"]
    assert frames(db, video_id)[0]["vision_summary"] == "third-0"
    assert segments(db, video_id)[0]["title"] == "third"


def test_publish_snapshot_can_restore_failed_or_cancelled_generation(tmp_path):
    db, project_id, video_id, video, cfg = _project_video(tmp_path)
    first = _publish_success(db, cfg, project_id, video, "stable")

    for terminal in ("failed", "cancelled"):
        run = create_perception_run(db, cfg, project_id, dict(project_videos(db, project_id)[0]))
        snapshot = capture_live_results(db, video_id)
        frame_dir = run_staging_dir(cfg, run["run_uuid"]) / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        paths = [frame_dir / "frame_00000.jpg", frame_dir / "frame_00001.jpg"]
        for path in paths:
            path.write_bytes(b"new")
        publish_staged_results(
            db,
            run["run_uuid"],
            _frame_results("temporary", paths),
            [_segment("temporary")],
        )
        assert frames(db, video_id)[0]["vision_summary"] == "temporary-0"
        restore_live_results(db, snapshot, run["run_uuid"], terminal, terminal)

        assert analysis_run(db, run["run_uuid"])["status"] == terminal
        assert frames(db, video_id)[0]["vision_summary"] == "stable-0"
        assert segments(db, video_id)[0]["title"] == "stable"
        state = perception_states_for_project(db, project_id)[video_id]
        assert state["last_successful_analysis_run_uuid"] == first["run_uuid"]
        assert state["analysis_current"] is False
        assert state["stale_fallback_available"] is True


def test_frame_manifest_detects_source_config_and_file_drift(tmp_path):
    db, project_id, _video_id, video, cfg = _project_video(tmp_path)
    run = create_perception_run(db, cfg, project_id, video)
    frame_dir = run_staging_dir(cfg, run["run_uuid"]) / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    paths = [frame_dir / "frame_00000.jpg", frame_dir / "frame_00001.jpg"]
    for path in paths:
        path.write_bytes(b"frame")
    manifest = build_frame_manifest(paths, cfg)
    set_run_frame_manifest(db, run["run_uuid"], manifest)
    current = analysis_run(db, run["run_uuid"])
    assert validate_run_inputs(current, video, cfg, manifest) == []

    Path(video["current_path"]).write_bytes(b"source-v2")
    assert any("source or extractor" in item for item in validate_run_inputs(current, video, cfg, manifest))

    Path(video["current_path"]).write_bytes(b"source-v1")
    changed_cfg = _cfg(tmp_path, interval=4)
    changed_errors = validate_run_inputs(current, video, changed_cfg, manifest)
    assert any("source or extractor" in item for item in changed_errors)
    assert any("count mismatch" in item or "timestamps" in item for item in changed_errors)

    paths[1].unlink()
    missing_errors = validate_run_inputs(current, video, cfg, manifest)
    assert any("frame missing" in item for item in missing_errors)


def test_restart_marks_interrupted_run_failed_and_workflow_not_current(tmp_path):
    db, project_id, video_id, video, cfg = _project_video(tmp_path)
    run = create_perception_run(db, cfg, project_id, video)
    assert recover_interrupted_perception_runs(db) == 1
    assert analysis_run(db, run["run_uuid"])["status"] == "failed"
    state = perception_states_for_project(db, project_id)[video_id]
    assert state["analysis_current"] is False
    assert state["current_status"] == "failed"
    workflow = project_workflow(cfg, db, project_id)
    perception = next(stage for stage in workflow["stages"] if stage["id"] == "perception")
    assert perception["status"] == "pending"


def test_shared_run_invalidates_every_linked_project_and_is_visible_in_jobs(tmp_path):
    db, project_a, video_id, video, cfg = _project_video(tmp_path)
    project_b = create_project(db, "B", [video_id])
    set_project_status(db, project_a, "approved")
    set_project_status(db, project_b, "approved")

    run = create_perception_run(db, cfg, project_a, video)

    assert dict(project(db, project_a))["status"] == "needs_review"
    assert dict(project(db, project_b))["status"] == "needs_review"
    state_a = perception_states_for_project(db, project_a)[video_id]
    state_b = perception_states_for_project(db, project_b)[video_id]
    assert state_a["current_analysis_run_uuid"] == run["run_uuid"]
    assert state_b["current_analysis_run_uuid"] == run["run_uuid"]
    assert any(job["run_uuid"] == run["run_uuid"] for job in perception_jobs(db, project_b))


def _fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "frame_00000.jpg", out_dir / "frame_00001.jpg"]
    for index, path in enumerate(paths):
        path.write_bytes(f"frame-{index}".encode())
    return paths


def _fake_analyze(_video: dict, _cfg: dict, manifest: list[dict], progress=None, should_cancel=None) -> dict:
    if progress:
        for index, frame in enumerate(manifest, 1):
            progress(index, len(manifest), frame)
    paths = [Path(row["frame_path"]) for row in manifest]
    return {
        "provider": "mock",
        "model": "mock-v1",
        "frames": _frame_results("new", paths),
        "segments": [_segment("new")],
    }


def _patch_orchestrator(monkeypatch, *, fail_plan: bool = False) -> None:
    monkeypatch.setattr(project_perception, "extract_frames", _fake_extract)
    monkeypatch.setattr(project_perception, "analyze_frame_manifest", _fake_analyze)
    monkeypatch.setattr(project_perception, "migrate_segment_state_for_video", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_perception, "rename_after_perception", lambda _cfg, _db, video: video)
    monkeypatch.setattr(project_perception, "perceive_output", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "draft_plan", lambda _cfg, _db, video: {"video_id": video["id"]})
    monkeypatch.setattr(project_perception, "write_plan_files", lambda *args, **kwargs: None)
    if fail_plan:
        monkeypatch.setattr(
            project_perception,
            "build_project_plan",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plan publish failed")),
        )
    else:
        monkeypatch.setattr(project_perception, "build_project_plan", lambda *args, **kwargs: {})


def test_orchestrator_failure_after_db_publish_restores_old_results(tmp_path, monkeypatch):
    db, project_id, video_id, video, cfg = _project_video(tmp_path)
    first = _publish_success(db, cfg, project_id, video, "old")
    _patch_orchestrator(monkeypatch, fail_plan=True)

    with pytest.raises(RuntimeError, match="plan publish failed"):
        run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))

    assert frames(db, video_id)[0]["vision_summary"] == "old-0"
    assert segments(db, video_id)[0]["title"] == "old"
    state = perception_states_for_project(db, project_id)[video_id]
    assert state["current_status"] == "failed"
    assert state["last_successful_analysis_run_uuid"] == first["run_uuid"]
    assert state["stale_fallback_available"] is True


def test_orchestrator_cancellation_after_db_publish_restores_old_results(tmp_path, monkeypatch):
    db, project_id, video_id, video, cfg = _project_video(tmp_path)
    first = _publish_success(db, cfg, project_id, video, "old")
    _patch_orchestrator(monkeypatch)
    calls = {"count": 0}

    def cancel_after_publish() -> bool:
        calls["count"] += 1
        return calls["count"] >= 4

    with pytest.raises(PerceptionCancelled):
        run_project_perception(
            cfg,
            db,
            project_id,
            dict(project_videos(db, project_id)[0]),
            should_cancel=cancel_after_publish,
        )

    assert frames(db, video_id)[0]["vision_summary"] == "old-0"
    assert segments(db, video_id)[0]["title"] == "old"
    state = perception_states_for_project(db, project_id)[video_id]
    assert state["current_status"] == "cancelled"
    assert state["last_successful_analysis_run_uuid"] == first["run_uuid"]
    assert state["stale_fallback_available"] is True
