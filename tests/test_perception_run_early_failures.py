from __future__ import annotations

from pathlib import Path

import pytest

from video_vault.database import (
    add_frame,
    connect,
    frames,
    init_db,
    project_videos,
    replace_segments,
    segments,
    set_video_status,
    upsert_video,
)
from video_vault.perception_runs import analysis_run, create_perception_run, perception_states_for_project
from video_vault.project import create_project
from video_vault.project_perception import run_project_perception
from video_vault.sampling import SamplingError
import video_vault.project_perception as project_perception


def _cfg(root: Path) -> dict:
    return {
        "library_root": str(root),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "ai": {"provider": "mock", "model": "mock-v1"},
    }


def _setup(tmp_path: Path, *, with_old_results: bool = False) -> tuple[Path, int, int, dict, dict, Path]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
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
    if with_old_results:
        old_frame = tmp_path / "old-frame.jpg"
        old_frame.write_bytes(b"old")
        add_frame(db, video_id, old_frame, 0)
        with connect(db) as con:
            con.execute(
                """update frames
                set vision_summary='old summary', tags='coffee',
                    score_visual_quality=0.8, score_usefulness=0.9
                where video_id=?""",
                (video_id,),
            )
        replace_segments(
            db,
            video_id,
            [
                {
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "segment_type": "key_action",
                    "title": "old segment",
                    "reason": "old",
                    "tags": ["coffee"],
                    "score": 0.9,
                    "suggested_use": "main",
                }
            ],
        )
        set_video_status(db, video_id, "perceived")
    project_id = create_project(db, "A", [video_id])
    video = dict(project_videos(db, project_id)[0])
    return db, project_id, video_id, video, _cfg(tmp_path), source


def _latest_run_uuid(db: Path, video_id: int) -> str:
    with connect(db) as con:
        row = con.execute(
            "select run_uuid from analysis_runs where video_id=? order by id desc limit 1",
            (video_id,),
        ).fetchone()
    assert row is not None
    return str(row["run_uuid"])


def test_missing_source_still_records_failed_generation(tmp_path):
    db, project_id, video_id, video, cfg, source = _setup(tmp_path)
    source.unlink()

    with pytest.raises(FileNotFoundError):
        create_perception_run(db, cfg, project_id, video)

    run_uuid = _latest_run_uuid(db, video_id)
    run = analysis_run(db, run_uuid)
    state = perception_states_for_project(db, project_id)[video_id]
    assert run["status"] == "failed"
    assert run["finished_at"]
    assert run["error"]
    assert state["current_analysis_run_uuid"] == run_uuid
    assert state["current_status"] == "failed"
    assert state["analysis_current"] is False


def test_partial_extraction_failure_keeps_old_published_results(tmp_path, monkeypatch):
    db, project_id, video_id, video, cfg, _source = _setup(tmp_path, with_old_results=True)

    def partial_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "frame_00000.jpg").write_bytes(b"partial")
        raise RuntimeError("partial extraction")

    monkeypatch.setattr(project_perception, "extract_frames", partial_extract)

    with pytest.raises(RuntimeError, match="partial extraction"):
        run_project_perception(cfg, db, project_id, video)

    run_uuid = _latest_run_uuid(db, video_id)
    state = perception_states_for_project(db, project_id)[video_id]
    assert analysis_run(db, run_uuid)["status"] == "failed"
    assert frames(db, video_id)[0]["vision_summary"] == "old summary"
    assert segments(db, video_id)[0]["title"] == "old segment"
    assert state["current_status"] == "failed"
    assert state["analysis_current"] is False


def test_adaptive_prescan_failure_keeps_old_published_results(tmp_path, monkeypatch):
    db, project_id, video_id, video, cfg, _source = _setup(
        tmp_path, with_old_results=True
    )
    cfg["sampling"] = {"mode": "adaptive", "baseline_interval_seconds": 5}
    monkeypatch.setattr(
        project_perception,
        "build_sampling_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SamplingError("adaptive sampling prescan failed")
        ),
    )

    with pytest.raises(SamplingError, match="prescan failed"):
        run_project_perception(cfg, db, project_id, video)

    run_uuid = _latest_run_uuid(db, video_id)
    state = perception_states_for_project(db, project_id)[video_id]
    assert analysis_run(db, run_uuid)["status"] == "failed"
    assert frames(db, video_id)[0]["vision_summary"] == "old summary"
    assert segments(db, video_id)[0]["title"] == "old segment"
    assert state["current_status"] == "failed"
