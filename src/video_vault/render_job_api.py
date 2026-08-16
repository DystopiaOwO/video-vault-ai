"""HTTP-neutral facade for persistent render jobs."""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .render_job_manager import RenderJobManager
from .project import project_dir


class RenderJobAPI:
    def __init__(self, manager: RenderJobManager):
        self.manager = manager

    def create(self, project_id: int, output_path: str = "") -> dict[str, Any]:
        return self.manager.enqueue(int(project_id), Path(output_path) if output_path else None)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.manager.get(job_id)
        return {"ok": bool(job), "job": job} if job else {"ok": False, "error": "job not found"}

    def list(self, project_id: int | None = None) -> dict[str, Any]:
        return {"ok": True, "jobs": self.manager.list(project_id)}

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.manager.cancel(job_id)

    def report(self, job_id: str) -> dict[str, Any]:
        """Return a safe, path-redacted Final Render Report summary."""
        job = self.manager.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        output = Path(str(job.get("output_path") or "")).expanduser()
        report_path = output.with_name(output.name + ".render.json") if output else None
        if report_path is None or not report_path.is_file():
            return {"ok": False, "error": "Render Report 尚未產生", "job": job}
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Render Report 無法讀取：{exc}"}
        if not isinstance(report, dict):
            return {"ok": False, "error": "Render Report 格式無效"}
        current = self._report_currentity(job, report)
        return {"ok": True, "report": _safe_report_summary(report, currentity=current)}

    def _report_currentity(self, job: dict[str, Any], report: dict[str, Any]) -> str:
        snapshot = report.get("approval_snapshot") if isinstance(report.get("approval_snapshot"), dict) else {}
        report_id = str(snapshot.get("snapshot_id") or job.get("approval_snapshot_id") or "")
        report_hash = str(snapshot.get("snapshot_hash") or job.get("approval_snapshot_hash") or "")
        if not report_id and not report_hash:
            return "stale"
        try:
            folder = project_dir(self.manager.cfg, int(job.get("project_id") or 0))
            review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return "stale"
        if (str(review.get("approval_snapshot_id") or "") == report_id
                and str(review.get("approval_snapshot_hash") or "") == report_hash
                and str(review.get("status") or "") == "approved"
                and review.get("approved_by_user") is True):
            return "current"
        return "historical"


__all__ = ["RenderJobAPI"]


def _safe_report_summary(report: dict[str, Any], *, currentity: str) -> dict[str, Any]:
    """Keep the report useful without exposing absolute Windows paths."""
    snapshot = report.get("approval_snapshot") if isinstance(report.get("approval_snapshot"), dict) else {}
    qc = report.get("qc") if isinstance(report.get("qc"), dict) else {}
    bgm = report.get("bgm") if isinstance(report.get("bgm"), dict) else {}
    safe_bgm = {key: value for key, value in bgm.items() if key not in {"source_path", "fingerprint"}}
    return {
        "status": currentity,
        "project_id": report.get("project_id"),
        "manifest_hash": report.get("manifest_hash"),
        "profile_id": report.get("profile_id"),
        "approval_snapshot": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "snapshot_hash": snapshot.get("snapshot_hash"),
            "schema_version": snapshot.get("schema_version"),
            "approved_project_revision": snapshot.get("approved_project_revision"),
        },
        "encoder_contract": report.get("encoder_contract") or {},
        "encoder_probe_audit": report.get("encoder_probe_audit") or ((report.get("encoder_contract") or {}).get("nvenc_probe") if isinstance(report.get("encoder_contract"), dict) else {}) or {},
        "loudness": report.get("loudness") or {},
        "color": report.get("color") or {},
        "timing": report.get("timing") or {},
        "measurements": report.get("measurements") or {},
        "bgm": safe_bgm,
        "output": {
            "filename": Path(str(report.get("output_path") or "")).name,
            "size": report.get("output_size"),
            "sha256": report.get("output_sha256"),
            "duration_seconds": report.get("duration_seconds"),
        },
        "segment_count": report.get("segment_count", 0),
        "qc": {
            "passed": bool(qc.get("passed")),
            "errors": list(qc.get("errors") or []),
            "warnings": list(qc.get("warnings") or []),
        },
        "cache": report.get("cache") or {},
        "probe_audit": report.get("probe_audit") or {},
        "created_at": report.get("created_at"),
    }
