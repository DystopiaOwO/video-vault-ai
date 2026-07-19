"""HTTP-neutral facade for persistent render jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .render_job_manager import RenderJobManager


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


__all__ = ["RenderJobAPI"]
