"""Shared project-operation coordinator.

Legacy perception jobs and formal render jobs are still persisted by their
existing stores, but admission now has one explicit capability/resource
contract.  The coordinator is intentionally independent from any UI so CLI,
HTTP and workers can use the same compatibility rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Iterable
from uuid import uuid4


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


CAPABILITIES = frozenset(
    {
        "project_state_writer",
        "approval_snapshot_reader",
        "source_analysis_writer",
        "ffmpeg_heavy",
        "gpu_heavy",
        "external_handoff",
        "read_only_snapshot",
    }
)

TERMINAL_STATES = frozenset({JobState.CANCELLED, JobState.SUCCEEDED, JobState.FAILED, JobState.SUPERSEDED})

# Two jobs can coexist only when neither writes project state and they do not
# contend for the same finite heavy resource.
INCOMPATIBLE_CAPABILITIES = {
    ("project_state_writer", "project_state_writer"),
    ("source_analysis_writer", "project_state_writer"),
    ("project_state_writer", "source_analysis_writer"),
    ("source_analysis_writer", "source_analysis_writer"),
    ("ffmpeg_heavy", "ffmpeg_heavy"),
    ("gpu_heavy", "gpu_heavy"),
    ("approval_snapshot_reader", "project_state_writer"),
    ("project_state_writer", "approval_snapshot_reader"),
    ("approval_snapshot_reader", "source_analysis_writer"),
    ("source_analysis_writer", "approval_snapshot_reader"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def capabilities_compatible(left: Iterable[str], right: Iterable[str]) -> bool:
    left_set = set(left)
    right_set = set(right)
    return not any((a, b) in INCOMPATIBLE_CAPABILITIES for a in left_set for b in right_set)


@dataclass
class CoordinatedJob:
    project_id: int
    kind: str
    capabilities: frozenset[str]
    base_revision: int
    resources: frozenset[str] = frozenset()
    job_id: str = field(default_factory=lambda: str(uuid4()))
    generation: int = 1
    state: JobState = JobState.QUEUED
    queue_reason: str = ""
    cancellation_mode: str = "cooperative"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    percent: float = 0.0
    error: str = ""
    result: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "generation": self.generation,
            "project_id": self.project_id,
            "kind": self.kind,
            "capabilities": sorted(self.capabilities),
            "resources": sorted(self.resources),
            "base_revision": self.base_revision,
            "state": self.state.value,
            "status": self.state.value,
            "queue_reason": self.queue_reason,
            "cancellation_mode": self.cancellation_mode,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "percent": self.percent,
            "error": self.error,
            "result": self.result,
        }


class JobCoordinator:
    """Per-process admission controller with FIFO queue and resource slots."""

    def __init__(self, *, ffmpeg_slots: int = 1, gpu_slots: int = 1, ai_provider_slots: int = 1):
        self.capacities = {
            "ffmpeg_heavy": max(1, int(ffmpeg_slots)),
            "gpu_heavy": max(1, int(gpu_slots)),
            "ai_provider": max(1, int(ai_provider_slots)),
        }
        self._jobs: dict[str, CoordinatedJob] = {}
        self._lock = threading.RLock()

    def submit(self, project_id: int, kind: str, capabilities: Iterable[str], base_revision: int, *, resources: Iterable[str] = (), cancellation_mode: str = "cooperative") -> CoordinatedJob:
        caps = frozenset(str(item) for item in capabilities)
        unknown = caps - CAPABILITIES
        if unknown:
            raise ValueError(f"未知工作 capability：{', '.join(sorted(unknown))}")
        job = CoordinatedJob(int(project_id), str(kind), caps, int(base_revision), frozenset(str(item) for item in resources), cancellation_mode=cancellation_mode)
        with self._lock:
            self._jobs[job.job_id] = job
            self._admit_locked(job)
        return job

    def get(self, job_id: str) -> CoordinatedJob | None:
        with self._lock:
            return self._jobs.get(str(job_id))

    def list(self, project_id: int | None = None) -> list[CoordinatedJob]:
        with self._lock:
            values = list(self._jobs.values())
            if project_id is not None:
                values = [job for job in values if job.project_id == int(project_id)]
            return sorted(values, key=lambda job: job.created_at)

    def start(self, job_id: str) -> CoordinatedJob:
        with self._lock:
            job = self._jobs[str(job_id)]
            if job.state == JobState.QUEUED:
                self._admit_locked(job)
            if job.state == JobState.RUNNING:
                return job
            raise RuntimeError(job.queue_reason or "工作仍在佇列中")

    def complete(self, job_id: str, *, result: dict | None = None) -> CoordinatedJob:
        return self._terminal(job_id, JobState.SUCCEEDED, result=result)

    def fail(self, job_id: str, error: str) -> CoordinatedJob:
        return self._terminal(job_id, JobState.FAILED, error=error)

    def supersede(self, job_id: str, reason: str = "") -> CoordinatedJob:
        return self._terminal(job_id, JobState.SUPERSEDED, error=reason)

    def cancel(self, job_id: str) -> CoordinatedJob:
        with self._lock:
            job = self._jobs[str(job_id)]
            if job.state == JobState.QUEUED:
                job.state = JobState.CANCELLED
                job.finished_at = utc_now()
                self._admit_waiting_locked()
            elif job.state == JobState.RUNNING:
                job.state = JobState.CANCELLING
            return job

    def is_current(self, job_id: str, generation: int) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            return bool(job and job.generation == int(generation) and job.state not in TERMINAL_STATES)

    def publish_allowed(self, job_id: str, generation: int, current_revision: int) -> tuple[bool, str]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job or job.generation != int(generation):
                return False, "工作 generation 已過期"
            if job.state in {JobState.CANCELLING, JobState.CANCELLED}:
                return False, "工作已取消"
            if job.state in TERMINAL_STATES:
                return False, f"工作已結束：{job.state.value}"
            if int(current_revision) != job.base_revision:
                job.state = JobState.SUPERSEDED
                job.finished_at = utc_now()
                return False, "專案 revision 已更新"
            return True, "ok"

    def _admit_locked(self, job: CoordinatedJob) -> None:
        if job.state != JobState.QUEUED:
            return
        active = [item for item in self._jobs.values() if item.project_id == job.project_id and item.state == JobState.RUNNING]
        if any(not capabilities_compatible(job.capabilities, item.capabilities) for item in active):
            job.queue_reason = "等待同專案不相容工作完成"
            return
        for resource in job.resources:
            capacity = self.capacities.get(resource, 1)
            used = sum(resource in item.resources for item in self._jobs.values() if item.state == JobState.RUNNING)
            if used >= capacity:
                job.queue_reason = f"等待 {resource} 資源 slot"
                return
        job.state = JobState.RUNNING
        job.queue_reason = ""
        job.started_at = job.started_at or utc_now()

    def _admit_waiting_locked(self) -> None:
        for job in self._jobs.values():
            if job.state == JobState.QUEUED:
                self._admit_locked(job)

    def _terminal(self, job_id: str, state: JobState, *, result: dict | None = None, error: str = "") -> CoordinatedJob:
        with self._lock:
            job = self._jobs[str(job_id)]
            if job.state == JobState.CANCELLED:
                return job
            job.state = state
            job.finished_at = utc_now()
            job.percent = 100.0 if state == JobState.SUCCEEDED else job.percent
            job.result = result or {}
            job.error = error
            self._admit_waiting_locked()
            return job


DEFAULT_COORDINATOR = JobCoordinator(ffmpeg_slots=1, gpu_slots=1, ai_provider_slots=1)


__all__ = ["CAPABILITIES", "CoordinatedJob", "DEFAULT_COORDINATOR", "INCOMPATIBLE_CAPABILITIES", "JobCoordinator", "JobState", "TERMINAL_STATES", "capabilities_compatible"]
