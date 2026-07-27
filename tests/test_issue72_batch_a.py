from __future__ import annotations

import json
from pathlib import Path

from video_vault.database import connect, init_db, project_videos, upsert_video
from video_vault.perception_runs import (
    analysis_run,
    create_perception_run,
    finalize_perception_run,
    publish_staged_results,
    run_staging_dir,
)
from video_vault.project import create_project
from video_vault.project_media import ensure_project_media_ownership, rollback_project_media_ownership


def _fixture(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"immutable-source")
    video_id = upsert_video(db, {
        "original_path": str(source),
        "current_path": str(source),
        "filename": source.name,
        "category": "travel",
        "duration_seconds": 10,
        "status": "uploaded",
    })
    project_id = create_project(db, "Batch A", [video_id], category="travel")
    cfg = {
        "library_root": str(tmp_path),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "ai": {"provider": "mock", "model": "mock-v1"},
    }
    return db, cfg, project_id, video_id


def test_project_media_migration_is_repeatable_and_reversible(tmp_path):
    db, cfg, project_id, _video_id = _fixture(tmp_path)
    first = ensure_project_media_ownership(cfg, db, project_id)
    assert first["changed"] is True
    row = dict(project_videos(db, project_id)[0])
    fingerprint = json.loads(row["source_fingerprint_json"])
    assert row["ownership_state"] == "project_owned"
    assert fingerprint["sha256"]
    second = ensure_project_media_ownership(cfg, db, project_id)
    assert second["changed"] is False
    assert rollback_project_media_ownership(cfg, db, project_id) is True
    restored = dict(project_videos(db, project_id)[0])
    assert restored["source_fingerprint_json"] in ("", "{}")


def test_perception_results_are_recorded_in_run_scope_before_finalize(tmp_path):
    db, cfg, project_id, video_id = _fixture(tmp_path)
    video = dict(project_videos(db, project_id)[0])
    run = create_perception_run(db, cfg, project_id, video)
    frame = run_staging_dir(cfg, run["run_uuid"]) / "frame.jpg"
    frame.write_bytes(b"frame")
    publish_staged_results(
        db,
        run["run_uuid"],
        [{"frame_path": str(frame), "timestamp_seconds": 0, "summary": "景色", "tags": ["landscape"]}],
        [{"start_seconds": 0, "end_seconds": 5, "segment_type": "scene", "title": "開場", "score": 0.9}],
    )
    with connect(db) as con:
        staged_frame = con.execute("select * from analysis_run_frames where run_uuid=?", (run["run_uuid"],)).fetchone()
        staged_segment = con.execute("select * from analysis_run_segments where run_uuid=?", (run["run_uuid"],)).fetchone()
    assert staged_frame is not None and staged_segment is not None
    assert analysis_run(db, run["run_uuid"])["status"] == "publishing"
    assert dict(project_videos(db, project_id)[0])["source_fingerprint_json"] != "{}"
    assert finalize_perception_run(db, run["run_uuid"])["status"] == "succeeded"
    assert video_id > 0
