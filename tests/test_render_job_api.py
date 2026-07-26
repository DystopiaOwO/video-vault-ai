from pathlib import Path
import json
import threading
import time

import video_vault.render_job_manager as manager_module
from video_vault.project import project_dir
from video_vault.render_job_api import RenderJobAPI
from video_vault.render_job_manager import RenderJobManager


def _setup(tmp_path: Path, monkeypatch):
    folder = project_dir({"library_root": str(tmp_path)}, 1)
    manifest = {"project_id": 1, "manifest_hash": "a" * 64, "segments": []}
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(manager_module, "can_project_render", lambda *args: (True, "approved"))
    manager = RenderJobManager({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3")
    return manager


def test_api_create_get_list_cancel(monkeypatch, tmp_path: Path):
    manager = _setup(tmp_path, monkeypatch)
    started = threading.Event()

    def blocking_render(*args, execution=None, **kwargs):
        started.set()
        while True:
            execution.check_cancelled()
            time.sleep(0.01)

    monkeypatch.setattr(manager_module, "render_project", blocking_render)
    api = RenderJobAPI(manager)
    created = api.create(1, str(tmp_path / "output.mp4"))
    assert created["ok"] is True
    job_id = created["job"]["job_id"]
    assert started.wait(timeout=5)
    assert api.get(job_id)["job"]["requested_output_path"] == str(tmp_path / "output.mp4")
    assert api.list(1)["jobs"][0]["job_id"] == job_id
    cancelled = api.cancel(job_id)
    assert cancelled["ok"] is True
    assert cancelled["job"]["status"] in {"cancelled", "cancelling"}
    manager.shutdown()


def test_api_rejects_approval_failure_without_creating_job(monkeypatch, tmp_path: Path):
    manager = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_module, "can_project_render", lambda *args: (False, "尚未核准"))
    result = RenderJobAPI(manager).create(1)
    assert result == {"created": False, "ok": False, "error": "尚未核准"}
    assert manager.list(1) == []
    manager.shutdown()
