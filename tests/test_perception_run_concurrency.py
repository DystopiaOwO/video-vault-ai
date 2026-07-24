from __future__ import annotations

from pathlib import Path

import pytest

from video_vault.database import connect, init_db, project_videos, replace_segments, upsert_video
from video_vault.perception_runs import (
    capture_live_results,
    create_perception_run,
    mark_perception_run_terminal,
    publish_staged_results,
    restore_live_results,
)
from video_vault.project import create_project


def _cfg(root: Path) -> dict:
    return {
        "library_root": str(root),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ai": {"provider": "mock", "model": "mock-v1"},
    }


def _add_video(db: Path, root: Path, name: str) -> tuple[int, int, dict]:
    source = root / name
    source.write_bytes(name.encode())
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 5,
            "status": "uploaded",
        },
    )
    project_id = create_project(db, name, [video_id])
    return project_id, video_id, dict(project_videos(db, project_id)[0])


def _segment(title: str) -> dict:
    return {
        "start_seconds": 0,
        "end_seconds": 5,
        "segment_type": "key_action",
        "title": title,
        "reason": title,
        "tags": ["coffee"],
        "score": 0.9,
        "suggested_use": "main",
    }


def test_second_active_run_for_same_video_is_rejected(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id, _video_id, video = _add_video(db, tmp_path, "a.mp4")
    cfg = _cfg(tmp_path)

    first = create_perception_run(db, cfg, project_id, video)
    with pytest.raises(RuntimeError, match="already has an active perception run"):
        create_perception_run(db, cfg, project_id, video)
    mark_perception_run_terminal(db, first["run_uuid"], "failed", "cleanup")


def test_rollback_does_not_delete_other_video_migrations_created_after_snapshot(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_a, video_a, row_a = _add_video(db, tmp_path, "a.mp4")
    _project_b, video_b, _row_b = _add_video(db, tmp_path, "b.mp4")
    cfg = _cfg(tmp_path)

    run_a = create_perception_run(db, cfg, project_a, row_a)
    snapshot_a = capture_live_results(db, video_a)

    replace_segments(db, video_b, [_segment("B migration")])
    with connect(db) as con:
        before = con.execute(
            "select count(*) as count from segment_identity_migrations where video_id=?",
            (video_b,),
        ).fetchone()["count"]
    assert int(before) == 1

    publish_staged_results(
        db,
        run_a["run_uuid"],
        [
            {
                "frame_path": str(tmp_path / "a-frame.jpg"),
                "timestamp_seconds": 0,
                "summary": "A temporary",
                "tags": ["coffee"],
                "visual_quality_score": 0.8,
                "usefulness_score": 0.9,
            }
        ],
        [_segment("A temporary")],
    )
    restore_live_results(db, snapshot_a, run_a["run_uuid"], "failed", "rollback")

    with connect(db) as con:
        after = con.execute(
            "select count(*) as count from segment_identity_migrations where video_id=?",
            (video_b,),
        ).fetchone()["count"]
    assert int(after) == 1
