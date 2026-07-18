from video_vault.render_jobs import RenderJobStore
from video_vault.render_types import RenderJobStatus, RenderKind, RenderStage


def test_job_store_persists_updates_and_recovers(tmp_path):
    store = RenderJobStore(tmp_path / "jobs")
    job = store.create_job("p1", RenderKind.FINAL, encoder="h264_nvenc", total_segments=2)
    assert store.get_job(job.job_id).kind is RenderKind.FINAL
    store.update_job(job.job_id, status=RenderJobStatus.RUNNING, stage=RenderStage.RENDER_SEGMENTS, percent=37.5, pid=2)
    recovered = store.recover_running_jobs()
    assert recovered[0].status is RenderJobStatus.FAILED
    assert store.get_job(job.job_id).pid is None


def test_corrupt_json_is_isolated(tmp_path):
    root = tmp_path / "jobs"; root.mkdir(); (root / "bad.json").write_text("{bad", encoding="utf-8")
    store = RenderJobStore(root); job = store.create_job("p1", RenderKind.ROUGH_PREVIEW)
    assert store.get_job("bad") is None and len(store.list_jobs()) == 1
