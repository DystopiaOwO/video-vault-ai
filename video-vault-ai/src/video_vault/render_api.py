"""Project-scoped Render Pipeline v2 API operations.

This module is deliberately independent from the HTTP server.  The WebUI and
future CLI callers use these functions so approval and preflight rules cannot
be bypassed by a different transport.
"""
from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .database import init_db, project
from .project import assert_project_approved, invalidate_project_approval, project_dir
from .render_manifest import compile_manifest, manifest_to_dict
from .render_profiles import get_render_profile, validate_render_profile
from .render_types import RenderKind
from .render_jobs import RenderJobStore
from .render_types import RenderJobStatus, RenderStage, to_dict


class RenderApiError(Exception):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.details, self.status = code, message, dict(details or {}), status

    def as_response(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "details": self.details}}


def settings_path(cfg: Mapping[str, Any], project_id: int) -> Path:
    return project_dir(dict(cfg), int(project_id)) / "render_settings.json"


def render_settings(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    path = settings_path(cfg, project_id)
    if not path.exists():
        return {"profile": "preview_1080p30", "encoder": "h264_nvenc", "transition": "cut",
                "overlay_enabled": False, "audio_role": "keep_original", "audio_crossfade_ms": 80,
                "bgm": {}, "color": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderApiError("invalid_render_settings", f"render_settings.json 無法讀取：{exc}") from exc
    return value if isinstance(value, dict) else {}


def update_render_settings(cfg: Mapping[str, Any], db: Path, project_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
    current = render_settings(cfg, project_id)
    merged = _deep_merge(current, dict(changes))
    profile = str(merged.get("profile") or "preview_1080p30")
    try:
        validate_render_profile(profile)
    except ValueError as exc:
        raise RenderApiError("invalid_profile", str(exc), details={"profile": profile}) from exc
    try:
        crossfade = int(merged.get("audio_crossfade_ms", 80))
    except (TypeError, ValueError) as exc:
        raise RenderApiError("invalid_audio_crossfade", "audio_crossfade_ms 必須是非負整數") from exc
    if crossfade < 0:
        raise RenderApiError("invalid_audio_crossfade", "audio_crossfade_ms 必須是非負整數")
    merged["audio_crossfade_ms"] = crossfade
    path = settings_path(cfg, project_id)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    invalidate_project_approval(dict(cfg), db, int(project_id), "render settings changed")
    return merged


def get_render_settings(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    """JSON response shape for GET /api/project/render/settings."""

    init_db(db)
    if not project(db, int(project_id)):
        return error_response("project_not_found", f"找不到專案：{project_id}", 404)
    return {"ok": True, "project_id": int(project_id), "settings": render_settings(cfg, project_id)}


def save_render_settings(cfg: Mapping[str, Any], db: Path, project_id: int, settings: Mapping[str, Any]) -> dict[str, Any]:
    try:
        stored = update_render_settings(cfg, db, project_id, settings)
    except RenderApiError as exc:
        return exc.as_response()
    return {"ok": True, "project_id": int(project_id), "settings": stored, "approval_invalidated": True}


def compile_project(cfg: Mapping[str, Any], db: Path, project_id: int, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    init_db(db)
    folder = project_dir(dict(cfg), int(project_id))
    plan_path = folder / "project_plan.json"
    if not plan_path.exists():
        raise RenderApiError("missing_plan", "缺少 project_plan.json")
    settings = render_settings(cfg, project_id)
    if overrides:
        settings = _deep_merge(settings, dict(overrides))
    try:
        review_path = folder / "segment_review.json"
        manifest = compile_manifest(plan_path, review_path if review_path.exists() else None,
                                    settings)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RenderApiError("manifest_compile_failed", str(exc)) from exc
    output = folder / "render" / "manifest" / "render_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest_to_dict(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "manifest": manifest_to_dict(manifest), "manifest_hash": manifest.manifest_hash, "path": str(output)}


def preflight_project(cfg: Mapping[str, Any], db: Path, project_id: int, *, final: bool = False,
                     overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if final:
        try:
            assert_project_approved(dict(cfg), db, int(project_id), action="final render")
        except PermissionError as exc:
            raise RenderApiError("approval_required", str(exc), status=403) from exc
    compiled = compile_project(cfg, db, project_id, overrides)
    manifest = compiled["manifest"]
    errors: list[str] = []
    warnings: list[str] = []
    profile = manifest.get("profile", "")
    try:
        get_render_profile(profile)
    except ValueError as exc:
        errors.append(str(exc))
    for key, label in (("ffmpeg_path", "FFmpeg"), ("ffprobe_path", "FFprobe")):
        executable = str(cfg.get(key, key))
        if not Path(executable).exists() and shutil.which(executable) is None:
            errors.append(f"找不到 {label}：{executable}")
    for segment in manifest.get("segments", []):
        source = Path(segment.get("source_file", ""))
        if not source.exists():
            errors.append(f"來源不存在：{source}")
        start, end = int(segment.get("source_in_ms", 0)), int(segment.get("source_out_ms", 0))
        if start < 0 or end <= start:
            errors.append(f"時間範圍無效：{segment.get('segment_id', '')}")
        if not 0.25 <= float(segment.get("speed", 1)) <= 4.0:
            errors.append(f"速度必須介於 0.25～4.0：{segment.get('segment_id', '')}")
        if segment.get("source_duration_ms") is not None and end > int(segment["source_duration_ms"]):
            errors.append(f"時間超過來源長度：{segment.get('segment_id', '')}")
        if segment.get("source_duration_ms") is None:
            warnings.append(f"尚未取得來源 Probe：{segment.get('segment_id', '')}")
    bgm = manifest.get("bgm") or {}
    if bgm.get("enabled") and bgm.get("source_file") and not Path(bgm["source_file"]).exists():
        errors.append(f"BGM 不存在：{bgm['source_file']}")
    color = manifest.get("color") or {}
    if color.get("lut_path") and not Path(color["lut_path"]).exists():
        errors.append(f"LUT 不存在：{color['lut_path']}")
    output_dir = project_dir(dict(cfg), int(project_id)) / "render" / "outputs"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".write_test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError as exc:
        errors.append(f"輸出目錄不可寫入：{exc}")
    result = {"ok": not errors, "errors": errors, "warnings": warnings, "manifest_hash": compiled["manifest_hash"],
              "manifest": manifest, "path": compiled["path"]}
    if errors:
        result["error"] = {"code": "preflight_failed", "message": "Preflight 檢查失敗", "details": errors}
    return result


def compile_project_manifest(cfg: Mapping[str, Any], db: Path, project_id: int,
                             settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        return compile_project(cfg, db, project_id, settings)
    except RenderApiError as exc:
        return exc.as_response()


def start_project_render(cfg: Mapping[str, Any], db: Path, project_id: int, kind: str,
                         *, runner: Any = None) -> dict[str, Any]:
    try:
        from .job_api import start_render_job
        result = start_render_job(cfg, db, project_id, kind, {"kind": kind})
        return result
    except (RenderApiError, ValueError) as exc:
        return error_response("render_start_failed", str(exc), getattr(exc, "status", 400))


def cancel_project_render(cfg: Mapping[str, Any], project_id: int, job_id: str) -> dict[str, Any]:
    from .job_api import cancel_render_job
    try:
        return cancel_render_job(cfg, project_id, job_id)
    except RenderApiError as exc:
        return exc.as_response()


def job_store(cfg: Mapping[str, Any]) -> RenderJobStore:
    return RenderJobStore(Path(str(cfg["library_root"])) / "08_projects" / ".render_jobs")


def _run_job(store: RenderJobStore, job_id: str, manifest: dict[str, Any], runner: Any) -> None:
    job = store.update_job(job_id, status=RenderJobStatus.RUNNING, stage=RenderStage.PREFLIGHT, percent=5.0,
                           started_at=_now())
    if job is None:
        return
    try:
        if runner is None:
            raise RuntimeError("Render Engine 尚未接入；目前只完成 manifest/preflight/job API")
        runner(manifest, job)
        store.update_job(job_id, status=RenderJobStatus.COMPLETED, stage=RenderStage.QUALITY_CHECK,
                         percent=100.0, finished_at=_now())
    except Exception as exc:
        store.update_job(job_id, status=RenderJobStatus.FAILED, error=str(exc), finished_at=_now())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def error_response(code: str, message: str, status: int = 400) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code, "message": message}}


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


__all__ = ["RenderApiError", "cancel_project_render", "compile_project", "compile_project_manifest",
           "error_response", "get_render_settings", "job_store", "preflight_project", "render_settings",
           "save_render_settings", "settings_path", "start_project_render", "update_render_settings"]
