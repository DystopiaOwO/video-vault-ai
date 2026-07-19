import json
import time
from pathlib import Path

from video_vault.render_job_store import RenderJobStore


def test_store_create_update_is_atomic_and_percent_never_decreases(tmp_path: Path):
    store = RenderJobStore({"library_root": str(tmp_path)})
    first = store.create(project_id=1, manifest_hash="a", approved_manifest_hash="a", segment_count=2)
    job_id = first["job_id"]
    path = store.job_path(job_id)
    assert path and path.exists()
    assert not path.with_name(f".{path.name}.tmp").exists()
    updated = store.update(job_id, status="running", stage="segments", percent=35, message="片段 1/2")
    assert updated["percent"] == 35
    assert store.update(job_id, percent=10)["percent"] == 35


def test_transition_is_conditional_and_atomic(tmp_path: Path):
    store = RenderJobStore({"library_root": str(tmp_path)})
    job = store.create(project_id=1, manifest_hash="a", approved_manifest_hash="a")
    assert store.transition(job["job_id"], {"running"}, status="failed") is None
    assert store.get(job["job_id"])["status"] == "queued"
    changed = store.transition(job["job_id"], {"queued"}, status="running", stage="validating", process_id=None)
    assert changed and changed["status"] == "running"
    assert store.transition(job["job_id"], {"queued"}, status="cancelled") is None
    assert store.get(job["job_id"])["status"] == "running"


def test_store_list_filters_and_sorts_projects(tmp_path: Path):
    store = RenderJobStore({"library_root": str(tmp_path)})
    first = store.create(project_id=1, manifest_hash="a", approved_manifest_hash="a")
    time.sleep(0.01)
    second = store.create(project_id=2, manifest_hash="b", approved_manifest_hash="b")
    assert [job["job_id"] for job in store.list(1)] == [first["job_id"]]
    assert [job["job_id"] for job in store.list()] == [second["job_id"], first["job_id"]]


def test_corrupt_job_does_not_hide_other_jobs(tmp_path: Path):
    store = RenderJobStore({"library_root": str(tmp_path)})
    job = store.create(project_id=1, manifest_hash="a", approved_manifest_hash="a")
    corrupt = store.jobs_dir(1) / "corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    assert [item["job_id"] for item in store.list()] == [job["job_id"]]


def test_stale_active_jobs_become_interrupted(tmp_path: Path):
    store = RenderJobStore({"library_root": str(tmp_path)})
    job = store.create(project_id=1, manifest_hash="a", approved_manifest_hash="a")
    store.update(job["job_id"], status="running", stage="segments", process_id=123)
    changed = store.mark_stale_jobs_interrupted()
    restored = store.get(job["job_id"])
    assert len(changed) == 1
    assert restored["status"] == "interrupted"
    assert restored["stage"] == "done"
    assert restored["process_id"] is None
    assert restored["finished_at"]
    assert restored["message"] == "程式重新啟動，前次正式輸出已中斷"
