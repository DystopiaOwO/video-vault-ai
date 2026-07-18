from pathlib import Path

from video_vault import ui
from video_vault.ui import JOBS, JOBS_LOCK, _kill_video_vault_processes, _set_job, _static_file, _web_dist, project_jobs, stop_project_jobs


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


def test_stop_processes_kills_ffmpeg_by_name(monkeypatch):
    seen = {}
    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kwargs: seen.setdefault("cmd", cmd))

    _kill_video_vault_processes()

    assert "Stop-Process -Name ffmpeg" in seen["cmd"][-1]
