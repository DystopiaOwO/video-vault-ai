from video_vault.render_engine import RenderCancelled, RenderEngine
from video_vault.render_jobs import RenderJobStore
from video_vault.render_types import RenderJobStatus, RenderKind


def test_engine_cancelled_job_does_not_start_render(tmp_path):
    store = RenderJobStore(tmp_path / "jobs")
    job = store.create_job("p1", RenderKind.FINAL)
    store.update_job(job.job_id, status=RenderJobStatus.CANCELLED)
    engine = RenderEngine({"render_root": str(tmp_path / "render")}, store)
    try:
        engine.render(object(), job.job_id)
    except RenderCancelled:
        pass
    else:
        raise AssertionError("cancelled job should stop before rendering")
