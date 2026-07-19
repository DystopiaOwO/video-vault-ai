from pathlib import Path
from types import SimpleNamespace
import json
import os
import shutil
import subprocess
import threading
import sys
import time

import pytest

from video_vault.database import add_analysis, connect, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_dir, set_review_status
from video_vault.render_job_manager import RenderJobManager


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def _make_source(path: Path, color: str, frequency: int, duration: float = 2.2):
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])


def _create_approved_project(tmp_path: Path) -> tuple[dict, Path]:
    library = tmp_path / "Render Job E2E Library"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    sources = [tmp_path / "red.mp4", tmp_path / "blue.mp4"]
    _make_source(sources[0], "red", 880)
    _make_source(sources[1], "blue", 660)
    video_ids = []
    for source in sources:
        video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 2.2})
        add_analysis(db, video_id, "mock", "render-job-e2e", {"segments": [{"start_seconds": 0, "end_seconds": 2.2, "segment_type": "key_action", "title": source.stem, "reason": "e2e", "tags": ["travel"], "score": 1, "suggested_use": "main"}]}, source.with_suffix(".raw.json"))
        video_ids.append(video_id)
    project_id = create_project(db, "Render Job E2E", video_ids, category="travel")
    build_project_plan(cfg, db, project_id)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    settings = {"profile_id": "final_1080p", "encoder": "cpu", "color": {"mode": "none", "lut_path": ""}, "audio": {"original_gain_db": 0, "lower_original_gain_db": -12, "bgm_gain_db": -18}, "transition": {"type": "cut", "duration_seconds": 0}, "overlay": {"enabled": False}}
    (project_dir(cfg, project_id) / "render_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    set_review_status(cfg, db, project_id, "approved")
    return {"cfg": cfg, "db": db, "project_id": project_id}, library


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_background_render_job_reports_real_stages_and_final_cache(tmp_path: Path):
    setup, library = _create_approved_project(tmp_path)
    manager = RenderJobManager(setup["cfg"], setup["db"])
    try:
        created = manager.enqueue(setup["project_id"])
        assert created["created"] is True
        job_id = created["job"]["job_id"]
        snapshots = []
        final = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = manager.get(job_id)
            if current:
                snapshots.append(current)
                if current["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    final = current
                    break
            time.sleep(0.05)
        assert final and final["status"] == "succeeded", final
        assert final["percent"] == 100
        stage_log = Path(final["log_path"]).read_text(encoding="utf-8")
        assert all(f"stage: {stage}" in stage_log for stage in ("segments", "assembling", "final_qc", "publishing"))
        assert {item["stage"] for item in snapshots} & {"segments", "assembling", "final_qc"}
        percents = [float(item["percent"]) for item in snapshots]
        assert percents == sorted(percents)
        output = Path(final["output_path"])
        assert output.exists()
        assert output.with_name(output.name + ".render.json").exists()
        assert Path(final["log_path"]).exists()
        assert (library / "08_projects" / f"project_{setup['project_id']}" / "renders" / "jobs" / f"{job_id}.json").exists()

        second = manager.enqueue(setup["project_id"])
        second_id = second["job"]["job_id"]
        second_final = _wait_for(manager, second_id, {"succeeded"}, timeout=30)
        assert second_final["cache_hit"] is True
        assert second_final["percent"] == 100
    finally:
        manager.shutdown()


def test_background_cancel_uses_real_process_and_preserves_source(tmp_path: Path, monkeypatch):
    import video_vault.render_job_manager as manager_module
    from video_vault.project import project_dir

    folder = project_dir({"library_root": str(tmp_path)}, 1)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original source")
    manifest = {"project_id": 1, "manifest_hash": "a" * 64, "segments": [{"segment_id": "a"}]}
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    child_pid_file = tmp_path / "child.pid"
    monkeypatch.setattr(manager_module, "can_project_render", lambda *args: (True, "approved"))

    def fake_render(cfg, db, project_id, *, runner=None, execution=None, **kwargs):
        code = f"import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(r'{child_pid_file}').write_text(str(p.pid)); print('out_time_us=0',flush=True); time.sleep(30)"
        runner.run([sys.executable, "-c", code], expected_duration_seconds=30)
        return SimpleNamespace(output_path=tmp_path / "never.mp4", cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    manager = RenderJobManager({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3")
    try:
        created = manager.enqueue(1)
        job_id = created["job"]["job_id"]
        running = _wait_for(manager, job_id, {"running"}, timeout=8)
        deadline = time.monotonic() + 5
        while manager.get(job_id).get("process_id") is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert manager.get(job_id).get("process_id")
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        assert manager.cancel(job_id)["ok"] is True
        final = _wait_for(manager, job_id, {"cancelled"}, timeout=15)
        assert final["process_id"] is None
        assert not _pid_exists(child_pid)
        assert source.read_bytes() == b"original source"
        assert not (tmp_path / "never.mp4").exists()
        assert Path(final["log_path"]).exists()
    finally:
        manager.shutdown()


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_cancel_after_publish_started_still_succeeds(tmp_path: Path, monkeypatch):
    setup, library = _create_approved_project(tmp_path)
    import video_vault.project_renderer as renderer_module

    publish_started = threading.Event()
    release_publish = threading.Event()
    original_publish = renderer_module.publish_final_render_atomically

    def gated_publish(partial, report_temp, output, report_path):
        publish_started.set()
        assert release_publish.wait(timeout=10)
        return original_publish(partial, report_temp, output, report_path)

    monkeypatch.setattr(renderer_module, "publish_final_render_atomically", gated_publish)
    manager = RenderJobManager(setup["cfg"], setup["db"])
    try:
        created = manager.enqueue(setup["project_id"])
        job_id = created["job"]["job_id"]
        assert publish_started.wait(timeout=90)
        cancelling = manager.cancel(job_id)
        assert cancelling["ok"] is True
        assert cancelling["job"]["status"] == "cancelling"
        release_publish.set()
        final = _wait_for(manager, job_id, {"succeeded", "failed", "cancelled"}, timeout=90)
        assert final["status"] == "succeeded"
        assert final["percent"] == 100
        output = Path(final["output_path"])
        report = output.with_name(output.name + ".render.json")
        assert output.exists()
        assert report.exists()
        assert not output.with_name(f"{output.stem}.partial.mp4").exists()
        assert not report.with_name(f".{report.name}.tmp").exists()
    finally:
        release_publish.set()
        manager.shutdown()


def _wait_for(manager: RenderJobManager, job_id: str, statuses: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.get("status") in statuses:
            return job
        time.sleep(0.03)
    raise AssertionError(f"job did not reach {statuses}: {manager.get(job_id)}")


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
