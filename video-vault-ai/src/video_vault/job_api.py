"""HTTP-independent asynchronous Render v2 job operations."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

from .project import project_dir
from .render_api import RenderApiError, compile_project, preflight_project
from .render_jobs import RenderJobStore
from .render_types import RenderJobStatus, RenderKind, RenderStage, to_dict


_MANAGERS: dict[str, RenderJobStore] = {}
_MANAGERS_LOCK = threading.Lock()


def job_store(cfg: Mapping[str, Any], project_id: int) -> RenderJobStore:
    root = project_dir(dict(cfg), int(project_id)) / "render" / "jobs"
    key = str(root.resolve())
    with _MANAGERS_LOCK:
        return _MANAGERS.setdefault(key, RenderJobStore(root))


def list_render_jobs(cfg: Mapping[str, Any], project_id: int) -> list[dict[str, Any]]:
    return [to_dict(job) for job in job_store(cfg, project_id).list_jobs(str(project_id))]


def get_render_job(cfg: Mapping[str, Any], project_id: int, job_id: str) -> dict[str, Any] | None:
    job = job_store(cfg, project_id).get_job(job_id)
    if job is None or job.project_id != str(project_id):
        return None
    return to_dict(job)


def start_render_job(cfg: Mapping[str, Any], db: Path, project_id: int, kind: RenderKind | str,
                     settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    kind = RenderKind(kind)
    effective_settings = dict(settings or {})
    effective_settings["kind"] = kind.value
    if kind is RenderKind.FINAL:
        result = preflight_project(cfg, db, project_id, final=True, overrides=effective_settings)
        if not result["ok"]:
            raise RenderApiError("preflight_failed", "正式輸出前置檢查失敗", details=result, status=422)
        encoder = str((result["manifest"].get("settings") or {}).get("encoder", ""))
        total = len(result["manifest"].get("segments", []))
    else:
        result = preflight_project(cfg, db, project_id, final=False, overrides=effective_settings)
        if not result["ok"]:
            raise RenderApiError("preflight_failed", "預覽前置檢查失敗", details=result, status=422)
        encoder = str((result["manifest"].get("settings") or {}).get("encoder", ""))
        total = len(result["manifest"].get("segments", []))
    store = job_store(cfg, project_id)
    job = store.create_job(str(project_id), kind, encoder=encoder, total_segments=total)
    thread = threading.Thread(target=_run_job, args=(store, job.job_id, result), daemon=True,
                              name=f"render-{job.job_id[:8]}")
    thread.start()
    return {"ok": True, "job_id": job.job_id, "manifest_hash": result["manifest_hash"], "message": "工作已排入佇列"}


def cancel_render_job(cfg: Mapping[str, Any], project_id: int, job_id: str) -> dict[str, Any]:
    store = job_store(cfg, project_id)
    job = store.get_job(job_id)
    if job is None or job.project_id != str(project_id):
        raise RenderApiError("job_not_found", "找不到指定 Render Job", status=404)
    if job.status in {RenderJobStatus.COMPLETED, RenderJobStatus.FAILED, RenderJobStatus.FAILED_QC, RenderJobStatus.CANCELLED}:
        return {"ok": True, "job": to_dict(job), "message": "工作已經結束"}
    updated = store.update_job(job_id, status=RenderJobStatus.CANCELLED, stage=RenderStage.QUALITY_CHECK,
                               error="由使用者取消", finished_at=store.clock())
    return {"ok": True, "job": to_dict(updated) if updated else None, "message": "已取消工作"}


def list_render_outputs(cfg: Mapping[str, Any], project_id: int) -> list[dict[str, Any]]:
    output_dir = project_dir(dict(cfg), int(project_id)) / "render" / "outputs"
    if not output_dir.exists():
        return []
    return [{"name": path.name, "path": str(path), "size": path.stat().st_size}
            for path in sorted(output_dir.iterdir()) if path.is_file() and not path.name.startswith(".")]


def list_project_jobs(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    return {"ok": True, "project_id": int(project_id), "jobs": list_render_jobs(cfg, project_id)}


def get_project_job(cfg: Mapping[str, Any], project_id: int, job_id: str) -> dict[str, Any]:
    job = get_render_job(cfg, project_id, job_id)
    if job is None:
        return {"ok": False, "error": {"code": "job_not_found", "message": "找不到指定 Render Job"}}
    return {"ok": True, "job": job}


def get_project_outputs(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    return {"ok": True, "project_id": int(project_id), "outputs": list_render_outputs(cfg, project_id)}


def _run_job(store: RenderJobStore, job_id: str, result: Mapping[str, Any]) -> None:
    store.update_job(job_id, status=RenderJobStatus.RUNNING, stage=RenderStage.PREFLIGHT,
                     started_at=store.clock(), percent=5.0)
    # Agent C supplies the actual executor.  Keeping this explicit makes an
    # incomplete engine visible to the client instead of claiming Completed.
    store.update_job(job_id, status=RenderJobStatus.FAILED, stage=RenderStage.QUALITY_CHECK,
                     percent=100.0, finished_at=store.clock(),
                     error="Render Engine 尚未接入；目前只完成 Manifest 與 Preflight")


__all__ = ["cancel_render_job", "get_project_job", "get_project_outputs", "get_render_job", "job_store",
           "list_project_jobs", "list_render_jobs", "list_render_outputs", "start_render_job"]
