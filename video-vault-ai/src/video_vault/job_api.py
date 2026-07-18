"""HTTP-independent asynchronous Render v2 job operations."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

from .project import project_dir
from .render_api import RenderApiError, compile_project, preflight_project
from .render_jobs import RenderJobStore
from .render_types import (
    BgmSettings,
    ColorSettings,
    RenderJobStatus,
    RenderKind,
    RenderManifest,
    RenderSegment,
    RenderSettings,
    RenderStage,
    to_dict,
)


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
                     settings: Mapping[str, Any] | None = None, *, runner: Any = None) -> dict[str, Any]:
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
    thread = threading.Thread(target=_run_job, args=(cfg, store, job.job_id, result, runner), daemon=True,
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


def _run_job(cfg: Mapping[str, Any], store: RenderJobStore, job_id: str,
             result: Mapping[str, Any], runner: Any = None) -> None:
    store.update_job(job_id, status=RenderJobStatus.RUNNING, stage=RenderStage.PREFLIGHT,
                     started_at=store.clock(), percent=5.0)
    try:
        manifest = _manifest_from_dict(result["manifest"])
        job = store.get_job(job_id)
        if job is None:
            return
        if runner is not None:
            runner(manifest, job)
            store.update_job(job_id, status=RenderJobStatus.COMPLETED,
                             stage=RenderStage.QUALITY_CHECK, percent=100.0,
                             finished_at=store.clock())
            return

        from .render_engine import RenderEngine
        from .segment_renderer import render_segment

        engine_cfg = dict(cfg)
        engine_cfg["render_root"] = str(project_dir(dict(cfg), int(job.project_id)) / "render")
        cache_dir = Path(engine_cfg["render_root"]) / "cache" / "segments"

        def segment_runner(manifest_value: RenderManifest, item: Any, _cache_path: Path) -> Path:
            segment = next((value for value in manifest_value.segments if value.segment_id == item.segment_id), None)
            if segment is None:
                raise ValueError(f"manifest segment not found: {item.segment_id}")
            rendered = render_segment(
                segment,
                engine_cfg,
                cache_dir=cache_dir,
                profile=manifest_value.profile,
                color=manifest_value.color,
                encoder=manifest_value.settings.encoder,
            )
            return rendered.path

        engine = RenderEngine(engine_cfg, store, segment_renderer=segment_runner)
        engine.render(manifest, job_id)
    except Exception as exc:
        current = store.get_job(job_id)
        if current and current.status not in {
            RenderJobStatus.COMPLETED,
            RenderJobStatus.FAILED,
            RenderJobStatus.FAILED_QC,
            RenderJobStatus.CANCELLED,
        }:
            store.update_job(job_id, status=RenderJobStatus.FAILED,
                             stage=RenderStage.QUALITY_CHECK, percent=100.0,
                             finished_at=store.clock(), error=str(exc))


def _manifest_from_dict(data: Mapping[str, Any]) -> RenderManifest:
    """Restore the shared dataclass contract after the API JSON boundary."""

    settings_data = dict(data.get("settings") or {})
    settings = RenderSettings(
        kind=RenderKind(settings_data.get("kind", data.get("render_kind", RenderKind.ROUGH_PREVIEW.value))),
        profile=str(settings_data.get("profile", data.get("profile", "preview_1080p30"))),
        encoder=str(settings_data.get("encoder", "")),
        transition=str(settings_data.get("transition", "cut")),
        overlay_enabled=bool(settings_data.get("overlay_enabled", False)),
        audio_role=str(settings_data.get("audio_role", "keep_original")),
        audio_crossfade_ms=int(settings_data.get("audio_crossfade_ms", 80)),
        bgm=BgmSettings(**_contract_values(dict(settings_data.get("bgm") or data.get("bgm") or {}), BgmSettings)),
        color=ColorSettings(**_contract_values(dict(settings_data.get("color") or data.get("color") or {}), ColorSettings)),
    )
    segments = [RenderSegment(**_segment_values(item)) for item in data.get("segments", [])]
    return RenderManifest(
        schema_version=str(data.get("schema_version", "1.0")),
        manifest_hash=str(data.get("manifest_hash", "")),
        plan_id=str(data.get("plan_id", "")),
        project_id=str(data.get("project_id", "")),
        render_kind=RenderKind(data.get("render_kind", settings.kind.value)),
        profile=str(data.get("profile", settings.profile)),
        settings=settings,
        segments=segments,
        timeline_duration_ms=int(data.get("timeline_duration_ms", 0)),
        bgm=BgmSettings(**_contract_values(dict(data.get("bgm") or {}), BgmSettings)),
        color=ColorSettings(**_contract_values(dict(data.get("color") or {}), ColorSettings)),
        overlays=list(data.get("overlays") or []),
        created_at=str(data.get("created_at", "")),
    )


def _contract_values(data: Mapping[str, Any], contract: type) -> dict[str, Any]:
    defaults = contract()
    return {field: data.get(field, getattr(defaults, field)) for field in defaults.__dataclass_fields__}


def _segment_values(data: Mapping[str, Any]) -> dict[str, Any]:
    defaults = RenderSegment("", "", 0, 0)
    return {field: data.get(field, getattr(defaults, field)) for field in defaults.__dataclass_fields__}


__all__ = ["cancel_render_job", "get_project_job", "get_project_outputs", "get_render_job", "job_store",
           "list_project_jobs", "list_render_jobs", "list_render_outputs", "start_render_job"]
