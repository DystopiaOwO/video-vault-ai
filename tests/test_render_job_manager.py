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


def test_queued_cancel_wins_before_worker_claim(manager: RenderJobManager, monkeypatch):
    worker_ready = threading.Event()
    release_worker = threading.Event()
    started: list[int] = []
    original_execute = manager._execute

    def gated_execute(job_id: str):
        worker_ready.set()
        assert release_worker.wait(timeout=5)
        return original_execute(job_id)

    monkeypatch.setattr(manager, "_execute", gated_execute)
    monkeypatch.setattr(manager_module, "render_project", lambda *args, **kwargs: started.append(args[2]))
    created = manager.enqueue(1)
    job_id = created["job"]["job_id"]
    assert worker_ready.wait(timeout=5)
    cancelled = manager.cancel(job_id)
    assert cancelled["ok"] is True
    assert cancelled["job"]["status"] == "cancelled"
    release_worker.set()
    _wait_for(manager, job_id, {"cancelled"})
    assert started == []
    assert job_id not in manager._active


def test_worker_claim_wins_and_running_job_has_runtime(manager: RenderJobManager, monkeypatch):
    claim_barrier = threading.Barrier(2)
    cancel_started = threading.Event()
    release_render = threading.Event()
    original_transition = manager.store.transition

    def synchronized_transition(job_id, expected_statuses, **changes):
        if expected_statuses == {"queued"} and changes.get("status") == "running":
            claim_barrier.wait(timeout=5)
        return original_transition(job_id, expected_statuses, **changes)

    monkeypatch.setattr(manager.store, "transition", synchronized_transition)

    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        while not release_render.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return SimpleNamespace(output_path=Path("out.mp4"), cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    created = manager.enqueue(1)
    job_id = created["job"]["job_id"]

    cancel_result: dict = {}

    def cancel_later():
        cancel_started.set()
        cancel_result.update(manager.cancel(job_id))

    cancel_thread = threading.Thread(target=cancel_later)
    cancel_thread.start()
    assert cancel_started.wait(timeout=5)
    claim_barrier.wait(timeout=5)
    cancel_thread.join(timeout=5)
    assert not cancel_thread.is_alive()
    assert cancel_result["ok"] is True
    assert cancel_result["job"]["status"] in {"cancelling", "cancelled"}
    assert job_id in manager._active or manager.get(job_id)["status"] == "cancelled"
    if job_id in manager._active:
        assert manager._active[job_id].cancel_event.is_set()
    release_render.set()
    final = _wait_for(manager, job_id, {"cancelled"})
    assert final["status"] == "cancelled"


def test_shutdown_timeout_keeps_worker_and_blocks_second_start(manager: RenderJobManager, monkeypatch):
    release_render = threading.Event()
    monkeypatch.setattr(manager_module, "_SHUTDOWN_TIMEOUT_SECONDS", 0.05)

    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        while not release_render.is_set():
            time.sleep(0.01)
        return SimpleNamespace(output_path=Path("out.mp4"), cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    created = manager.enqueue(1)
    job_id = created["job"]["job_id"]
    _wait_for(manager, job_id, {"running"})
    old_worker = manager._worker
    assert old_worker is not None
    assert manager.shutdown(wait=True) is False
    assert manager._started is True
    assert manager._worker is old_worker
    manager.start()
    assert manager._worker is old_worker
    release_render.set()
    assert manager.shutdown(wait=True) is True
    assert manager._worker is None


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


def test_current_segment_is_persisted_before_segment_work(manager: RenderJobManager, monkeypatch):
    segment_visible = threading.Event()
    release = threading.Event()

    def fake_render(cfg, db, project_id, *, execution=None, **kwargs):
        execution.update(
            stage="segments",
            percent=5,
            message="正在輸出片段 1/2",
            current_segment_id="segment-001",
            current_segment_index=1,
            force=True,
        )
        segment_visible.set()
        release.wait(timeout=5)
        return SimpleNamespace(output_path=Path("out.mp4"), cache_hit=False)

    monkeypatch.setattr(manager_module, "render_project", fake_render)
    created = manager.enqueue(1)
    job_id = created["job"]["job_id"]
    assert segment_visible.wait(timeout=5)
    current = manager.get(job_id)
    assert current["current_segment_id"] == "segment-001"
    assert current["current_segment_index"] == 1
    release.set()
    _wait_for(manager, job_id, {"succeeded"})


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
