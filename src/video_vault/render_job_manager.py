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
from .database import project_revision
from .job_coordinator import JobCoordinator, JobState
from .project_renderer import render_project
from .render_job_models import ACTIVE_JOB_STATUSES, RenderCancelled, utc_now
from .render_job_store import RenderJobStore


_SHUTDOWN_TIMEOUT_SECONDS = 10.0


@dataclass
class _Runtime:
    cancel_event: threading.Event
    runner: ManagedFFmpegRunner
    execution: RenderExecutionContext


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
        self._publish_lock = threading.RLock()
        self._publish_committed = False

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RenderCancelled("render cancellation requested")

    def request_cancel(self) -> bool:
        """Request cancellation unless the final output is already committed.

        The publication lock makes the decision atomic with the final
        ``partial -> output`` replacement.  A caller therefore receives one
        deterministic answer instead of racing a late cancellation against a
        successful persistent render result.
        """
        with self._publish_lock:
            if self._publish_committed:
                return False
            self.cancel_event.set()
            self.runner.request_cancel()
            return True

    def publish_atomically(self, publish: Callable[[], Any]) -> Any:
        """Publish the final output or honour a cancellation before it starts."""
        with self._publish_lock:
            self.check_cancelled()
            result = publish()
            self._publish_committed = True
            return result

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
        self.coordinator = JobCoordinator(ffmpeg_slots=1, gpu_slots=1, ai_provider_slots=1)
        self._claiming: set[str] = set()
        self._enqueue_inflight: dict[int, threading.Event] = {}

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
            if not self._started and not self._enqueue_inflight:
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
            if not self._enqueue_inflight:
                self._shutting_down = False
            return True

    def enqueue(self, project_id: int, output_path: Path | None = None) -> dict[str, Any]:
        project_id = int(project_id)
        allowed, reason = can_project_render(self.cfg, self.db, project_id)
        if not allowed:
            return {"created": False, "ok": False, "error": reason}
        while True:
            with self._lock:
                if self._shutting_down:
                    return {"created": False, "ok": False, "error": "Render Manager 正在關閉"}
                existing = next((job for job in self.store.list(project_id) if job.get("status") in ACTIVE_JOB_STATUSES), None)
                if existing:
                    return {"created": False, "ok": True, "job": existing}
                reservation = self._enqueue_inflight.get(project_id)
                if reservation is None:
                    reservation = threading.Event()
                    self._enqueue_inflight[project_id] = reservation
                    owner = True
                else:
                    owner = False
            if not owner:
                reservation.wait()
                continue
            try:
                prepared = self._prepare_enqueue(project_id, output_path)
                # A probe may be slow and the approval can change while it is
                # running.  Revalidate before touching the manager state or
                # creating a persistent job so the contract cannot be stale.
                allowed, reason = can_project_render(self.cfg, self.db, project_id)
                if not allowed:
                    return {"created": False, "ok": False, "error": reason}
                with self._lock:
                    if self._shutting_down:
                        return {"created": False, "ok": False, "error": "Render Manager 正在關閉"}
                    existing = next((job for job in self.store.list(project_id) if job.get("status") in ACTIVE_JOB_STATUSES), None)
                    if existing:
                        return {"created": False, "ok": True, "job": existing}
                    if not _approval_binding_matches(self.cfg, project_id, prepared):
                        return {"created": False, "ok": False, "error": "正式輸出核准狀態在 encoder probe 期間變更，請重新核准"}
                    if not self._started:
                        self._start_locked()
                    if self._shutting_down:
                        return {"created": False, "ok": False, "error": "Render Manager 正在關閉"}
                    try:
                        base_revision = project_revision(self.db, project_id)
                    except ValueError:
                        # Keep the existing HTTP-neutral manager tests and legacy
                        # callers usable when they provide a mocked render gate.
                        base_revision = 1
                    coordinated = self.coordinator.submit(
                        project_id,
                        "formal_render",
                        {"approval_snapshot_reader", "ffmpeg_heavy", "gpu_heavy"},
                        base_revision,
                        resources={"ffmpeg_heavy", "gpu_heavy"},
                    )
                    job = self.store.create(
                        project_id=project_id,
                        manifest_hash=prepared["manifest_hash"],
                        approved_manifest_hash=prepared["approved_manifest_hash"],
                        approval_snapshot_id=prepared["snapshot_id"],
                        approval_snapshot_hash=prepared["snapshot_hash"],
                        approval_snapshot=prepared["snapshot"],
                        encoder_contract=prepared["encoder_contract"],
                        requested_output_path=str(output_path or ""),
                        segment_count=prepared["segment_count"],
                        base_revision=base_revision,
                        generation=coordinated.generation,
                        capabilities=sorted(coordinated.capabilities),
                        resources=sorted(coordinated.resources),
                        queue_reason=coordinated.queue_reason,
                    )
                    job["coordinator_job_id"] = coordinated.job_id
                    self.store.update(str(job["job_id"]), coordinator_job_id=coordinated.job_id)
                    self._queue.put(str(job["job_id"]))
                    return {"created": True, "ok": True, "job": job}
            except Exception as exc:
                return {"created": False, "ok": False, "error": str(exc) or exc.__class__.__name__}
            finally:
                with self._lock:
                    self._enqueue_inflight.pop(project_id, None)
                    reservation.set()
                    if self._shutting_down and not self._started and not self._enqueue_inflight:
                        self._shutting_down = False

    def _prepare_enqueue(self, project_id: int, output_path: Path | None) -> dict[str, Any]:
        del output_path
        folder = project_dir(self.cfg, int(project_id))
        manifest = _read_json(folder / "render_manifest.json")
        review = _read_json(folder / "review_status.json")
        from .approval_snapshot import load_approval_snapshot, validate_snapshot

        snapshot = None
        snapshot_token = str(review.get("approval_snapshot_path") or "")
        if snapshot_token:
            try:
                snapshot = load_approval_snapshot(folder / snapshot_token)
                snapshot_validation = validate_snapshot(snapshot, check_assets=True)
            except Exception as exc:
                raise ValueError(f"approval snapshot 無法讀取：{exc}") from exc
            if not snapshot_validation["valid"]:
                raise ValueError("approval snapshot 已失效：" + "; ".join(snapshot_validation["errors"]))
        encoder_contract = None
        if snapshot is not None:
            from .encoder_contract import resolve_encoder_contract
            try:
                # This is deliberately outside ``self._lock``.  The resolver
                # may run both an NVENC probe and ``ffmpeg -version``.
                encoder_contract = resolve_encoder_contract(
                    self.cfg,
                    dict(snapshot.get("manifest", {}).get("profile") or {}),
                    str((snapshot.get("manifest", {}).get("settings") or {}).get("encoder") or "auto"),
                )
            except Exception as exc:
                raise ValueError(f"encoder 無法解析：{exc}") from exc
        return {
            "folder": folder,
            "manifest_hash": str(manifest.get("manifest_hash") or ""),
            "approved_manifest_hash": str(review.get("approved_manifest_hash") or ""),
            "approval_snapshot_path": snapshot_token,
            "snapshot_id": str((snapshot or {}).get("snapshot_id") or ""),
            "snapshot_hash": str((snapshot or {}).get("snapshot_hash") or ""),
            "snapshot": snapshot,
            "encoder_contract": encoder_contract,
            "segment_count": len(manifest.get("segments") or []),
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get(job_id)

    def list(self, project_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list(project_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        # A worker that has entered the atomic queued -> running claim must
        # finish that tiny boundary before cancellation decides whether it is
        # stopping a queued job or an already claimed runtime.
        while True:
            with self._lock:
                claiming = str(job_id) in self._claiming
            if not claiming:
                break
            time.sleep(0.001)
        with self._lock:
            job = self.store.get(job_id)
            if not job:
                return {"ok": False, "reason": "job not found"}
            status = str(job.get("status"))
            coordinator_id = str(job.get("coordinator_job_id") or "")
            if status == "queued":
                if coordinator_id:
                    self.coordinator.cancel(coordinator_id)
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
            if runtime is not None and not runtime.execution.request_cancel():
                # The final MP4/report pair is already committed.  Do not move
                # either state machine into cancellation: the worker will
                # immediately complete both as succeeded.
                return {
                    "ok": False,
                    "code": "cancel_too_late",
                    "reason": "output already published",
                    "job": self.store.get(job_id) or job,
                }
            if coordinator_id:
                self.coordinator.cancel(coordinator_id)
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
                # The worker claims the persistent state before registering its
                # runtime. In that narrow window there is no process to stop;
                # complete cancellation now so callers never observe a
                # transient cancelling job with no runtime behind it.
                if runtime is None:
                    completed = self.store.transition(
                        job_id,
                        {"cancelling"},
                        status="cancelled",
                        stage="done",
                        message="正式輸出已取消",
                        cancel_requested=True,
                        process_id=None,
                        finished_at=utc_now(),
                    )
                    if completed is not None:
                        updated = completed
                    if coordinator_id:
                        self.coordinator.finish_cancel(coordinator_id)
            else:
                updated = job
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
                        coordinator_id = str(current.get("coordinator_job_id") or "")
                        cancelled = current.get("status") in {"cancelling", "cancelled"} or bool(current.get("cancel_requested"))
                        if coordinator_id:
                            if cancelled:
                                self.coordinator.finish_cancel(coordinator_id)
                            else:
                                self.coordinator.fail(coordinator_id, "render worker exception")
                        self.store.update(
                            job_id,
                            status="cancelled" if cancelled else "failed",
                            stage="done",
                            message="正式輸出已取消" if cancelled else "Render Worker 發生未預期錯誤",
                            error="" if cancelled else "render worker exception",
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
        queued = self.store.get(job_id)
        coordinator_id = str((queued or {}).get("coordinator_job_id") or "")
        while coordinator_id:
            coordinated = self.coordinator.get(coordinator_id)
            if not coordinated or coordinated.state == JobState.RUNNING:
                break
            if coordinated.state in {JobState.CANCELLED, JobState.SUPERSEDED}:
                self.store.transition(job_id, {"queued"}, status="cancelled", stage="done", message="正式輸出未開始前已取消", finished_at=utc_now())
                return
            if coordinated.state == JobState.CANCELLING:
                self.coordinator.finish_cancel(coordinator_id)
                self.store.transition(job_id, {"queued", "running", "cancelling"}, status="cancelled", stage="done", message="正式輸出未開始前已取消", finished_at=utc_now())
                return
            try:
                self.coordinator.start(coordinator_id)
            except RuntimeError:
                time.sleep(0.1)
        # Do not hold the manager lock across this conditional store write.
        # Cancellation must be able to win the queued -> running race while a
        # store adapter or test hook is waiting for another thread.
        with self._lock:
            self._claiming.add(job_id)
        try:
            claimed = self.store.transition(
                job_id,
                {"queued"},
                status="running",
                stage="validating",
                message="正在驗證正式輸出條件",
                started_at=utc_now(),
                process_id=None,
            )
        finally:
            with self._lock:
                self._claiming.discard(job_id)
        if claimed is None:
            return
        with self._lock:
            latest = self.store.get(job_id)
            if latest and (latest.get("status") == "cancelling" or latest.get("cancel_requested")):
                cancel_event.set()
                runner.request_cancel()
            self._active[job_id] = _Runtime(cancel_event, runner, context)
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
                approval_snapshot=current.get("approval_snapshot") if isinstance(current.get("approval_snapshot"), dict) else None,
                encoder_contract=current.get("encoder_contract") if isinstance(current.get("encoder_contract"), dict) else None,
            )
        except RenderCancelled:
            if coordinator_id:
                self.coordinator.finish_cancel(coordinator_id)
            self.store.append_log(job_id, "result: cancelled")
            self.store.update(job_id, status="cancelled", stage="done", message="正式輸出已取消", error="", process_id=None, finished_at=utc_now())
        except Exception as exc:
            if cancel_event.is_set():
                if coordinator_id:
                    self.coordinator.finish_cancel(coordinator_id)
                self.store.append_log(job_id, "result: cancelled\n" + traceback.format_exc())
                self.store.update(job_id, status="cancelled", stage="done", message="正式輸出已取消", error="", process_id=None, finished_at=utc_now())
            else:
                if coordinator_id:
                    self.coordinator.fail(coordinator_id, str(exc))
                error = str(exc) or exc.__class__.__name__
                self.store.append_log(job_id, "result: failed\n" + traceback.format_exc())
                self.store.update(job_id, status="failed", stage="done", message="正式輸出失敗", error=error, process_id=None, finished_at=utc_now())
        else:
            if coordinator_id:
                self.coordinator.complete(coordinator_id)
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


def _approval_binding_matches(cfg: dict[str, Any], project_id: int, prepared: dict[str, Any]) -> bool:
    """Compare the cheap immutable approval pointers after an unlocked probe."""
    del cfg
    folder = Path(prepared["folder"])
    try:
        manifest = _read_json(folder / "render_manifest.json")
        review = _read_json(folder / "review_status.json")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if str(manifest.get("manifest_hash") or "") != prepared["manifest_hash"]:
        return False
    if str(review.get("approved_manifest_hash") or "") != prepared["approved_manifest_hash"]:
        return False
    if str(review.get("approval_snapshot_path") or "") != prepared["approval_snapshot_path"]:
        return False
    if prepared["snapshot"] is None:
        return True
    try:
        from .approval_snapshot import load_approval_snapshot

        latest = load_approval_snapshot(folder / prepared["approval_snapshot_path"])
    except Exception:
        return False
    return (
        int(latest.get("project_id") or 0) == int(project_id)
        and str(latest.get("snapshot_id") or "") == prepared["snapshot_id"]
        and str(latest.get("snapshot_hash") or "") == prepared["snapshot_hash"]
    )


__all__ = ["RenderExecutionContext", "RenderJobManager"]
