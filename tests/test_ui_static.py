from pathlib import Path

from video_vault import ui
from video_vault.ui import JOBS, JOBS_LOCK, _kill_video_vault_processes, _set_job, _static_file, _web_dist, cancel_legacy_job, project_jobs, stop_project_jobs


def test_audio_ui_exposes_force_preview_and_reset_override_controls():
    source = (Path(__file__).parents[1] / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    assert "強制重新產生" in source
    assert "強制重跑" in source
    assert "resetSegment" in source
    assert "使用專案預設" in source


def test_static_file_serving_stays_inside_web_dist():
    dist = _web_dist()
    assert _static_file("/index.html") == dist / "index.html" if (dist / "index.html").exists() else True
    assert _static_file("/../config.yaml") is None


def test_project_jobs_tracks_percent_and_stop(monkeypatch):
    monkeypatch.setattr(ui, "_kill_video_vault_processes", lambda: None)
    with JOBS_LOCK:
        JOBS.clear()
    _set_job(7, "render", kind="輸出", status="running", done=1, total=4)

    job = project_jobs(7)[0]
    assert job["percent"] == 25

    stop_project_jobs(7)
    assert project_jobs(7)[0]["status"] == "stopped"


def test_cancel_legacy_job_is_scoped_to_one_key(monkeypatch):
    with JOBS_LOCK:
        JOBS.clear()
    _set_job(7, "analyze", kind="內容感知", status="running", done=1, total=2)
    _set_job(7, "color", kind="調色預覽", status="running", done=1, total=2)

    class GuardManager:
        def list(self, project_id):
            return [{"job_id": "formal-1", "status": "running", "project_id": 7}]

        def cancel_project(self, project_id):
            raise AssertionError("legacy cancellation must not cancel persistent Render Jobs")

    result = cancel_legacy_job(7, "analyze")

    assert result["ok"] is True
    assert result["job"]["legacy_job_key"] == "analyze"
    jobs = project_jobs(7, GuardManager())
    legacy_jobs = {job["legacy_job_key"]: job for job in jobs if "legacy_job_key" in job}
    assert legacy_jobs["analyze"]["status"] == "stopped"
    assert legacy_jobs["color"]["status"] == "running"
    assert next(job for job in jobs if job.get("job_id") == "formal-1")["status"] == "running"


def test_stop_processes_does_not_use_global_process_kill(monkeypatch):
    monkeypatch.setattr(ui.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("global process kill is forbidden")))
    _kill_video_vault_processes()
