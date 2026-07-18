"""Persistent Render Pipeline v2 job state."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .render_types import RenderJob, RenderJobStatus, RenderKind, RenderStage, to_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _enum(value: Any, enum_type: type):
    return value if isinstance(value, enum_type) else enum_type(value)


def job_from_dict(data: dict[str, Any]) -> RenderJob:
    return RenderJob(
        job_id=str(data["job_id"]), project_id=str(data["project_id"]),
        kind=_enum(data["kind"], RenderKind),
        status=_enum(data.get("status", "queued"), RenderJobStatus),
        stage=_enum(data.get("stage", "compile_manifest"), RenderStage),
        percent=float(data.get("percent", 0)), current_segment=data.get("current_segment"),
        total_segments=int(data.get("total_segments", 0)), pid=data.get("pid"),
        encoder=str(data.get("encoder", "")), output=data.get("output"), error=data.get("error"),
        created_at=str(data.get("created_at", "")), updated_at=str(data.get("updated_at", "")),
        started_at=data.get("started_at"), finished_at=data.get("finished_at"),
    )


class RenderJobStore:
    def __init__(self, root: str | Path, *, clock: Callable[[], str] = _now):
        self.root, self.clock = Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def save(self, job: RenderJob) -> RenderJob:
        job = replace(job, updated_at=self.clock())
        fd, temp = tempfile.mkstemp(prefix=f".{job.job_id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(to_dict(job), stream, ensure_ascii=False, indent=2)
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self._path(job.job_id))
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return job

    def create_job(self, project_id: str, kind: RenderKind, *, encoder: str = "",
                   total_segments: int = 0, output: str | None = None,
                   job_id: str | None = None) -> RenderJob:
        now = self.clock()
        return self.save(RenderJob(job_id or str(uuid4()), project_id, _enum(kind, RenderKind),
                                   encoder=encoder, total_segments=total_segments, output=output,
                                   created_at=now, updated_at=now))

    def get_job(self, job_id: str) -> RenderJob | None:
        try:
            with self._path(job_id).open("r", encoding="utf-8") as stream:
                return job_from_dict(json.load(stream))
        except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def update_job(self, job_id: str, **changes: Any) -> RenderJob | None:
        current = self.get_job(job_id)
        if current is None: return None
        allowed = set(RenderJob.__dataclass_fields__) - {"job_id", "created_at"}
        changes = {k: v for k, v in changes.items() if k in allowed}
        for key, enum_type in (("kind", RenderKind), ("status", RenderJobStatus), ("stage", RenderStage)):
            if key in changes: changes[key] = _enum(changes[key], enum_type)
        return self.save(replace(current, **changes))

    def list_jobs(self, project_id: str | None = None) -> list[RenderJob]:
        jobs = [self.get_job(p.stem) for p in sorted(self.root.glob("*.json"))]
        return sorted([j for j in jobs if j and (project_id is None or j.project_id == project_id)],
                      key=lambda j: (j.created_at, j.job_id))

    def recover_running_jobs(self) -> list[RenderJob]:
        result = []
        for job in self.list_jobs():
            if job.status is RenderJobStatus.RUNNING:
                updated = self.update_job(job.job_id, status=RenderJobStatus.FAILED, pid=None,
                                          finished_at=self.clock(),
                                          error="Render service restarted while this job was running")
                if updated: result.append(updated)
        return result


def create_job(store: RenderJobStore, project_id: str, kind: RenderKind, **kwargs: Any) -> RenderJob:
    return store.create_job(project_id, kind, **kwargs)


def get_job(store: RenderJobStore, job_id: str) -> RenderJob | None:
    return store.get_job(job_id)


def update_job(store: RenderJobStore, job_id: str, **changes: Any) -> RenderJob | None:
    return store.update_job(job_id, **changes)


def list_jobs(store: RenderJobStore, project_id: str | None = None) -> list[RenderJob]:
    return store.list_jobs(project_id)


__all__ = ["RenderJobStore", "create_job", "get_job", "job_from_dict", "list_jobs", "update_job"]
