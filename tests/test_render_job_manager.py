from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

import pytest

import video_vault.render_job_manager as manager_module
from video_vault.project import project_dir
from video_vault.render_job_manager import RenderJobManager


def _project(tmp_path: Path, project_id: int) -> dict:
    folder = project_dir({"library_root": str(tmp_path)}, project_id)
    manifest = {"project_id": project_id, "manifest_hash": f"{project_id:064x}", "segments": [{"segment_id": "a"}]}
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": manifest["manifest_hash"]}), encoding="utf-8")
    return manifest


def _wait_for(manager: RenderJobManager, job_id: str, statuses: set[str], timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.get("status") in statuses:
            return job
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} did not reach {statuses}: {manager.get(job_id)}")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    _project(tmp_path, 1)
    _project(tmp_path, 2)
    monkeypatch.setattr(manager_module, "can_project_render", lambda *args: (True, "approved"))
    instance = RenderJobManager({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3")
    yield instance
    instance.shutdown()


def test_same_project_is_deduplicated(manager: RenderJobManager):
    first = manager.enqueue(1)
    second = manager.enqueue(1)
    assert first["created"] is True
    assert second["created"] is False
    assert second["job"]["job_id"] == first["job"]["job_id"]


def test_queue_is_fifo_and_only_one_render_runs(manager: RenderJobManager, monkeypatch):
    release = threading.Event()
    started: list[int] = []

    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        started.append(project_id)
        execution.update(stage="segments", percent=20, message=f"正在輸出專案 {project_id}")
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.02)
        return SimpleNamespace(output_path=Path(f"project-{project_id}.mp4"), cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    first = manager.enqueue(1)
    _wait_for(manager, first["job"]["job_id"], {"running"})
    second = manager.enqueue(2)
    assert manager.get(second["job"]["job_id"])["status"] == "queued"
    assert started == [1]
    release.set()
    _wait_for(manager, first["job"]["job_id"], {"succeeded"})
    _wait_for(manager, second["job"]["job_id"], {"succeeded"})
    assert started == [1, 2]


def test_queued_cancel_does_not_start_worker(manager: RenderJobManager, monkeypatch):
    release = threading.Event()
    started: list[int] = []

    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        started.append(project_id)
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.02)
        return SimpleNamespace(output_path=Path("out.mp4"), cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    first = manager.enqueue(1)
    _wait_for(manager, first["job"]["job_id"], {"running"})
    second = manager.enqueue(2)
    cancelled = manager.cancel(second["job"]["job_id"])
    assert cancelled["ok"] is True
    assert manager.get(second["job"]["job_id"])["status"] == "cancelled"
    release.set()
    _wait_for(manager, first["job"]["job_id"], {"succeeded"})
    assert started == [1]


def test_running_cancel_is_idempotent_and_not_failed(manager: RenderJobManager, monkeypatch):
    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        while True:
            execution.check_cancelled()
            time.sleep(0.02)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    created = manager.enqueue(1)
    job_id = created["job"]["job_id"]
    _wait_for(manager, job_id, {"running"})
    assert manager.cancel(job_id)["ok"] is True
    assert manager.cancel(job_id)["ok"] is True
    final = _wait_for(manager, job_id, {"cancelled"})
    assert final["status"] == "cancelled"
    assert final["status"] != "failed"
    assert final["process_id"] is None


def test_failed_job_does_not_block_next_job(manager: RenderJobManager, monkeypatch):
    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        if project_id == 1:
            raise RuntimeError("first failed")
        return SimpleNamespace(output_path=Path("out.mp4"), cache_hit=True)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    first = manager.enqueue(1)
    second = manager.enqueue(2)
    assert _wait_for(manager, first["job"]["job_id"], {"failed"})["error"] == "first failed"
    assert _wait_for(manager, second["job"]["job_id"], {"succeeded"})["cache_hit"] is True
