from __future__ import annotations

import time

from video_vault.database import init_db, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.job_api import get_render_job, start_render_job
from video_vault.render_types import RenderKind, RenderJobStatus


def test_render_job_dispatches_to_injected_runner(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "20260718_120000_food.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {
        "original_path": str(source),
        "current_path": str(source),
        "filename": source.name,
        "category": "food",
        "duration_seconds": 5,
    })
    project_id = create_project(db, "測試", [video_id], category="food")
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"}
    build_project_plan(cfg, db, project_id)
    called = []

    def runner(manifest, job):
        called.append((manifest.project_id, job.job_id))

    result = start_render_job(cfg, db, project_id, RenderKind.ROUGH_PREVIEW, runner=runner)
    deadline = time.time() + 5
    while time.time() < deadline:
        job = get_render_job(cfg, project_id, result["job_id"])
        if job and job["status"] == RenderJobStatus.COMPLETED.value:
            break
        time.sleep(0.02)

    job = get_render_job(cfg, project_id, result["job_id"])
    assert job is not None
    assert job["status"] == RenderJobStatus.COMPLETED.value
    assert called and called[0][0] == str(project_id)
