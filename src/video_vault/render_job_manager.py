"""Single-worker persistent render-job manager."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import queue
import threading
import time
import traceback
from typing import Any, Callable

from .ffmpeg_process_runner import ManagedFFmpegRunner
from .project import can_project_render, project_dir
from .project_renderer import render_project
from .render_job_models import ACTIVE_JOB_STATUSES, RenderCancelled, utc_now
from .render_job_store import RenderJobStore


_SHUTDOWN_TIMEOUT_SECONDS = 10.0


@dataclass
class _Runtime:
    cancel_event: threading.Event
    runner: ManagedFFmpegRunner


class RenderExecutionContext:
    def __init__(self, job_id: str, runner: ManagedFFmpegRunner, cancel_event: threading.Event, update_callback: Callable[..., None]):
        self.job_id = job_id
        self.runner = runner
        self.cancel_event = cancel_event
        self._update_callback = update_callback
        self._lock = threading.RLock()
        self._last_update_at = 0.0
        self._last_percent = -1.0
        self._last_stage = ""
        self._ffmpeg_stage = ""
        self._ffmpeg_base = 0.0
        self._ffmpeg_span = 0.0
        self._ffmpeg_message = ""

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RenderCancelled("render cancellation requested")

    def update(
        self,
        *,
        stage: str,
        percent: float,
        message: str,
        current_segment_id: str | None = None,
        current_segment_index: int | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            changed_stage = stage != self._last_stage
            significant = abs(float(percent) - self._last_percent) >= 1.0
            if not force and not changed_stage and not significant and now - self._last_update_at < 0.25:
                return
            changes: dict[str, Any] = {"stage": stage, "percent": percent, "message": message}
            if current_segment_id is not None:
                changes["current_segment_id"] = current_segment_id
            if current_segment_index is not None:
                changes["current_segment_index"] = current_segment_index
            self._update_callback(**changes)
            self._last_update_at = now
            self._last_percent = float(percent)
            self._last_stage = stage

    def begin_ffmpeg(self, stage: str, base_percent: float, span_percent: float, expected_duration: float, message: str) -> None:
        del expected_duration
        with self._lock:
            self._ffmpeg_stage = stage
            self._ffmpeg_base = float(base_percent)
            self._ffmpeg_span = float(span_percent)
            self._ffmpeg_message = message
            self.runner.on_progress = self._on_runner_progress
        self.update(stage=stage, percent=base_percent, message=message, force=True)

    def _on_runner_progress(self, fraction: float, values: dict[str, str]) -> None:
        del values
        with self._lock:
            stage = self._ffmpeg_stage
            base = self._ffmpeg_base
            span = self._ffmpeg_span
            message = self._ffmpeg_message
        self.update(stage=stage, percent=base + span * max(0.0, min(1.0, fraction)), message=message)


class RenderJobManager:
    def __init__(self, cfg: dict, db: Path, *, store: RenderJobStore | None = None):
        self.cfg = cfg
        self.db = Path(db)
        self.store = store or RenderJobStore(cfg)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._active: dict[str, _Runtime] = {}
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._shutting_down = False

    def start(self) -> None:
        with self._lock:
            if self._started or self._shutting_down:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        if self._started or self._shutting_down:
            return
        self.store.mark_stale_jobs_interrupted()
        self._stop.clear()
        self._started = True
        self._shutting_down = False
        self._worker = threading.Thread(target=self._worker_loop, name="video-vault-render-worker", daemon=True)
        self._worker.start()

    def shutdown(self, wait: bool = True) -> bool:
        with self._lock:
            if not self._started:
                return True
            self._shutting_down = True
            self._stop.set()
            queued_jobs = [job for job in self.store.list() if job.get("status") == "queued"]
            for job in queued_jobs:
                try:
                    self.store.transition(
                        job["job_id"],
                        {"queued"},
                        status="cancelled",
                        stage="done",
                        cancel_requested=True,
                        message="Render Manager 已關閉",
                        process_id=None,
                        finished_at=utc_now(),
                    )
                except (KeyError, ValueError):
                    pass
            active = list(self._active.values())
            worker = self._worker
        for runtime in active:
            runtime.cancel_event.set()
            runtime.runner.request_cancel()
        if wait and worker is not None:
            worker.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        with self._lock:
            if worker is not None and worker.is_alive():
                return False
            self._started = False
            self._worker = None
            self._shutting_down = False
            return True

    def enqueue(self, project_id: int, output_path: Path | None = None) -> dict[str, Any]:
        allowed, reason = can_project_render(self.cfg, self.db, int(project_id))
        if not allowed:
            return {"created": False, "ok": False, "error": reason}
        with self._lock:
            if self._shutting_down:
                return {"created": False, "ok": False, "error": "Render Manager 正在關閉"}
            if not self._started:
                self._start_locked()
            if self._shutting_down:
                return {"created": False, "ok": False, "error": "Render Manager 正在關閉"}
            existing = next((job for job in self.store.list(int(project_id)) if job.get("status") in ACTIVE_JOB_STATUSES), None)
            if existing:
                return {"created": False, "ok": True, "job": existing}
            folder = project_dir(self.cfg, int(project_id))
            manifest = _read_json(folder / "render_manifest.json")
            review = _read_json(folder / "review_status.json")
            job = self.store.create(
                project_id=int(project_id),
                manifest_hash=str(manifest.get("manifest_hash") or ""),
                approved_manifest_hash=str(review.get("approved_manifest_hash") or ""),
                requested_output_path=str(output_path or ""),
                segment_count=len(manifest.get("segments") or []),
            )
            self._queue.put(str(job["job_id"]))
            return {"created": True, "ok": True, "job": job}

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get(job_id)

    def list(self, project_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list(project_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.get(job_id)
            if not job:
                return {"ok": False, "reason": "job not found"}
            status = str(job.get("status"))
            if status == "queued":
                updated = self.store.transition(
                    job_id,
                    {"queued"},
                    status="cancelled",
                    stage="done",
                    percent=job.get("percent", 0),
                    message="已取消排隊中的正式輸出",
                    cancel_requested=True,
                    process_id=None,
                    finished_at=utc_now(),
                )
                if updated is not None:
                    return {"ok": True, "job": updated}
                job = self.store.get(job_id)
                status = str(job.get("status")) if job else ""
            if status not in ACTIVE_JOB_STATUSES:
                return {"ok": False, "reason": "job is already finished", "job": job}
            runtime = self._active.get(job_id)
            if status == "running":
                updated = self.store.transition(
                    job_id,
                    {"running"},
                    status="cancelling",
                    message="正在停止正式輸出",
                    cancel_requested=True,
                )
                if updated is None:
                    job = self.store.get(job_id)
                    status = str(job.get("status")) if job else ""
                    if status == "cancelling":
                        updated = job
                    elif status not in ACTIVE_JOB_STATUSES:
                        return {"ok": False, "reason": "job is already finished", "job": job}
                    else:
                        updated = self.store.transition(
                            job_id,
                            {"running"},
                            status="cancelling",
                            message="正在停止正式輸出",
                            cancel_requested=True,
                        )
                if updated is None:
                    return {"ok": False, "reason": "job state changed", "job": self.store.get(job_id)}
            else:
                updated = job
        if runtime is not None:
            runtime.cancel_event.set()
            runtime.runner.request_cancel()
        return {"ok": True, "job": updated}

    def cancel_project(self, project_id: int) -> dict[str, Any]:
        jobs = [job for job in self.store.list(int(project_id)) if job.get("status") in ACTIVE_JOB_STATUSES]
        results = [self.cancel(str(job["job_id"])) for job in jobs]
        return {"ok": True, "jobs": results}

    def _worker_loop(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                job_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._execute(job_id)
            except Exception:
                traceback.print_exc()
                with self._lock:
                    self._active.pop(job_id, None)
                try:
                    current = self.store.get(job_id)
                    if current and current.get("status") in ACTIVE_JOB_STATUSES:
                        self.store.update(
                            job_id,
                            status="failed",
                            stage="done",
                            message="Render Worker 發生未預期錯誤",
                            error="render worker exception",
                            process_id=None,
                            finished_at=utc_now(),
                        )
                except Exception:
                    traceback.print_exc()
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        cancel_event = threading.Event()
        runner = ManagedFFmpegRunner(
            cancel_event=cancel_event,
            on_process=lambda pid: self._process_update(job_id, pid),
            on_log=lambda line: self.store.append_log(job_id, line),
        )
        context = RenderExecutionContext(job_id, runner, cancel_event, lambda **changes: self._progress_update(job_id, **changes))
        runner.on_progress = context._on_runner_progress
        with self._lock:
            claimed = self.store.transition(
                job_id,
                {"queued"},
                status="running",
                stage="validating",
                message="正在驗證正式輸出條件",
                started_at=utc_now(),
                process_id=None,
            )
            if claimed is None:
                return
            self._active[job_id] = _Runtime(cancel_event, runner)
        try:
            current = claimed
            self.store.append_log(job_id, f"started_at: {current.get('started_at')}\n")
            context.check_cancelled()
            result = render_project(
                self.cfg,
                self.db,
                int(current["project_id"]),
                output_path=Path(current["requested_output_path"]) if current.get("requested_output_path") else None,
                runner=runner,
                execution=context,
            )
        except RenderCancelled:
            self.store.append_log(job_id, "result: cancelled")
            self.store.update(job_id, status="cancelled", stage="done", message="正式輸出已取消", error="", process_id=None, finished_at=utc_now())
        except Exception as exc:
            if cancel_event.is_set():
                self.store.append_log(job_id, "result: cancelled\n" + traceback.format_exc())
                self.store.update(job_id, status="cancelled", stage="done", message="正式輸出已取消", error="", process_id=None, finished_at=utc_now())
            else:
                error = str(exc) or exc.__class__.__name__
                self.store.append_log(job_id, "result: failed\n" + traceback.format_exc())
                self.store.update(job_id, status="failed", stage="done", message="正式輸出失敗", error=error, process_id=None, finished_at=utc_now())
        else:
            self.store.append_log(job_id, f"result: succeeded\noutput: {result.output_path}")
            self.store.update(job_id, status="succeeded", stage="done", percent=100, message="正式輸出完成", output_path=str(result.output_path), cache_hit=bool(result.cache_hit), error="", process_id=None, finished_at=utc_now())
        finally:
            with self._lock:
                self._active.pop(job_id, None)

    def _process_update(self, job_id: str, pid: int | None) -> None:
        try:
            self.store.update(job_id, process_id=pid)
        except (KeyError, ValueError):
            pass

    def _progress_update(self, job_id: str, **changes: Any) -> None:
        try:
            self.store.update(job_id, **changes)
            if "stage" in changes:
                self.store.append_log(job_id, f"stage: {changes['stage']} percent={changes.get('percent', '')} message={changes.get('message', '')}")
        except (KeyError, ValueError):
            pass


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid JSON object: {path}")
    return data


__all__ = ["RenderExecutionContext", "RenderJobManager"]
