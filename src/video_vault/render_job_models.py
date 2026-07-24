"""Persistent render-job data contracts for Phase 4B."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import uuid


JOB_STATUSES = frozenset({"queued", "running", "cancelling", "cancelled", "succeeded", "failed", "interrupted"})
JOB_STAGES = frozenset({"queued", "validating", "segments", "assembling", "final_qc", "publishing", "done"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancelling"})
FINISHED_JOB_STATUSES = frozenset({"cancelled", "succeeded", "failed", "interrupted"})


class RenderCancelled(RuntimeError):
    """Raised when the current render job was deliberately cancelled."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class RenderJob:
    job_id: str
    project_id: int
    manifest_hash: str
    approved_manifest_hash: str
    requested_output_path: str = ""
    status: str = "queued"
    stage: str = "queued"
    percent: float = 0.0
    message: str = "等待正式輸出"
    kind: str = "正式輸出"
    current_segment_id: str = ""
    current_segment_index: int = 0
    segment_count: int = 0
    cache_hit: bool = False
    output_path: str = ""
    error: str = ""
    cancel_requested: bool = False
    process_id: int | None = None
    log_path: str = ""
    created_at: str = ""
    started_at: str | None = None
    updated_at: str = ""
    finished_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        project_id: int,
        manifest_hash: str,
        approved_manifest_hash: str,
        requested_output_path: str = "",
        segment_count: int = 0,
        log_path: str = "",
    ) -> "RenderJob":
        now = utc_now()
        return cls(
            job_id=uuid.uuid4().hex,
            project_id=int(project_id),
            manifest_hash=str(manifest_hash),
            approved_manifest_hash=str(approved_manifest_hash),
            requested_output_path=str(requested_output_path or ""),
            segment_count=max(0, int(segment_count)),
            log_path=str(log_path or ""),
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RenderJob":
        fields = {field for field in cls.__dataclass_fields__}
        values = {key: data[key] for key in fields if key in data}
        return cls(**values)


def validate_job_fields(data: dict[str, Any]) -> None:
    if data.get("status") not in JOB_STATUSES:
        raise ValueError(f"invalid render job status: {data.get('status')}")
    if data.get("stage") not in JOB_STAGES:
        raise ValueError(f"invalid render job stage: {data.get('stage')}")
    try:
        percent = float(data.get("percent", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("render job percent must be numeric") from exc
    if not 0 <= percent <= 100:
        raise ValueError("render job percent must be between 0 and 100")
    if int(data.get("current_segment_index", 0) or 0) < 0:
        raise ValueError("current_segment_index must be non-negative")
    if int(data.get("segment_count", 0) or 0) < 0:
        raise ValueError("segment_count must be non-negative")


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "FINISHED_JOB_STATUSES",
    "JOB_STAGES",
    "JOB_STATUSES",
    "RenderCancelled",
    "RenderJob",
    "utc_now",
    "validate_job_fields",
]
