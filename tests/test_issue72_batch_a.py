from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_vault.database import add_bgm_track, connect, init_db, project_videos, upsert_video
from video_vault.perception_runs import (
    analysis_run,
    create_perception_run,
    finalize_perception_run,
    publish_staged_results,
    run_staging_dir,
)
from video_vault.project import build_project_plan, create_project, project_dir
from video_vault.project_perception import ProjectMediaOwnershipError, run_project_perception
from video_vault.project_media import ensure_project_media_ownership, rollback_project_media_ownership
from video_vault.duration_budget import apply_duration_budget
from video_vault.visual_timeline import build_visual_timeline, validate_visual_timeline
from video_vault.bgm import license_contract


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


def test_duration_budget_keeps_group_coverage_and_is_deterministic():
    def groups():
        return [
            {"label": "早上", "order": 1, "segments": [
                {"segment_id": "a", "clip_id": "clip_001", "start_seconds": 0, "end_seconds": 8, "score": 0.8},
            ]},
            {"label": "下午", "order": 2, "segments": [
                {"segment_id": "b", "clip_id": "clip_002", "start_seconds": 0, "end_seconds": 8, "score": 0.7},
            ]},
        ]

    first = groups()
    second = groups()
    result_a = apply_duration_budget(first, 10)
    result_b = apply_duration_budget(second, 10)
    assert result_a == result_b
    assert {segment["segment_id"] for group in first for segment in group["segments"] if segment["include"]} == {"a", "b"}
    assert all(group["covered"] for group in result_a["groups"])
    assert result_a["estimated_seconds"] == 16


def test_visual_timeline_is_versioned_and_manifest_ready():
    timeline = build_visual_timeline([
        {"label": "南港車站", "segments": [{"include": True, "estimated_output_seconds": 4}]},
    ])
    assert timeline["items"][0]["type"] == "chapter_card"
    assert validate_visual_timeline(timeline)["valid"] is True


def test_bgm_license_contract_distinguishes_verified_required_and_unknown(tmp_path):
    assert license_contract({"license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/", "attribution_required": True})["attribution_status"] == "required"
    assert license_contract({"license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/", "attribution_required": True})["license_status"] == "verified"
    assert license_contract({"license_name": "", "source_url": ""}) == {
        "attribution_status": "unknown", "license_status": "unverified", "license_verified_at": "",
    }


def test_legacy_zero_attribution_without_evidence_is_unknown(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    add_bgm_track(db, {"title": "Unclear", "file_path": str(tmp_path / "unclear.mp3"), "license_name": "", "source_url": "", "attribution_required": 0})
    with connect(db) as con:
        row = con.execute("select attribution_status, license_status from bgm_tracks").fetchone()
    assert dict(row) == {"attribution_status": "unknown", "license_status": "unverified"}


def test_shared_project_perception_fails_closed_before_creating_a_run(tmp_path):
    db, cfg, project_id, video_id = _fixture(tmp_path)
    create_project(db, "Other project", [video_id])
    before = [dict(row) for row in project_videos(db, project_id)]

    with pytest.raises(ProjectMediaOwnershipError, match="多個專案共用"):
        run_project_perception(cfg, db, project_id, before[0])

    assert [dict(row) for row in project_videos(db, project_id)] == before
    with connect(db) as con:
        assert con.execute("select count(*) from analysis_runs").fetchone()[0] == 0


def test_failed_publish_restores_project_revision_and_status(tmp_path):
    db, cfg, project_id, _video_id = _fixture(tmp_path)
    video = dict(project_videos(db, project_id)[0])
    run = create_perception_run(db, cfg, project_id, video)
    from video_vault.perception_runs import capture_live_results, restore_live_results

    snapshot = capture_live_results(db, video["id"])
    with connect(db) as con:
        con.execute("update projects set status='approved', project_revision=99 where id=?", (project_id,))
    restore_live_results(db, snapshot, run["run_uuid"], "failed", "publish failed")
    with connect(db) as con:
        row = con.execute("select status, project_revision from projects where id=?", (project_id,)).fetchone()
    assert dict(row) == {"status": "needs_review", "project_revision": 1}


def test_target_duration_uses_reviewed_speed_and_trim(tmp_path):
    db, cfg, project_id, video_id = _fixture(tmp_path)
    from video_vault.database import replace_segments

    replace_segments(db, video_id, [{
        "start_seconds": 0, "end_seconds": 8, "segment_type": "scene",
        "title": "scene", "reason": "test", "tags": ["travel"],
        "score": 0.9, "suggested_use": "main",
    }])
    build_project_plan(cfg, db, project_id)
    initial_plan = json.loads((project_dir(cfg, project_id) / "project_plan.json").read_text(encoding="utf-8"))
    segment_id = initial_plan["groups"][0]["segments"][0]["segment_id"]
    review_path = project_dir(cfg, project_id) / "feedback" / "segment_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps([{
        "segment_id": segment_id, "start_seconds": 1, "end_seconds": 5, "speed": 2.0,
    }]), encoding="utf-8")
    plan = build_project_plan(cfg, db, project_id)
    segment = plan["groups"][0]["segments"][0]
    assert segment["start_seconds"] == 1
    assert segment["end_seconds"] == 5
    assert segment["speed"] == 2.0
    assert segment["estimated_output_seconds"] == 2.0
