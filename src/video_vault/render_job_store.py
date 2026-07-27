"""Atomic, process-local persistence for render jobs."""

from __future__ import annotations

from pathlib import Path
import json
import threading
from typing import Any, Iterable

from .render_job_models import ACTIVE_JOB_STATUSES, RenderJob, utc_now, validate_job_fields


_STORE_LOCK = threading.RLock()


class RenderJobStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.projects_root = Path(str(cfg.get("library_root") or ".")).expanduser().resolve() / "08_projects"

    def jobs_dir(self, project_id: int) -> Path:
        return self.projects_root / f"project_{int(project_id)}" / "renders" / "jobs"

    def job_path(self, job_id: str) -> Path | None:
        job_id = str(job_id)
        if not job_id or any(char not in "0123456789abcdef" for char in job_id.lower()):
            return None
        with _STORE_LOCK:
            for path in self.projects_root.glob(f"project_*/renders/jobs/{job_id}.json"):
                return path
        return None

    def log_path(self, job_id: str) -> Path | None:
        path = self.job_path(job_id)
        return path.with_suffix(".log") if path else None

    def create(
        self,
        *,
        project_id: int,
        manifest_hash: str,
        approved_manifest_hash: str,
        approval_snapshot_id: str = "",
        approval_snapshot_hash: str = "",
        approval_snapshot: dict[str, Any] | None = None,
        encoder_contract: dict[str, Any] | None = None,
        requested_output_path: str = "",
        segment_count: int = 0,
        base_revision: int = 0,
        capabilities: list[str] | None = None,
        resources: list[str] | None = None,
        queue_reason: str = "",
        generation: int = 1,
    ) -> dict[str, Any]:
        with _STORE_LOCK:
            directory = self.jobs_dir(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            job = RenderJob.create(
                project_id=project_id,
                manifest_hash=manifest_hash,
                approved_manifest_hash=approved_manifest_hash,
                approval_snapshot_id=approval_snapshot_id,
                approval_snapshot_hash=approval_snapshot_hash,
                approval_snapshot=approval_snapshot,
                encoder_contract=encoder_contract,
                requested_output_path=requested_output_path,
                segment_count=segment_count,
                log_path=str((directory / "PLACEHOLDER.log").resolve()),
                base_revision=base_revision,
                capabilities=capabilities,
                resources=resources,
                queue_reason=queue_reason,
                generation=generation,
            )
            log_path = directory / f"{job.job_id}.log"
            job.log_path = str(log_path.resolve())
            self._write_json(directory / f"{job.job_id}.json", job.to_dict())
            log_path.write_text(
                f"job_id: {job.job_id}\nproject_id: {job.project_id}\nmanifest_hash: {job.manifest_hash}\napproval_snapshot_id: {job.approval_snapshot_id}\nencoder: {(job.encoder_contract or {}).get('implementation', '')}\ncreated_at: {job.created_at}\n",
                encoding="utf-8",
            )
            return job.to_dict()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with _STORE_LOCK:
            path = self.job_path(job_id)
            return self._read_json(path) if path else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with _STORE_LOCK:
            path = self.job_path(job_id)
            if path is None:
                raise KeyError(f"render job not found: {job_id}")
            current = self._read_json(path)
            if current is None:
                raise ValueError(f"render job JSON is invalid: {job_id}")
            if "percent" in changes:
                requested = max(0.0, min(100.0, float(changes["percent"])))
                changes["percent"] = max(float(current.get("percent", 0)), requested)
            current.update(changes)
            current["updated_at"] = utc_now()
            validate_job_fields(current)
            self._write_json(path, current)
            return current

    def transition(
        self,
        job_id: str,
        expected_statuses: set[str] | frozenset[str],
        **changes: Any,
    ) -> dict[str, Any] | None:
        """Conditionally update a job without releasing the store lock."""
        with _STORE_LOCK:
            path = self.job_path(job_id)
            if path is None:
                return None
            current = self._read_json(path)
            if current is None or current.get("status") not in expected_statuses:
                return None
            if "percent" in changes:
                requested = max(0.0, min(100.0, float(changes["percent"])))
                changes["percent"] = max(float(current.get("percent", 0)), requested)
            current.update(changes)
            current["updated_at"] = utc_now()
            validate_job_fields(current)
            self._write_json(path, current)
            return current

    def list(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with _STORE_LOCK:
            if not self.projects_root.exists():
                return []
            paths: Iterable[Path]
            if project_id is None:
                paths = self.projects_root.glob("project_*/renders/jobs/*.json")
            else:
                paths = self.jobs_dir(project_id).glob("*.json")
            jobs = [job for path in paths if not path.name.startswith(".") and (job := self._read_json(path)) is not None]
            return sorted(jobs, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def mark_stale_jobs_interrupted(self, active_job_ids: set[str] | None = None) -> list[dict[str, Any]]:
        active_job_ids = active_job_ids or set()
        changed: list[dict[str, Any]] = []
        with _STORE_LOCK:
            for job in self.list():
                if job.get("job_id") in active_job_ids or job.get("status") not in ACTIVE_JOB_STATUSES:
                    continue
                path = self.job_path(str(job["job_id"]))
                if path is None:
                    continue
                job.update(
                    status="interrupted",
                    stage="done",
                    message="程式重新啟動，前次正式輸出已中斷",
                    process_id=None,
                    finished_at=utc_now(),
                    updated_at=utc_now(),
                )
                validate_job_fields(job)
                self._write_json(path, job)
                changed.append(job)
        return changed

    def append_log(self, job_id: str, message: str) -> None:
        with _STORE_LOCK:
            path = self.log_path(job_id)
            if path is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(str(message).rstrip() + "\n")

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            validate_job_fields(data)
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)


__all__ = ["RenderJobStore"]
