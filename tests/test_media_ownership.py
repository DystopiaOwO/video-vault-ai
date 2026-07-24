from __future__ import annotations

from datetime import datetime
from pathlib import Path

from video_vault.database import (
    add_frame,
    connect,
    frames,
    init_db,
    project,
    project_videos,
    replace_segments,
    set_project_status,
    set_project_videos,
    update_project_media_summary,
    update_video_summary,
    upsert_video,
    videos,
)
from video_vault.naming import rename_after_perception
from video_vault.project import build_project_plan, create_project, project_detail
from video_vault.segment_state_migration import migrate_segment_state_for_video


def _video(db: Path, path: Path) -> int:
    path.write_bytes(b"source")
    return upsert_video(
        db,
        {
            "original_path": str(path),
            "current_path": str(path),
            "filename": path.name,
            "category": "unknown",
            "duration_seconds": 30,
            "status": "uploaded",
        },
    )


def _frame(db: Path, video_id: int, root: Path, *, tags: str = "coffee", summary: str = "global summary") -> None:
    frame = root / f"frame_{video_id}.jpg"
    frame.write_bytes(b"frame")
    add_frame(db, video_id, frame, 0)
    with connect(db) as con:
        con.execute(
            "update frames set tags=?, vision_summary=? where video_id=?",
            (tags, summary, video_id),
        )


def _segment(start: float, end: float) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "segment_type": "key_action",
        "title": "pour",
        "reason": "useful",
        "tags": ["coffee", "hands"],
        "score": 0.9,
        "suggested_use": "main",
    }


def test_project_naming_is_local_and_shared_source_is_immutable(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "clip.mp4"
    video_id = _video(db, source)
    _frame(db, video_id, tmp_path)
    project_a = create_project(db, "A", [video_id])
    project_b = create_project(db, "B", [video_id])
    before_global = dict(videos(db)[0])
    a_before = dict(project_videos(db, project_a)[0])
    b_before = dict(project_videos(db, project_b)[0])

    assert a_before["project_media_uuid"] != b_before["project_media_uuid"]
    monkeypatch.setattr(
        "video_vault.naming.recording_time",
        lambda path, cfg: datetime(2026, 7, 24, 8, 15, 0),
    )
    renamed = rename_after_perception(
        {"ffprobe_path": "ffprobe"},
        db,
        a_before,
    )

    after_global = dict(videos(db)[0])
    a_after = dict(project_videos(db, project_a)[0])
    b_after = dict(project_videos(db, project_b)[0])
    expected = "20260724_081500_coffee_clip.mp4"

    assert source.is_file()
    assert renamed["filename"] == expected
    assert after_global["current_path"] == before_global["current_path"] == str(source)
    assert after_global["filename"] == before_global["filename"] == "clip.mp4"
    assert after_global["category"] == before_global["category"] == "unknown"
    assert a_after["filename"] == expected
    assert a_after["category"] == "coffee"
    assert a_after["status"] == "perceived"
    assert a_after["perception_revision"] == 1
    assert b_after["filename"] == "clip.mp4"
    assert b_after["category"] == "unknown"
    assert b_after["status"] == "uploaded"
    assert b_after["perception_revision"] == 0
    assert b_after["current_path"] == str(source)

    cfg = {"library_root": str(tmp_path)}
    assert project_detail(cfg, db, project_a)["clips"][0]["filename"] == expected
    assert project_detail(cfg, db, project_b)["clips"][0]["filename"] == "clip.mp4"


def test_summary_edit_is_project_local_and_legacy_shared_write_fails_closed(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "clip.mp4")
    _frame(db, video_id, tmp_path, summary="global summary")
    project_a = create_project(db, "A", [video_id])
    project_b = create_project(db, "B", [video_id])

    assert update_video_summary(db, video_id, "unsafe legacy edit") is False
    assert str(frames(db, video_id)[0]["vision_summary"]) == "global summary"

    assert update_project_media_summary(db, project_a, video_id, "A-only summary") is True
    a = dict(project_videos(db, project_a)[0])
    b = dict(project_videos(db, project_b)[0])
    assert a["project_summary"] == "A-only summary"
    assert b["project_summary"] == "global summary"
    assert str(frames(db, video_id)[0]["vision_summary"]) == "global summary"

    cfg = {"library_root": str(tmp_path)}
    assert project_detail(cfg, db, project_a)["clips"][0]["visual_summary"] == "A-only summary"
    assert project_detail(cfg, db, project_b)["clips"][0]["visual_summary"] == "global summary"

    assert update_video_summary(db, video_id, "A second edit", project_id=project_a) is True
    assert dict(project_videos(db, project_a)[0])["project_summary"] == "A second edit"
    assert dict(project_videos(db, project_b)[0])["project_summary"] == "global summary"
    assert project_detail(cfg, db, project_a)["clips"][0]["visual_summary"] == "A second edit"
    assert project_detail(cfg, db, project_b)["clips"][0]["visual_summary"] == "global summary"


def test_reanalysis_of_shared_effective_input_invalidates_every_linked_project(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = _video(db, tmp_path / "clip.mp4")
    _frame(db, video_id, tmp_path)
    replace_segments(db, video_id, [_segment(0, 5)])
    project_a = create_project(db, "A", [video_id])
    project_b = create_project(db, "B", [video_id])
    cfg = {"library_root": str(tmp_path)}
    build_project_plan(cfg, db, project_a)
    build_project_plan(cfg, db, project_b)
    set_project_status(db, project_a, "approved")
    set_project_status(db, project_b, "approved")

    report = replace_segments(db, video_id, [_segment(0.05, 5.05)])
    migrations = migrate_segment_state_for_video(cfg, db, video_id, report)

    assert {item["project_id"] for item in migrations} == {project_a, project_b}
    assert dict(project(db, project_a))["status"] == "needs_review"
    assert dict(project(db, project_b))["status"] == "needs_review"


def test_removing_one_project_does_not_delete_shared_asset_or_other_project_state(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "clip.mp4"
    video_id = _video(db, source)
    project_a = create_project(db, "A", [video_id])
    project_b = create_project(db, "B", [video_id])
    b_uuid = str(project_videos(db, project_b)[0]["project_media_uuid"])

    with connect(db) as con:
        con.execute("delete from project_videos where project_id=?", (project_a,))
        con.execute("delete from projects where id=?", (project_a,))

    assert source.is_file()
    assert len(videos(db)) == 1
    assert [int(row["id"]) for row in project_videos(db, project_b)] == [video_id]
    assert str(project_videos(db, project_b)[0]["project_media_uuid"]) == b_uuid


def test_reordering_preserves_project_local_metadata(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    first = _video(db, tmp_path / "first.mp4")
    second = _video(db, tmp_path / "second.mp4")
    project_id = create_project(db, "A", [first, second])
    assert update_project_media_summary(db, project_id, first, "first summary")
    first_uuid = str(project_videos(db, project_id)[0]["project_media_uuid"])

    set_project_videos(db, project_id, [second, first])
    rows = [dict(row) for row in project_videos(db, project_id)]

    assert [int(row["id"]) for row in rows] == [second, first]
    assert rows[1]["project_media_uuid"] == first_uuid
    assert rows[1]["project_summary"] == "first summary"
