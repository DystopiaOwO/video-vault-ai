"""Safe Render Report DTO and HTTP-neutral API facade.

The legacy ``render_job_api`` shape remains available.  This module adds a
stable report contract that can be used by HTTP, CLI, or WebUI adapters
without returning the persisted report's local filesystem paths.
"""

from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .project import project_dir
from .render_job_api import RenderJobAPI as _LegacyRenderJobAPI
from .render_job_models import RenderReportDTO


_PATH_KEYS = frozenset({
    "canonical_path", "file_path", "frame_path", "log_path", "output_path",
    "path", "report_path", "source_file", "source_path", "temp_path",
})
_ABSOLUTE_PATH = re.compile(r"(?:^[a-zA-Z]:[\\/]|^[\\/]{1,2}|\\\\)")


class RenderReportAPI:
    """Read and sanitize a final report for transport to an API client."""

    def __init__(self, manager: Any):
        self.manager = manager

    def report(self, job_id: str) -> dict[str, Any]:
        job = self.manager.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        report_path = _report_path(job)
        if report_path is None or not report_path.is_file():
            return {"ok": False, "error": "Render Report 尚未產生"}
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "error": "Render Report 無法讀取"}
        if not isinstance(report, dict):
            return {"ok": False, "error": "Render Report 格式無效"}
        currentity = report_currentity(self.manager, job, report)
        return {"ok": True, "report": build_render_report_dto(report, currentity=currentity).to_dict()}

    get_report = report


class RenderAPI(_LegacyRenderJobAPI, RenderReportAPI):
    """Backward-compatible job API with the safe report operation added."""

    def __init__(self, manager: Any):
        _LegacyRenderJobAPI.__init__(self, manager)

    def report(self, job_id: str) -> dict[str, Any]:
        return RenderReportAPI.report(self, job_id)

    get_report = report


# New callers may use the v2 name while old callers keep importing the legacy
# facade from render_job_api.
RenderJobReportAPI = RenderReportAPI


def build_render_report_dto(report: Mapping[str, Any], *, currentity: str) -> RenderReportDTO:
    """Project the internal report into a path-safe, stable DTO."""
    qc = _mapping(report.get("qc"))
    loudness = _mapping(report.get("loudness"))
    final_loudness = _mapping(loudness.get("final"))
    measurements = _mapping(report.get("measurements"))
    effective = _mapping(report.get("effective"))
    color = _mapping(report.get("color"))
    bgm = _mapping(report.get("bgm"))
    cache = _mapping(report.get("cache"))
    profile = _mapping(report.get("profile"))
    if not profile and report.get("profile_id"):
        profile = {"profile_id": report.get("profile_id")}

    asset_mismatches = report.get("asset_mismatches", report.get("asset_mismatch"))
    if isinstance(asset_mismatches, Mapping):
        asset_mismatches = [asset_mismatches]
    if not isinstance(asset_mismatches, list):
        asset_mismatches = []
    hard_failures = report.get("hard_failures", report.get("failures"))
    if hard_failures is None:
        hard_failures = qc.get("errors") or []
    warnings = report.get("warnings")
    if warnings is None:
        warnings = qc.get("warnings") or []

    color_source = report.get("color_effective_source")
    if color_source is None:
        color_source = color.get("effective_source", color.get("source"))
    if color_source is None:
        color_source = _mapping(effective.get("color")).get("source")

    bgm_source = report.get("bgm_source")
    if bgm_source is None:
        bgm_source = {key: bgm.get(key) for key in (
            "source", "source_type", "track_id", "title", "artist", "license_name", "source_url"
        ) if bgm.get(key) is not None}
    bgm_migration = report.get("bgm_migration")
    if bgm_migration is None:
        bgm_migration = _mapping(report.get("audio")).get("migration")
    if bgm_migration is None:
        bgm_migration = _mapping(effective.get("audio")).get("migration")

    lufs = report.get("lufs")
    if lufs is None:
        lufs = final_loudness.get("integrated_lufs", final_loudness.get("lufs", loudness.get("integrated_lufs")))
    true_peak = report.get("true_peak")
    if true_peak is None:
        true_peak = final_loudness.get("true_peak_dbtp", final_loudness.get("true_peak", loudness.get("true_peak_dbtp")))

    frame = _mapping(report.get("frame")) or _mapping(measurements.get("frame"))
    decode = _mapping(report.get("decode")) or _mapping(measurements.get("decode"))
    output = {
        "filename": _basename(report.get("output_path") or report.get("output_file")),
        "size": report.get("output_size"),
        "sha256": report.get("output_sha256"),
        "duration_seconds": report.get("duration_seconds"),
    }
    snapshot = _mapping(report.get("approval_snapshot"))
    encoder_contract = _mapping(report.get("encoder_contract"))
    encoder_probe_audit = _mapping(report.get("encoder_probe_audit")) or _mapping(encoder_contract.get("nvenc_probe"))
    return RenderReportDTO(
        status=currentity,
        project_id=_int_or_none(report.get("project_id")),
        manifest_hash=str(report.get("manifest_hash") or ""),
        approval_snapshot=_safe({key: snapshot.get(key) for key in (
            "snapshot_id", "snapshot_hash", "schema_version", "approved_project_revision"
        ) if key in snapshot}),
        profile=_safe(profile),
        profile_id=str(report.get("profile_id") or profile.get("profile_id") or ""),
        encoder_contract=_safe(encoder_contract),
        encoder_probe_audit=_safe(encoder_probe_audit),
        gpu_execution_contract_version=str(report.get("gpu_execution_contract_version") or ""),
        gpu_execution_requested=str(report.get("gpu_execution_requested") or ""),
        gpu_execution_segments=_safe(list(report.get("gpu_execution_segments") or [])),
        loudness=_safe(loudness),
        lufs=lufs,
        true_peak=true_peak,
        color=_safe(color),
        color_effective_source=_safe(color_source),
        timing=_safe(_mapping(report.get("timing")) or _mapping(measurements.get("timing"))),
        frame=_safe(frame),
        decode=_safe(decode),
        measurements=_safe(measurements),
        asset_mismatches=_safe(asset_mismatches),
        bgm=_safe(bgm),
        bgm_migration=_safe(bgm_migration),
        bgm_source=_safe(bgm_source),
        hard_failures=_safe(list(hard_failures or [])),
        warnings=_safe(list(warnings or [])),
        cache=_safe(cache),
        cache_miss_reason=_safe(report.get("cache_miss_reason", cache.get("miss_reason"))),
        output=_safe(output),
        segment_count=_int_or_none(report.get("segment_count")) or 0,
        created_at=str(report.get("created_at")) if report.get("created_at") is not None else None,
    )


def report_currentity(manager: Any, job: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    """Classify a report against the project's current approval pointer."""
    snapshot = _mapping(report.get("approval_snapshot"))
    report_id = str(snapshot.get("snapshot_id") or "")
    report_hash = str(snapshot.get("snapshot_hash") or "")
    job_id = str(job.get("approval_snapshot_id") or "")
    job_hash = str(job.get("approval_snapshot_hash") or "")
    if not report_id or not report_hash:
        return "stale"
    if (job_id and job_id != report_id) or (job_hash and job_hash != report_hash):
        return "stale"
    try:
        folder = project_dir(manager.cfg, int(job.get("project_id") or 0))
        review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "stale"
    if (str(review.get("approval_snapshot_id") or "") == report_id
            and str(review.get("approval_snapshot_hash") or "") == report_hash
            and str(review.get("status") or "") == "approved"
            and review.get("approved_by_user") is True):
        return "current"
    return "historical"


def _report_path(job: Mapping[str, Any]) -> Path | None:
    value = str(job.get("output_path") or job.get("requested_output_path") or "").strip()
    if not value:
        return None
    return Path(value).expanduser().with_name(Path(value).name + ".render.json")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {str(item_key): _safe(item, str(item_key)) for item_key, item in value.items() if str(item_key).lower() not in _PATH_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe(item, key) for item in value]
    if isinstance(value, Path):
        return _basename(value)
    if isinstance(value, str):
        if key.lower() in _PATH_KEYS:
            return _basename(value)
        if _ABSOLUTE_PATH.search(value):
            return "<redacted-path>"
    return value


def _basename(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return re.split(r"[\\/]", raw.rstrip("\\/"))[-1]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "RenderAPI", "RenderJobReportAPI", "RenderReportAPI", "RenderReportDTO",
    "build_render_report_dto", "report_currentity",
]
