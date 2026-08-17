from __future__ import annotations

from email import policy
from email.parser import BytesParser
from copy import deepcopy
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import json
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time

from .analyzer.vision_pipeline import analyze_video_frames
from .audio_state import audio_state_for_api, update_audio_state
from .artifact_retention import RetentionError, build_cleanup_plan, execute_cleanup_plan, free_disk_bytes, load_inventory, reconcile_inventory, set_artifact_pinned
from .audio_preview import AudioPreviewError, audio_preview_file_path, render_project_audio_preview
from .doctor import collect_doctor_report_from_config
from .delivery_qa import DeliveryQAError, QAReviewVersionConflict, delivery_qa_artifact_path, delivery_qa_for_api, delivery_qa_output_path, rerun_current_delivery_qa, review_delivery_qa
from .bgm_pipeline import BgmPipelineError, validate_bgm_track
from .bgm import import_bgm, list_bgm
from .color import render_color_preview
from .color_consistency import ColorReferenceError, analyze_project_color, color_state_for_api, preview_file_path, reference_file_path, render_project_color_previews, set_color_reference, update_color_state
from .color_pipeline import ColorPipelineError
from .creative_brief import creative_brief_api_payload, creative_brief_options, ensure_creative_brief, recommend_creative_brief, save_approved_creative_brief
from .visual_style import VisualStyleError, ensure_visual_style_state, preview_visual_styles, save_visual_style_approval, visual_style_api_payload, visual_style_options, visual_style_preview_path
from .cloud_review import CloudReviewError, add_review_usage, build_review_plan, empty_review_usage, execute_review_plan
from .cloud_review_ledger import finalize_attempt as finalize_cloud_review_attempt, reserve_attempt as reserve_cloud_review_attempt, usage_for_scope as cloud_review_usage_for_scope
from .database import add_frame, add_project_bgm, bgm_tracks as db_bgm_tracks, connect, frames as db_frames, init_db, project as db_project, project_bgm_tracks, project_videos, set_project_videos, set_video_status, update_video_summary, upsert_video, videos
from .ffmpeg_tools import extract_frames, frame_timestamp, metadata
from .hyperframes import export_hyperframes_project, render_fast_draft
from .naming import rename_after_perception
from .opencut import OPENCUT_URL, export_opencut_handoff, opencut_status, start_opencut
from .paths import db_path
from .planner import draft_plan, perceive_output, review_text, revise_plan, set_plan_status, video_dir, write_plan_files
from .project import build_project_plan, can_project_render, create_project, list_projects, mark_project_needs_review, project_detail, project_dir, revise_project, save_revision_notes, save_segment_review, set_review_status, sync_project_files, update_segment_evidence, update_segment_timing
from .project_lifecycle import CancellationRequested, CancellationToken, ProjectRevisionConflict, check_base_revision, current_revision, project_commit
from .job_coordinator import DEFAULT_COORDINATOR, JobState
from .project_perception import run_project_perception
from .perception_runs import PerceptionCancelled, analysis_run, ensure_perception_schema, perception_jobs, perception_states_for_project, recover_interrupted_perception_runs, set_run_cloud_review
from .story_calibration import calibration_for_profile, recalculate_calibration, reset_calibration
from .render_api import RenderAPI
from .render_job_manager import RenderJobManager
from .renderer import render_approved
from .scanner import scan_inbox
from .storyboard import generate_storyboard, generate_thumbnail, storyboard_for_api, storyboard_thumbnail_path, update_storyboard
from .storyboard_preview import StoryboardPreviewError, render_storyboard_preview, storyboard_preview_path
from .story_generation import StoryGenerationError, StoryValidationError, apply_story_generation_to_storyboard, generate_project_story, get_story_generation, project_story_detail, update_story_generation_review
from .story_profiles import CreatorProfileRevisionConflict, StorySettingsRevisionConflict, load_creator_profile, load_project_story_settings, save_creator_profile, save_project_story_settings, story_profile_definition
from .web_security import WebSecurityError, parse_content_length, parse_single_range, validate_local_bind_host, validate_origin_headers


JOBS: dict[tuple[int, str], dict] = {}
JOBS_LOCK = threading.Lock()
JOB_COORDINATOR = DEFAULT_COORDINATOR
JOB_TOKENS: dict[tuple[int, str], CancellationToken] = {}
FORM_CSRF_TOKEN = ""
MEDIA_STREAM_CHUNK = 64 * 1024


class MultipartFormError(ValueError):
    """A client-side multipart request was malformed or truncated."""


UPLOAD_READ_CHUNK = 64 * 1024
UPLOAD_SPOOL_THRESHOLD = 8 * 1024 * 1024
MAX_MULTIPART_HEADER = 64 * 1024
MAX_TEXT_FIELD = 1024 * 1024
MAX_JSON_BODY = 2 * 1024 * 1024
MAX_FORM_BODY = 2 * 1024 * 1024
MAX_UPLOAD_BODY = 100 * 1024 * 1024 * 1024
UPLOAD_SEMAPHORE = threading.BoundedSemaphore(2)


def _base_revision(data: dict) -> int | None:
    value = data.get("base_revision", data.get("project_revision"))
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("base_revision 必須是整數") from exc


def _sampling_override(data: dict) -> dict | None:
    raw = data.get("sampling")
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError("sampling 必須是物件")
    result: dict[str, object] = {}
    if "mode" in raw:
        mode = str(raw["mode"]).lower()
        if mode not in {"fixed", "adaptive"}:
            raise ValueError("sampling.mode 必須是 fixed 或 adaptive")
        result["mode"] = mode
    if "preset" in raw:
        preset = str(raw["preset"]).lower()
        if preset not in {"balanced", "dense"}:
            raise ValueError("sampling.preset 必須是 balanced 或 dense")
        result["preset"] = preset
    if "baseline_interval_seconds" in raw:
        interval = float(raw["baseline_interval_seconds"])
        if not 0.5 <= interval <= 60:
            raise ValueError("baseline_interval_seconds 必須介於 0.5 和 60")
        result["baseline_interval_seconds"] = interval
    if "max_frames_per_clip" in raw:
        maximum = int(raw["max_frames_per_clip"])
        if not 1 <= maximum <= 2000:
            raise ValueError("max_frames_per_clip 必須介於 1 和 2000")
        result["max_frames_per_clip"] = maximum
    return result or None


def _cloud_review_candidates(cfg: dict, db: Path, project_id: int) -> list[dict]:
    """Build review candidates from current local runs without exposing paths."""

    states = perception_states_for_project(db, project_id)
    candidates: list[dict] = []
    for video in project_videos(db, project_id):
        video_id = int(video["id"])
        state = states.get(video_id) or {}
        run_uuid = str(state.get("current_analysis_run_uuid") or "")
        if not run_uuid:
            continue
        run = analysis_run(db, run_uuid)
        manifest_by_window = {
            str(window.get("window_uuid") or ""): window
            for window in run.get("window_manifest") or []
            if isinstance(window, dict)
        }
        for result in run.get("window_results") or []:
            if not isinstance(result, dict):
                continue
            window_uuid = str(result.get("window_uuid") or "")
            window = manifest_by_window.get(window_uuid) or {}
            frame_entries = list(window.get("frames") or [])
            candidates.append({
                "project_id": int(project_id),
                "video_id": video_id,
                "run_uuid": run_uuid,
                "window_uuid": window_uuid,
                "segment_uuid": str(result.get("segment_uuid") or ""),
                "ordinal": int(result.get("ordinal") or window.get("ordinal") or 0),
                "start_seconds": float(result.get("start_seconds") or window.get("start_seconds") or 0),
                "end_seconds": float(result.get("end_seconds") or window.get("end_seconds") or 0),
                "frame_timestamps": [float(value) for value in result.get("frame_timestamps") or []],
                "confidence": float(result.get("confidence") or 0),
                "validation": result.get("validation") if isinstance(result.get("validation"), dict) else {},
                "frame_paths": [str(frame.get("frame_path") or "") for frame in frame_entries],
            })
    return candidates


def _cloud_review_selection(data: dict) -> set[str] | None:
    raw = data.get("window_uuids", data.get("selected_window_ids"))
    if raw in (None, ""):
        return None
    if not isinstance(raw, list):
        raise ValueError("window_uuids 必須是陣列")
    return {str(value).strip() for value in raw if str(value).strip()}


def _cloud_review_scope(candidates: list[dict], states: dict[int, dict]) -> tuple[list[str], dict[str, dict]]:
    run_uuids = sorted({str(candidate.get("run_uuid") or "") for candidate in candidates if str(candidate.get("run_uuid") or "")})
    baseline_by_run = {}
    for candidate in candidates:
        run_uuid = str(candidate.get("run_uuid") or "")
        state = states.get(int(candidate.get("video_id") or 0)) or {}
        if run_uuid and run_uuid not in baseline_by_run:
            baseline_by_run[run_uuid] = state.get("current_cloud_review") or {}
    return run_uuids, baseline_by_run


def _cloud_review_plan(cfg: dict, db: Path, project_id: int, data: dict) -> dict:
    candidates = _cloud_review_candidates(cfg, db, project_id)
    states = perception_states_for_project(db, project_id)
    scope_run_uuids, baseline_by_run = _cloud_review_scope(candidates, states)
    public = build_review_plan(
        candidates,
        cfg,
        selected_window_ids=_cloud_review_selection(data),
        usage=cloud_review_usage_for_scope(db, project_id, scope_run_uuids, baseline_by_run),
    )
    return {"ok": True, "plan": public, "project_revision": current_revision(db, project_id)}


def _cloud_review_execute(cfg: dict, db: Path, project_id: int, data: dict) -> dict:
    base_revision = _base_revision(data)
    check_base_revision(db, project_id, base_revision)
    candidates = _cloud_review_candidates(cfg, db, project_id)
    states = perception_states_for_project(db, project_id)
    scope_run_uuids, baseline_by_run = _cloud_review_scope(candidates, states)
    plan = build_review_plan(
        candidates,
        cfg,
        selected_window_ids=_cloud_review_selection(data),
        usage=cloud_review_usage_for_scope(db, project_id, scope_run_uuids, baseline_by_run),
    )
    if plan.get("status") != "ready":
        raise ValueError(f"cloud review plan is not ready: {plan.get('status')}")
    frame_paths_by_window = {}
    staging_roots: dict[str, Path] = {}
    for candidate in candidates:
        run_uuid = str(candidate.get("run_uuid") or "")
        raw_staging = str(analysis_run(db, run_uuid).get("staging_path") or "").strip()
        if not raw_staging:
            raise ValueError("current perception staging root is unavailable")
        staging_roots.setdefault(run_uuid, Path(raw_staging).resolve())
        if candidate["window_uuid"] not in {item["window_uuid"] for item in plan.get("windows") or []}:
            continue
        root = staging_roots[run_uuid]
        paths = []
        for raw_path in candidate.get("frame_paths") or []:
            path = Path(raw_path).resolve()
            if root not in path.parents:
                raise ValueError("cloud review frame is outside the current perception staging root")
            paths.append(path)
        frame_paths_by_window[candidate["window_uuid"]] = paths
    def reserve_attempt(item: dict, frame_count: int) -> dict:
        return reserve_cloud_review_attempt(
            db,
            project_id=project_id,
            video_id=int(item.get("video_id") or 0),
            run_uuid=str(item.get("run_uuid") or ""),
            window_uuid=str(item.get("window_uuid") or ""),
            frame_count=int(frame_count),
            estimated_cost_usd=int(frame_count) * float((plan.get("policy") or {}).get("estimated_cost_per_frame_usd") or 0.0),
            policy=plan.get("policy") or {},
            scope_run_uuids=scope_run_uuids,
            baseline_by_run=baseline_by_run,
        )

    def finalize_attempt(record: dict, status: str, error: str) -> None:
        reservation_uuid = str(record.get("reservation_uuid") or "")
        if reservation_uuid:
            finalize_cloud_review_attempt(db, reservation_uuid, status, error)

    try:
        outcome = execute_review_plan(
            plan,
            frame_paths_by_window,
            cfg,
            attempt_reserver=reserve_attempt,
            attempt_finalizer=finalize_attempt,
        )
    except CloudReviewError as exc:
        outcome = {"contract_version": plan["contract_version"], "status": "failed", "provider": plan.get("provider"), "error": str(exc), "results": [], "completed_count": 0}
    review_by_run: dict[str, dict] = {}
    result_by_window = {str(item.get("window_uuid") or ""): item for item in outcome.get("results") or []}
    increment_by_run: dict[str, dict] = {}
    attempts_by_run: dict[str, list[dict]] = {}
    for attempt in outcome.get("attempts") or []:
        run_uuid = str(attempt.get("run_uuid") or "")
        attempts_by_run.setdefault(run_uuid, []).append(dict(attempt))
        frames = int(attempt.get("frame_count") or 0)
        cost = float(attempt.get("estimated_cost_usd") or 0.0)
        increment = increment_by_run.setdefault(run_uuid, empty_review_usage())
        increment["calls"] += 1
        increment["frames"] += frames
        increment["estimated_cost_usd"] = round(increment["estimated_cost_usd"] + cost, 6)
        clip = increment["by_clip"].setdefault(str(attempt.get("video_id") or ""), {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0})
        clip["calls"] += 1
        clip["frames"] += frames
        clip["estimated_cost_usd"] = round(clip["estimated_cost_usd"] + cost, 6)
    for item in plan.get("windows") or []:
        run_uuid = str(item.get("run_uuid") or "")
        prior = (states.get(int(item.get("video_id") or 0)) or {}).get("current_cloud_review") or {}
        review = review_by_run.setdefault(run_uuid, {
            "contract_version": plan["contract_version"],
            "status": outcome.get("status"),
            "provider": outcome.get("provider"),
            "model": outcome.get("model"),
            "policy": plan.get("policy"),
            "windows": [],
            "error": outcome.get("error"),
            "usage": add_review_usage(prior.get("usage") if isinstance(prior, dict) else None, increment_by_run.get(run_uuid)),
            "ledger_backed": bool((prior.get("ledger_backed") if isinstance(prior, dict) else False) or attempts_by_run.get(run_uuid)),
            "actual_attempt_count": len(attempts_by_run.get(run_uuid) or []),
            "attempts": attempts_by_run.get(run_uuid) or [],
        })
        review["windows"].append({**{key: value for key, value in item.items() if key not in {"source_paths_exposed"}}, "result": result_by_window.get(item["window_uuid"])})
    with project_commit(db, project_id, base_revision) as commit:
        for run_uuid, review in review_by_run.items():
            set_run_cloud_review(db, run_uuid, review)
        mark_project_needs_review(cfg, db, project_id)
        commit.record_changed(True)
    return {
        "ok": outcome.get("status") == "completed",
        "code": None if outcome.get("status") == "completed" else "cloud_review_failed",
        "review_status": outcome.get("status"),
        "cloud_review": outcome,
        "project_revision": current_revision(db, project_id),
        "local_result_preserved": True,
    }


class ProjectRevisionRequired(ValueError):
    """A modern API writer was called without its optimistic-lock token."""

    code = "base_revision_required"

    def __init__(self) -> None:
        super().__init__("缺少 base_revision，請重新載入專案後再操作")


def _require_api_base_revision(data: dict) -> int:
    revision = _base_revision(data)
    if revision is None:
        raise ProjectRevisionRequired()
    return revision


def _approve_project_api(cfg: dict, db: Path, data: dict) -> dict:
    """Apply the project approval API contract without swallowing conflicts.

    Missing storyboard initialization is a user-actionable validation result,
    while optimistic-lock conflicts must continue to propagate to ``do_POST``
    so callers receive the established HTTP 409 response.
    """
    try:
        set_review_status(
            cfg,
            db,
            int(data.get("project_id", 0)),
            "approved",
            data.get("notes", ""),
            base_revision=_base_revision(data),
        )
    except ValueError as exc:
        if not str(exc).startswith("缺少 storyboard.json"):
            raise
        return {
            "ok": False,
            "code": "storyboard_required",
            "error": "尚未建立 storyboard.json，請先到「分鏡審核」執行「建立分鏡」，完成後再核准。",
        }
    return {"ok": True}


class _UploadPart:
    def __init__(self, filename: str, file, value: str = ""):
        self.filename = filename
        self.file = file
        self.value = value

    def close(self) -> None:
        try:
            self.file.close()
        except (OSError, ValueError):
            pass


class _BoundedRequestReader:
    def __init__(self, stream, length: int):
        self.stream = stream
        self.remaining = length

    def read_chunk(self) -> bytes:
        if self.remaining <= 0:
            return b""
        size = min(UPLOAD_READ_CHUNK, self.remaining)
        try:
            chunk = self.stream.read(size)
        except (OSError, ValueError) as exc:
            raise MultipartFormError(f"讀取上傳內容失敗：{exc}") from exc
        if not chunk:
            raise MultipartFormError("multipart request 被截斷")
        if len(chunk) > size:
            raise MultipartFormError("上傳串流違反固定大小讀取限制")
        self.remaining -= len(chunk)
        return bytes(chunk)


class _StreamingMultipartReader:
    def __init__(self, request: _BoundedRequestReader, boundary: bytes):
        self.request = request
        self.boundary = boundary
        self.buffer = bytearray()

    def _fill(self) -> None:
        chunk = self.request.read_chunk()
        if not chunk:
            raise MultipartFormError("multipart request 缺少結束 boundary")
        self.buffer.extend(chunk)

    def _read_until(self, marker: bytes, *, limit: int) -> bytes:
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                result = bytes(self.buffer[:index])
                del self.buffer[: index + len(marker)]
                return result
            if len(self.buffer) > limit:
                raise MultipartFormError("multipart header 超過大小限制")
            self._fill()

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self._fill()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read_part(self, target, text_buffer: bytearray | None) -> None:
        marker = b"\r\n--" + self.boundary
        keep = len(marker) - 1
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                payload = bytes(self.buffer[:index])
                del self.buffer[: index + len(marker)]
                _consume_part_payload(target, text_buffer, payload)
                return
            if len(self.buffer) > keep:
                payload = bytes(self.buffer[:-keep])
                del self.buffer[:-keep]
                _consume_part_payload(target, text_buffer, payload)
            self._fill()


def _consume_part_payload(target, text_buffer: bytearray | None, payload: bytes) -> None:
    if not payload:
        return
    if target is not None:
        target.write(payload)
        return
    if text_buffer is None:
        return
    if len(text_buffer) + len(payload) > MAX_TEXT_FIELD:
        raise MultipartFormError("文字欄位超過大小限制")
    text_buffer.extend(payload)


def _part_headers(raw_headers: bytes):
    try:
        return BytesParser(policy=policy.default).parsebytes(raw_headers + b"\r\n\r\n")
    except (ValueError, UnicodeError) as exc:
        raise MultipartFormError(f"multipart part header 無法解析：{exc}") from exc


def _close_form(form: dict[str, list[_UploadPart]]) -> None:
    for items in form.values():
        for item in items:
            item.close()


def _multipart_form(handler: BaseHTTPRequestHandler) -> dict[str, list[_UploadPart]]:
    try:
        length = parse_content_length(
            handler.headers.get("content-length"),
            maximum=MAX_UPLOAD_BODY,
            required=True,
        )
    except WebSecurityError as exc:
        raise MultipartFormError(exc.message) from exc
    content_type = str(handler.headers.get("content-type", "") or "")
    header = BytesParser(policy=policy.default).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    boundary_text = header.get_boundary()
    if not boundary_text:
        raise MultipartFormError("multipart request 缺少 boundary")
    boundary = str(boundary_text).encode("utf-8")
    reader = _StreamingMultipartReader(_BoundedRequestReader(handler.rfile, length), boundary)
    fields: dict[str, list[_UploadPart]] = {}
    try:
        initial = reader._read_until(b"\r\n", limit=MAX_MULTIPART_HEADER)
        if initial != b"--" + boundary:
            raise MultipartFormError("multipart request 起始 boundary 無效")
        while True:
            raw_headers = reader._read_until(b"\r\n\r\n", limit=MAX_MULTIPART_HEADER)
            part = _part_headers(raw_headers)
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename() or ""
            stored_file = None
            try:
                stored_file = tempfile.SpooledTemporaryFile(max_size=UPLOAD_SPOOL_THRESHOLD, mode="w+b") if name and filename else None
                text_buffer = bytearray() if name and not filename else None
                reader.read_part(stored_file, text_buffer)
                closing = reader._read_exact(2)
                if closing not in {b"\r\n", b"--"}:
                    raise MultipartFormError("multipart boundary 結尾無效")
                if name:
                    if stored_file is not None:
                        stored_file.seek(0)
                        fields.setdefault(str(name), []).append(_UploadPart(str(filename), stored_file))
                        stored_file = None
                    else:
                        charset = part.get_content_charset() or "utf-8"
                        value = bytes(text_buffer or b"").decode(charset, errors="replace")
                        empty = tempfile.SpooledTemporaryFile(max_size=0, mode="w+b")
                        fields.setdefault(str(name), []).append(_UploadPart("", empty, value))
                if closing == b"--":
                    if reader.request.remaining:
                        trailing = reader.request.read_chunk()
                        if trailing.strip(b"\r\n"):
                            raise MultipartFormError("multipart request 在結束 boundary 後仍有無效內容")
                    return fields
            except BaseException:
                if stored_file is not None:
                    stored_file.close()
                raise
    except BaseException:
        _close_form(fields)
        raise


def _form_items(form: dict[str, list[_UploadPart]], name: str) -> list[_UploadPart]:
    return form.get(name, [])


def _form_value(form: dict[str, list[_UploadPart]], name: str) -> str:
    items = _form_items(form, name)
    return items[0].value if items else ""


def run_ui(cfg: dict, host: str = "127.0.0.1", port: int = 8765) -> None:
    validate_local_bind_host(host)
    global FORM_CSRF_TOKEN
    FORM_CSRF_TOKEN = secrets.token_urlsafe(32)
    db = db_path(cfg)
    init_db(db)
    ensure_perception_schema(db)
    recover_interrupted_perception_runs(db, cfg)
    # Story generation recovery is a startup boundary only. GET/project
    # polling must remain observational and never rewrite in-flight state.
    from .story_generation import recover_interrupted_story_generations

    recover_interrupted_story_generations(db)
    render_manager = RenderJobManager(cfg, db)
    render_manager.coordinator = JOB_COORDINATOR
    render_manager.start()
    render_api = RenderAPI(render_manager)

    class Handler(BaseHTTPRequestHandler):
        def _check_host(self) -> None:
            validate_origin_headers(self.headers, host, port, csrf_token=None, supplied_token=None)

        def _check_mutation(self, supplied_token: str | None = None) -> None:
            validate_origin_headers(self.headers, host, port, csrf_token=FORM_CSRF_TOKEN, supplied_token=supplied_token or self.headers.get("x-video-vault-csrf"))

        def do_GET(self) -> None:
            self._handle_get(head_only=False)

        def _handle_get(self, *, head_only: bool) -> None:
            self._head_only = head_only
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                # GET requests do not need CSRF, but Host validation still
                # prevents DNS-rebinding access to this local service.
                from .web_security import validate_host_header

                validate_host_header(self.headers.get("host"), host, port)
            except WebSecurityError as exc:
                self._json(exc.as_dict(), status=400)
                return
            if parsed.path == "/api/security":
                self._json({"ok": True, "csrf_token": FORM_CSRF_TOKEN})
            elif parsed.path in {"/", "/system-status"} and _web_dist().exists():
                self._file(_web_dist() / "index.html")
            elif parsed.path == "/classic" or (parsed.path == "/" and not _web_dist().exists()):
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                self._html(render_page(cfg, db, project_id, query.get("message", [""])[0], render_manager))
            elif parsed.path == "/bgm" and _web_dist().exists():
                self._file(_web_dist() / "index.html")
            elif parsed.path in {"/bgm", "/classic-bgm"}:
                self._html(render_bgm_page(db, query.get("message", [""])[0]))
            elif parsed.path == "/api/projects":
                self._json(list_projects(db))
            elif parsed.path == "/api/project":
                self._json(_project_detail_for_api(cfg, project_detail(cfg, db, int(query.get("id", ["0"])[0]))))
            elif parsed.path == "/api/doctor":
                mode = str(query.get("mode", ["default"])[0] or "default").lower()
                check_id = str(query.get("check_id", [""])[0] or "").strip() or None
                try:
                    self._json(collect_doctor_report_from_config(cfg, mode=mode, check_id=check_id, repo_root=Path(__file__).resolve().parents[2]))
                except ValueError as exc:
                    self._json({"ok": False, "code": "invalid_doctor_mode", "error": str(exc)}, status=400)
            elif parsed.path == "/api/project/storyboard":
                self._json(storyboard_for_api(cfg, db, int(query.get("project_id", ["0"])[0] or 0)))
            elif parsed.path == "/api/project/story":
                self._json(_project_detail_for_api(cfg, project_detail(cfg, db, int(query.get("project_id", ["0"])[0] or 0))).get("story", {}))
            elif parsed.path == "/api/project/creative-brief":
                self._json(creative_brief_api_payload(ensure_creative_brief(cfg, db, int(query.get("project_id", ["0"])[0] or 0))))
            elif parsed.path == "/api/project/creative-brief/options":
                self._json(creative_brief_options())
            elif parsed.path == "/api/project/visual-style":
                self._json(visual_style_api_payload(ensure_visual_style_state(cfg, db, int(query.get("project_id", ["0"])[0] or 0))))
            elif parsed.path == "/api/project/visual-style/options":
                self._json(visual_style_options())
            elif parsed.path == "/api/creator-profile":
                self._json(load_creator_profile(cfg))
            elif parsed.path == "/api/project/story/calibration":
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                settings = load_project_story_settings(cfg, db, project_id)
                self._json(calibration_for_profile(cfg, db, str(settings.get("profile_id") or "general_diary")))
            elif parsed.path == "/api/project/storage":
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                inventory = reconcile_inventory(cfg, project_id)
                artifacts = inventory.get("artifacts", [])
                public_artifacts = [{**{key: value for key, value in item.items() if key != "path"}, "display_name": Path(str(item.get("path") or "")).name} for item in artifacts]
                self._json({
                    "ok": True,
                    "project_id": project_id,
                    "artifacts": public_artifacts,
                    "total_bytes": sum(int(item.get("size") or 0) for item in artifacts),
                    "protected_bytes": sum(int(item.get("size") or 0) for item in artifacts if item.get("pinned") or item.get("type") in {"source_media", "approval_snapshot", "formal_output", "manifest", "qa_report", "qa_evidence"}),
                    "pinned_count": sum(1 for item in artifacts if item.get("pinned")),
                    "free_bytes": free_disk_bytes(project_dir(cfg, project_id)),
                    "recovered_count": int(inventory.get("recovered") or 0),
                })
            elif parsed.path == "/api/videos":
                self._json(video_list(cfg, db))
            elif parsed.path == "/api/bgm":
                self._json(list_bgm(db))
            elif parsed.path == "/api/jobs":
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                jobs = project_jobs(project_id, render_manager, db) if project_id else project_jobs(0, render_manager, db)
                if query.get("meta", ["0"])[0] == "1":
                    row = db_project(db, project_id) if project_id else None
                    revision = int(row["project_revision"] or 1) if row else None
                    previous = query.get("since_revision", [""])[0]
                    try:
                        project_changed = revision is not None and previous != "" and int(previous) < revision
                    except (TypeError, ValueError):
                        project_changed = False
                    self._json({"jobs": jobs, "project_revision": revision, "project_changed": project_changed})
                else:
                    self._json(jobs)
            elif parsed.path == "/api/render-job":
                self._json(render_api.get(query.get("id", [""])[0]))
            elif parsed.path == "/api/render-job/report":
                self._json(render_api.report(query.get("id", [""])[0]))
            elif parsed.path == "/api/render-jobs":
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                result = render_api.list(project_id or None)
                self._json(result)
            elif parsed.path == "/api/project/delivery-qa":
                self._json(delivery_qa_for_api(cfg, int(query.get("project_id", ["0"])[0] or 0)))
            elif parsed.path == "/api/project/delivery-qa/artifact":
                try:
                    self._file(delivery_qa_artifact_path(
                        cfg,
                        int(query.get("project_id", ["0"])[0] or 0),
                        query.get("run_uuid", [""])[0],
                        query.get("artifact_id", [""])[0],
                    ))
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/delivery-qa/output":
                try:
                    self._file(delivery_qa_output_path(
                        cfg,
                        int(query.get("project_id", ["0"])[0] or 0),
                        query.get("run_uuid", [""])[0],
                    ))
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/color-preview-file":
                try:
                    path = preview_file_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/audio-preview-file":
                try:
                    path = audio_preview_file_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/storyboard-thumbnail-file":
                try:
                    path = storyboard_thumbnail_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/storyboard-preview-file":
                try:
                    path = storyboard_preview_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/visual-style-preview-file":
                try:
                    path = visual_style_preview_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError, VisualStyleError):
                    self.send_error(404)
            elif parsed.path == "/api/project/color-reference-file":
                try:
                    path = reference_file_path(cfg, int(query.get("project_id", ["0"])[0] or 0), query.get("file", [""])[0])
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/media":
                try:
                    path = registered_project_media_path(
                        cfg,
                        db,
                        int(query.get("project_id", ["0"])[0] or 0),
                        query.get("media_id", [""])[0],
                    )
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif parsed.path == "/api/project/perception-evidence":
                try:
                    path = perception_evidence_path(
                        cfg,
                        db,
                        int(query.get("project_id", ["0"])[0] or 0),
                        query.get("run_uuid", [""])[0],
                        query.get("window_uuid", [""])[0],
                        query.get("file", [""])[0],
                    )
                    self._file(path)
                except (FileNotFoundError, ValueError):
                    self.send_error(404)
            elif _web_dist().exists() and _static_file(parsed.path):
                self._file(_static_file(parsed.path))
            else:
                self.send_error(404)

        def do_HEAD(self) -> None:
            self._handle_get(head_only=True)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/ui/"):
                self._ui_post(parsed.path)
                return
            try:
                self._check_mutation()
                if parsed.path == "/api/upload":
                    self._json(self._bounded_upload(lambda: upload(self, cfg)))
                    return
                if parsed.path == "/api/project/upload":
                    self._json(self._bounded_upload(lambda: upload_project(self, cfg, db)))
                    return
                if parsed.path == "/api/upload-bgm":
                    self._json(self._bounded_upload(lambda: upload_bgm(self, cfg, db)))
                    return
                data = self._json_body()
                self._api_post(parsed.path, data)
            except ProjectRevisionConflict as exc:
                self._json({
                    "ok": False,
                    "code": exc.code,
                    "error": str(exc),
                    "project_revision": exc.current,
                    "current_revision": exc.current,
                }, status=409)
            except CreatorProfileRevisionConflict as exc:
                self._json({
                    "ok": False,
                    "code": exc.code,
                    "error": str(exc),
                    "profile_version": exc.current,
                }, status=409)
            except StorySettingsRevisionConflict as exc:
                self._json({
                    "ok": False,
                    "code": exc.code,
                    "error": str(exc),
                    "expected_version": exc.expected,
                    "profile_version": exc.current,
                    "current_version": exc.current,
                }, status=409)
            except QAReviewVersionConflict as exc:
                self._json(exc.as_dict(), status=409)
            except DeliveryQAError as exc:
                self._json(exc.as_dict(), status=exc.status)
            except WebSecurityError as exc:
                self._json(exc.as_dict(), status=403)
            except RetentionError as exc:
                self._json(exc.as_dict(), status=409 if exc.code == "stale_cleanup_plan" else 400)

        def _ui_post(self, path: str) -> None:
            if path in {"/ui/upload-project", "/ui/upload-bgm"}:
                if path == "/ui/upload-project":
                    result = self._bounded_upload(lambda: upload_project(self, cfg, db))
                    project_id = int(result.get("project_id") or 0)
                    if not result.get("ok", True):
                        self._redirect(project_id, str(result.get("error") or "上傳失敗"))
                        return
                    self._redirect(project_id, f"已匯入 {len(result.get('files', []))} 支素材")
                else:
                    result = self._bounded_upload(lambda: upload_bgm(self, cfg, db))
                    if not result.get("ok", True):
                        self._redirect(0, str(result.get("error") or "上傳失敗"))
                        return
                    self._redirect(0, "BGM 已登錄")
                return

            data = self._form_body()
            try:
                self._check_mutation(data.get("csrf_token"))
            except WebSecurityError as exc:
                self._redirect(project_id if "project_id" in locals() else 0, f"安全性驗證失敗：{exc.message}")
                return
            project_id = int(data.get("project_id", "0") or 0)
            try:
                if path == "/ui/create":
                    project_id = create_project(db, data.get("name", ""), [], category=data.get("category", "unknown"), content_type=data.get("content_type", "diary_montage"), platform="YouTube")
                    sync_project_files(cfg, db, project_id)
                    self._redirect(project_id, "專案已建立")
                elif path == "/ui/analyze-project":
                    started = start_analyze_job(cfg, db, project_id, data.get("force") == "1")
                    self._redirect(project_id, "內容感知已開始" if started else "內容感知已在執行中")
                elif path == "/ui/analyze-video":
                    started = start_analyze_video_job(cfg, db, project_id, int(data.get("video_id", 0)))
                    self._redirect(project_id, "單支素材感知已開始" if started else "內容感知已在執行中")
                elif path == "/ui/build-plan":
                    build_project_plan(cfg, db, project_id)
                    self._redirect(project_id, "故事整理已更新")
                elif path == "/ui/approve":
                    set_review_status(cfg, db, project_id, "approved", data.get("notes", ""))
                    self._redirect(project_id, "專案已核准")
                elif path == "/ui/reject":
                    set_review_status(cfg, db, project_id, "rejected", data.get("notes", ""))
                    self._redirect(project_id, "專案已退回")
                elif path == "/ui/color-preview":
                    started = start_color_job(cfg, db, project_id, data.get("mode") or cfg.get("color", {}).get("default_mode", "safe_restore"))
                    self._redirect(project_id, "調色預覽已開始" if started else "調色預覽已在執行中")
                elif path == "/ui/opencut-export":
                    render_clips = data.get("render_clips") == "1"
                    if render_clips:
                        ok, reason = can_project_render(cfg, db, project_id)
                        if not ok:
                            self._redirect(project_id, f"正式輸出被擋下：{reason}")
                            return
                    out = export_opencut_handoff(cfg, db, project_id, render_clips, int(data.get("max_segments", 20)))
                    self._redirect(project_id, f"OpenCut 匯出完成：{out}")
                elif path == "/ui/opencut-handoff":
                    started = start_opencut_job(
                        cfg,
                        db,
                        project_id,
                        data.get("render_clips") == "1",
                        int(data.get("max_segments", 20)),
                        confirm_local_action=data.get("confirm_local_action") == "1",
                    )
                    self._redirect(project_id, "正在準備 OpenCut" if started else "OpenCut 準備工作已在執行中")
                elif path == "/ui/opencut-folder":
                    _require_local_action_confirmation(data)
                    out = Path(data.get("folder", ""))
                    if out.exists():
                        _open_folder(cfg, project_id, out, "open_opencut_folder")
                    self._redirect(project_id, f"已打開資料夾：{out}" if out.exists() else "找不到 OpenCut 素材包，請先準備素材")
                elif path == "/ui/opencut-start":
                    _require_local_action_confirmation(data)
                    _audit_local_action(cfg, project_id, "start_opencut", OPENCUT_URL)
                    status = start_opencut()
                    self._redirect(project_id, "OpenCut 已啟動" if status.get("running") else f"OpenCut 啟動失敗：{status.get('error', '')}")
                elif path == "/ui/hyperframes-export":
                    if data.get("render") == "1":
                        ok, reason = can_project_render(cfg, db, project_id)
                        if not ok:
                            self._redirect(project_id, f"正式輸出被擋下：{reason}")
                            return
                    started = start_hyperframes_job(
                        cfg,
                        db,
                        project_id,
                        data.get("render") == "1",
                        int(data.get("max_segments", 20)),
                        confirm_local_action=data.get("confirm_local_action") == "1",
                    )
                    self._redirect(project_id, "正在產生 HyperFrames 初剪" if started else "HyperFrames 工作已在執行中")
                elif path == "/ui/hyperframes-folder":
                    _require_local_action_confirmation(data)
                    out = project_dir(cfg, project_id) / "output" / "hyperframes"
                    if out.exists():
                        _open_folder(cfg, project_id, out, "open_hyperframes_folder")
                    self._redirect(project_id, f"已打開資料夾：{out}" if out.exists() else "尚未產生 HyperFrames 專案")
                elif path == "/ui/stop-jobs":
                    stop_project_jobs(project_id, render_manager)
                    self._redirect(project_id, "已停止目前背景工作")
                elif path == "/ui/render-job":
                    result = render_api.create(project_id, data.get("output_path", ""))
                    self._redirect(project_id, "正式輸出已排入工作佇列" if result.get("created") else str(result.get("error") or "正式輸出工作已在執行中"))
                elif path == "/ui/project-bgm":
                    add_project_bgm(db, project_id, int(data.get("bgm_id", 0)))
                    mark_project_needs_review(cfg, db, project_id)
                    self._redirect(project_id, "BGM 已加入本專案，專案已回到待審")
                else:
                    self.send_error(404)
            except Exception as exc:
                self._redirect(project_id, f"操作失敗：{exc}")

        def _api_post(self, path: str, data: dict) -> None:
            if path == "/api/process-inbox":
                self._json(process_inbox(cfg, db))
            elif path == "/api/project/analyze":
                self._json(analyze_project(cfg, db, int(data.get("project_id", 0)), bool(data.get("force")), base_revision=_base_revision(data)))
            elif path == "/api/project/analyze-job":
                project_id = int(data.get("project_id", 0))
                started = start_analyze_job(cfg, db, project_id, bool(data.get("force")), base_revision=_base_revision(data))
                self._json({"ok": started, "message": "內容感知已開始" if started else "內容感知已在執行中"})
            elif path == "/api/project/analyze-video":
                project_id = int(data.get("project_id", 0))
                try:
                    sampling_override = _sampling_override(data)
                except (TypeError, ValueError) as exc:
                    self._json(
                        {
                            "ok": False,
                            "code": "invalid_sampling",
                            "error": str(exc),
                        },
                        status=400,
                    )
                    return
                started = start_analyze_video_job(
                    cfg,
                    db,
                    project_id,
                    int(data.get("video_id", 0)),
                    base_revision=_base_revision(data),
                    sampling_override=sampling_override,
                )
                self._json({"ok": started, "message": "單支素材感知已開始" if started else "內容感知已在執行中"})
            elif path == "/api/project/cloud-review/plan":
                try:
                    project_id = int(data.get("project_id", 0))
                    self._json(_cloud_review_plan(cfg, db, project_id, data))
                except (OSError, TypeError, ValueError, CloudReviewError) as exc:
                    self._json({"ok": False, "code": "cloud_review_plan_invalid", "error": str(exc)})
            elif path == "/api/project/cloud-review":
                try:
                    project_id = int(data.get("project_id", 0))
                    self._json(_cloud_review_execute(cfg, db, project_id, data))
                except ProjectRevisionConflict:
                    raise
                except (OSError, TypeError, ValueError, CloudReviewError) as exc:
                    self._json({"ok": False, "code": "cloud_review_failed", "error": str(exc), "local_result_preserved": True})
            elif path == "/api/project/clip-summary":
                project_id = int(data.get("project_id", 0))
                user_summary = data.get("user_summary", data.get("summary", ""))
                ok = update_clip_summary(cfg, db, project_id, int(data.get("video_id", 0)), str(user_summary), base_revision=_base_revision(data))
                self._json({"ok": ok, "plan_rebuilt": ok})
            elif path == "/api/projects":
                project_id = create_project(db, data.get("name", ""), [int(v) for v in data.get("video_ids", [])], category=data.get("category", "unknown"), content_type=data.get("content_type", "diary_montage"), platform=data.get("platform", "YouTube"), target_duration_seconds=float(data.get("target_duration_seconds") or 0))
                sync_project_files(cfg, db, project_id)
                self._json({"ok": True, "id": project_id})
            elif path == "/api/project/build-plan":
                project_id = int(data.get("project_id", 0))
                self._json({"ok": True, "plan": build_project_plan(cfg, db, project_id, base_revision=_base_revision(data))})
            elif path == "/api/project/revise":
                project_id = int(data.get("project_id", 0))
                self._json({"ok": True, "plan": revise_project(cfg, db, project_id, data.get("notes", ""), base_revision=_base_revision(data))})
            elif path == "/api/project/segments":
                try:
                    project_id = int(data.get("project_id", 0))
                    self._json({"ok": True, "path": str(save_segment_review(cfg, db, project_id, data.get("segments", []), base_revision=_base_revision(data)))})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_segment_review", "error": str(exc)})
            elif path == "/api/project/segment-timing":
                try:
                    project_id = int(data.get("project_id", 0))
                    output_path = update_segment_timing(
                        cfg,
                        db,
                        project_id,
                        str(data.get("segment_id") or ""),
                        float(data.get("start_seconds")),
                        float(data.get("end_seconds")),
                        float(data.get("speed")),
                        base_revision=_base_revision(data),
                    )
                    self._json({"ok": True, "path": str(output_path)})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_segment_timing", "error": str(exc)})
            elif path == "/api/project/segment-evidence":
                try:
                    project_id = int(data.get("project_id", 0))
                    output_path = update_segment_evidence(
                        cfg,
                        db,
                        project_id,
                        str(data.get("segment_id") or ""),
                        data.get("patch") if isinstance(data.get("patch"), dict) else {},
                        base_revision=_require_api_base_revision(data),
                    )
                    self._json({"ok": True, "path": str(output_path)})
                except (OSError, TypeError, ValueError) as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "invalid_segment_evidence"), "error": str(exc)})
            elif path == "/api/project/storyboard":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = update_storyboard(cfg, db, project_id, data.get("state", data), return_result=True, base_revision=_base_revision(data))
                    self._json({"ok": True, "storyboard": result["state"], "render_changed": result["render_changed"], "approval_invalidated": result["approval_invalidated"]})
                except (TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_storyboard", "error": str(exc)})
            elif path == "/api/project/storyboard/generate":
                try:
                    project_id = int(data.get("project_id", 0))
                    state = generate_storyboard(cfg, db, project_id, force=bool(data.get("force")), base_revision=_base_revision(data))
                    self._json({"ok": True, "storyboard": storyboard_for_api(cfg, db, project_id), "state": state})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "storyboard_generation_failed", "error": str(exc)})
            elif path == "/api/project/story/settings":
                try:
                    project_id = int(data.get("project_id", 0))
                    if "creator_profile" in data:
                        raise ValueError("Creator Profile 必須透過 /api/creator-profile 獨立儲存")
                    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
                    expected = data.get("expected_version", patch.get("profile_version"))
                    expected_version = int(expected) if expected not in (None, "") else None
                    settings = save_project_story_settings(cfg, db, project_id, patch, base_revision=_base_revision(data), expected_version=expected_version) if "settings" in data else load_project_story_settings(cfg, db, project_id)
                    self._json({"ok": True, "settings": settings, "profile_version": settings.get("profile_version"), "project_revision": current_revision(db, project_id)})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_story_settings", "error": str(exc)})
            elif path == "/api/project/creative-brief/recommend":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = recommend_creative_brief(cfg, db, project_id, base_revision=_base_revision(data))
                    self._json({"ok": True, "creative_brief": creative_brief_api_payload(result), "project_revision": current_revision(db, project_id)})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "creative_brief_recommendation_failed", "error": str(exc)})
            elif path == "/api/project/creative-brief":
                try:
                    project_id = int(data.get("project_id", 0))
                    brief = data.get("brief") if isinstance(data.get("brief"), dict) else data
                    approval_source = str(data.get("approval_source") or "human_override")
                    result = save_approved_creative_brief(cfg, db, project_id, brief, base_revision=_base_revision(data), approval_source=approval_source)
                    self._json({"ok": True, "creative_brief": creative_brief_api_payload(result), "project_revision": current_revision(db, project_id)})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_creative_brief", "error": str(exc)})
            elif path == "/api/project/visual-style/preview":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = preview_visual_styles(cfg, db, project_id, force=bool(data.get("force")))
                    for variant in result.get("variants", []):
                        filename = Path(str(variant.get("file") or "")).name
                        variant["file"] = filename
                        variant["url"] = f"/api/project/visual-style-preview-file?project_id={project_id}&file={filename}"
                    if result.get("file"):
                        result["file"] = Path(str(result["file"])).name
                    self._json(result)
                except (OSError, TypeError, ValueError, VisualStyleError) as exc:
                    self._json({"ok": False, "code": getattr(exc, "code", "visual_style_preview_failed"), "error": str(exc)})
            elif path == "/api/project/visual-style":
                try:
                    project_id = int(data.get("project_id", 0))
                    payload = data.get("visual_style") if isinstance(data.get("visual_style"), dict) else data
                    result = save_visual_style_approval(cfg, db, project_id, payload, base_revision=_base_revision(data))
                    self._json({"ok": True, "visual_style": visual_style_api_payload(result), "project_revision": current_revision(db, project_id)})
                except (OSError, TypeError, ValueError, VisualStyleError) as exc:
                    self._json({"ok": False, "code": getattr(exc, "code", "invalid_visual_style"), "error": str(exc)})
            elif path == "/api/creator-profile":
                try:
                    profile = data.get("profile") if isinstance(data.get("profile"), dict) else data
                    expected = data.get("expected_version", profile.get("profile_version")) if isinstance(profile, dict) else None
                    saved = save_creator_profile(cfg, profile, expected_version=int(expected) if expected not in (None, "") else None)
                    self._json({"ok": True, "creator_profile": saved, "profile_version": saved.get("profile_version")})
                except CreatorProfileRevisionConflict:
                    raise
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "invalid_creator_profile", "error": str(exc)})
            elif path == "/api/project/story/generate":
                try:
                    project_id = int(data.get("project_id", 0))
                    creative_brief = ensure_creative_brief(cfg, db, project_id)
                    if creative_brief.get("status") != "approved":
                        self._json({
                            "ok": False,
                            "code": "creative_brief_required",
                            "error": "請先在人工作業 checkpoint 核准 Creative Brief，再生成 Story",
                            "creative_brief": creative_brief_api_payload(creative_brief),
                        }, status=409)
                        return
                    result = generate_project_story(cfg, db, project_id, base_revision=_base_revision(data), provider_override=str(data.get("provider") or "") or None, force=bool(data.get("force")))
                    self._json({"ok": True, "generation": result, "story": project_story_detail(cfg, db, project_id)})
                except ProjectRevisionConflict:
                    raise
                except (OSError, TypeError, ValueError, StoryGenerationError) as exc:
                    self._json({"ok": False, "code": "story_generation_failed", "error": str(exc)})
            elif path == "/api/project/story/review":
                try:
                    generation_uuid = str(data.get("story_generation_uuid") or "")
                    result = update_story_generation_review(
                        db,
                        generation_uuid,
                        data.get("review") if isinstance(data.get("review"), dict) else {},
                        project_id=int(data.get("project_id", 0)),
                        base_revision=_base_revision(data),
                    )
                    self._json({"ok": True, "generation": result})
                except (OSError, TypeError, ValueError, StoryGenerationError) as exc:
                    self._json({"ok": False, "code": "invalid_story_review", "error": str(exc)})
            elif path == "/api/project/story/apply":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = apply_story_generation_to_storyboard(cfg, db, project_id, str(data.get("story_generation_uuid") or ""), base_revision=_base_revision(data))
                    self._json({"ok": True, **result})
                except StoryGenerationError as exc:
                    code = "stale_story_generation" if "input_hash 已過期" in str(exc) else "story_apply_failed"
                    self._json({"ok": False, "code": code, "error": str(exc)})
                except ProjectRevisionConflict:
                    raise
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "story_apply_failed", "error": str(exc)})
            elif path == "/api/project/story/calibration":
                try:
                    project_id = int(data.get("project_id", 0))
                    settings = load_project_story_settings(cfg, db, project_id)
                    profile_id = str(data.get("profile_id") or settings.get("profile_id") or "general_diary")
                    action = str(data.get("action") or "recalculate")
                    if action == "reset":
                        reset_calibration(cfg, profile_id)
                        result = {"status": "reset", "profile_id": profile_id}
                    else:
                        result = recalculate_calibration(cfg, db, profile_id)
                    self._json({"ok": True, "calibration": result})
                except (OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "calibration_failed", "error": str(exc)})
            elif path == "/api/project/storage/reconcile":
                project_id = int(data.get("project_id", 0))
                self._json(reconcile_inventory(cfg, project_id))
            elif path == "/api/project/storage/plan":
                project_id = int(data.get("project_id", 0))
                active_ids = {str(job.get("job_id")) for job in project_jobs(project_id, render_manager, db) if job.get("status") in {"queued", "running", "cancelling"} and job.get("job_id")}
                self._json({"ok": True, "plan": build_cleanup_plan(cfg, project_id, data.get("policy") if isinstance(data.get("policy"), dict) else None, active_job_ids=active_ids)})
            elif path == "/api/project/storage/cleanup":
                project_id = int(data.get("project_id", 0))
                plan_id = Path(str(data.get("plan_id") or "")).name
                plan_path = project_dir(cfg, project_id) / "storage" / f"{plan_id}.json"
                if not plan_id or plan_path.parent.resolve() not in plan_path.resolve().parents or not plan_path.is_file():
                    raise RetentionError("invalid_cleanup_plan", "找不到指定 cleanup plan", action="重新建立 dry-run plan")
                active_ids = {str(job.get("job_id")) for job in project_jobs(project_id, render_manager, db) if job.get("status") in {"queued", "running", "cancelling"} and job.get("job_id")}
                self._json(execute_cleanup_plan(cfg, json.loads(plan_path.read_text(encoding="utf-8")), active_job_ids=active_ids))
            elif path == "/api/project/storage/pin":
                project_id = int(data.get("project_id", 0))
                self._json({"ok": True, "artifact": set_artifact_pinned(cfg, project_id, str(data.get("artifact_id") or ""), bool(data.get("pinned")))})
            elif path == "/api/project/storyboard/thumbnail":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = generate_thumbnail(cfg, db, project_id, str(data.get("segment_id") or ""), float(data.get("ratio", 0.5)), force=bool(data.get("force")))
                    result["url"] = f"/api/project/storyboard-thumbnail-file?project_id={project_id}&file={result['file']}"
                    self._json({"ok": True, **result})
                except (OSError, TypeError, ValueError, RuntimeError) as exc:
                    self._json({"ok": False, "code": "thumbnail_failed", "error": str(exc)})
            elif path == "/api/project/storyboard/preview":
                try:
                    project_id = int(data.get("project_id", 0))
                    result = render_storyboard_preview(
                        cfg,
                        db,
                        project_id,
                        mode=str(data.get("mode") or "range"),
                        segment_id=str(data.get("segment_id") or "") or None,
                        duration_seconds=float(data.get("duration_seconds") or 8),
                        timeline_start_seconds=float(data.get("timeline_start_seconds") or 0),
                        storyboard_state=data.get("storyboard_state") if isinstance(data.get("storyboard_state"), dict) else None,
                        force=bool(data.get("force")),
                    )
                    for preview in result.get("previews", []):
                        preview["url"] = f"/api/project/storyboard-preview-file?project_id={project_id}&file={preview['file']}"
                    if result.get("file"):
                        result["url"] = f"/api/project/storyboard-preview-file?project_id={project_id}&file={result['file']}"
                    self._json(result)
                except (AudioPreviewError, StoryboardPreviewError, OSError, TypeError, ValueError) as exc:
                    self._json({"ok": False, "code": "storyboard_preview_failed", "error": str(exc)})
            elif path == "/api/project/audio-settings":
                try:
                    project_id = int(data.get("project_id", 0))
                    patch = data.get("patch") if isinstance(data.get("patch"), dict) else data.get("state", {})
                    selected = (patch.get("bgm") or {}).get("bgm_id") if isinstance(patch, dict) and isinstance(patch.get("bgm"), dict) else None
                    if selected is not None:
                        selected_id = int(selected)
                        track = next((dict(row) for row in db_bgm_tracks(db) if int(row["id"]) == selected_id), None)
                        if not track:
                            raise ValueError("找不到指定 BGM")
                        validate_bgm_track({"source_path": track.get("file_path")}, str(cfg.get("ffprobe_path") or "ffprobe"))
                    state = update_audio_state(cfg, db, project_id, patch if isinstance(patch, dict) else {}, base_revision=_base_revision(data))
                    self._json({"ok": True, "state": audio_state_for_api(cfg, project_id, db)})
                except BgmPipelineError as exc:
                    self._json({"ok": False, "code": "bgm_file_missing", "error": "所選 BGM 檔案不存在或無法讀取，請重新匯入或選擇其他音樂。"})
                except ValueError as exc:
                    message = "找不到指定 BGM，請重新選擇。" if "找不到指定 BGM" in str(exc) else "音訊設定格式無效。"
                    self._json({"ok": False, "code": "bgm_not_found" if "找不到指定 BGM" in str(exc) else "invalid_audio_settings", "error": message})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "error": f"音訊設定儲存失敗：{exc}"})
            elif path == "/api/project/audio-preview":
                try:
                    result = render_project_audio_preview(
                        cfg,
                        db,
                        int(data.get("project_id", 0)),
                        segment_id=str(data.get("segment_id") or "") or None,
                        timeline_start_seconds=float(data.get("timeline_start_seconds") or 0),
                        duration_seconds=float(data.get("duration_seconds") or 12),
                        audio_patch=data.get("patch") if isinstance(data.get("patch"), dict) else None,
                        force=bool(data.get("force")),
                    )
                    result["url"] = f"/api/project/audio-preview-file?project_id={int(data.get('project_id', 0))}&file={result['file']}"
                    self._json(result)
                except AudioPreviewError as exc:
                    message = str(exc)
                    if "BGM" in message or "bgm" in message.lower():
                        self._json({"ok": False, "code": "bgm_file_missing", "error": "所選 BGM 檔案不存在或無法讀取，請重新匯入或選擇其他音樂。"})
                    else:
                        self._json({"ok": False, "code": "audio_preview_failed", "error": "音訊預覽無法產生，請檢查素材與音訊設定。"})
            elif path == "/api/project/bgm":
                project_id = int(data.get("project_id", 0))
                with project_commit(db, project_id, _base_revision(data)) as commit:
                    before_tracks = {int(row["id"]) for row in project_bgm_tracks(db, project_id)}
                    add_project_bgm(db, project_id, int(data.get("bgm_id", 0)))
                    changed = before_tracks != {int(row["id"]) for row in project_bgm_tracks(db, project_id)}
                    commit.record_changed(changed)
                    if changed:
                        mark_project_needs_review(cfg, db, project_id)
                self._json({"ok": True})
            elif path == "/api/project/color-preview":
                try:
                    self._json(color_preview_project(cfg, db, int(data.get("project_id", 0)), data.get("mode") or cfg.get("color", {}).get("default_mode", "safe_restore"), force=bool(data.get("force")), base_revision=_require_api_base_revision(data)))
                except ColorPipelineError as exc:
                    self._json({"ok": False, "code": "missing_lut" if "LUT file does not exist" in str(exc) else "color_pipeline_error", "error": str(exc)})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "color_preview_failed"), "error": f"調色預覽失敗：{exc}"})
            elif path == "/api/project/color-job":
                project_id = int(data.get("project_id", 0))
                try:
                    started = start_color_job(cfg, db, project_id, data.get("mode") or cfg.get("color", {}).get("default_mode", "safe_restore"), base_revision=_require_api_base_revision(data))
                    self._json({"ok": started, "message": "調色預覽已開始" if started else "調色預覽已在執行中"})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "color_job_failed"), "error": str(exc)})
            elif path == "/api/project/color-analyze":
                try:
                    state = analyze_project_color(cfg, db, int(data.get("project_id", 0)), force=bool(data.get("force")), base_revision=_require_api_base_revision(data))
                    self._json({"ok": True, "state": color_state_for_api(cfg, int(data.get("project_id", 0)), state)})
                except ColorReferenceError as exc:
                    self._json({"ok": False, "code": exc.code, "error": str(exc), "warnings": [str(exc)]})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "color_analysis_failed"), "error": f"色彩分析失敗：{exc}"})
            elif path == "/api/project/color-settings":
                try:
                    project_id = int(data.get("project_id", 0))
                    patch = data.get("state") if isinstance(data.get("state"), dict) else data.get("patch", {})
                    state = update_color_state(cfg, db, project_id, patch if isinstance(patch, dict) else {}, base_revision=_require_api_base_revision(data))
                    self._json({"ok": True, "state": color_state_for_api(cfg, int(data.get("project_id", 0)), state)})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "color_settings_failed"), "error": f"色彩設定儲存失敗：{exc}"})
            elif path == "/api/project/color-reference":
                try:
                    state = set_color_reference(cfg, db, int(data.get("project_id", 0)), str(data.get("reference_id", "")), base_revision=_require_api_base_revision(data))
                    self._json({"ok": True, "state": color_state_for_api(cfg, int(data.get("project_id", 0)), state)})
                except ColorReferenceError as exc:
                    self._json({"ok": False, "code": exc.code, "error": str(exc)})
                except Exception as exc:
                    if isinstance(exc, ProjectRevisionConflict):
                        raise
                    self._json({"ok": False, "code": getattr(exc, "code", "color_reference_failed"), "error": f"色彩基準更新失敗：{exc}"})
            elif path == "/api/project/opencut-export":
                project_id = int(data.get("project_id", 0))
                if bool(data.get("render_clips")):
                    ok, reason = can_project_render(cfg, db, project_id)
                    if not ok:
                        self._json({"ok": False, "error": f"正式輸出被擋下：{reason}"})
                        return
                out = export_opencut_handoff(
                    cfg,
                    db,
                    project_id,
                    bool(data.get("render_clips")),
                    int(data.get("max_segments", 20)),
                    base_revision=_base_revision(data),
                )
                self._json({"ok": True, "folder": str(out)})
            elif path == "/api/project/opencut-job":
                project_id = int(data.get("project_id", 0))
                started = start_opencut_job(
                    cfg,
                    db,
                    project_id,
                    bool(data.get("render_clips")),
                    int(data.get("max_segments", 20)),
                    base_revision=_base_revision(data),
                    confirm_local_action=bool(data.get("confirm_local_action")),
                )
                self._json({"ok": started, "message": "OpenCut 工作已開始" if started else "OpenCut 工作已在執行中"})
            elif path == "/api/project/hyperframes-export":
                project_id = int(data.get("project_id", 0))
                render = bool(data.get("render"))
                if render:
                    ok, reason = can_project_render(cfg, db, project_id)
                    if not ok:
                        self._json({"ok": False, "error": f"正式輸出被擋下：{reason}"})
                        return
                out = export_hyperframes_project(
                    cfg,
                    db,
                    project_id,
                    render,
                    int(data.get("max_segments", 20)),
                    base_revision=_base_revision(data),
                )
                result = render_fast_draft(out, cfg, db=db, project_id=project_id) if render else None
                if result and not result["ok"]:
                    self._json({"ok": False, "folder": str(out), "error": f"快速輸出 MP4 失敗：{result['stderr'][-500:]}"})
                    return
                self._json({"ok": True, "folder": str(out), "output": result["output"] if result else ""})
            elif path == "/api/project/hyperframes-job":
                project_id = int(data.get("project_id", 0))
                started = start_hyperframes_job(
                    cfg,
                    db,
                    project_id,
                    bool(data.get("render")),
                    int(data.get("max_segments", 20)),
                    base_revision=_base_revision(data),
                    confirm_local_action=bool(data.get("confirm_local_action")),
                )
                self._json({"ok": started, "message": "HyperFrames 工作已開始" if started else "HyperFrames 工作已在執行中"})
            elif path == "/api/project/stop-jobs":
                stop_project_jobs(int(data.get("project_id", 0)), render_manager)
                self._json({"ok": True, "message": "已停止目前背景工作"})
            elif path == "/api/project/legacy-job/cancel":
                self._json(cancel_legacy_job(int(data.get("project_id", 0)), str(data.get("legacy_job_key", ""))))
            elif path == "/api/project/render-job":
                self._json(render_api.create(int(data.get("project_id", 0)), str(data.get("output_path", ""))))
            elif path == "/api/render-job/cancel":
                self._json(render_api.cancel(str(data.get("job_id", ""))))
            elif path == "/api/project/delivery-qa/review":
                project_id = int(data.get("project_id", 0))
                result = review_delivery_qa(
                    cfg,
                    project_id,
                    str(data.get("qa_run_uuid") or ""),
                    action=str(data.get("action") or ""),
                    expected_version=int(data.get("expected_version") or 0),
                    reason=str(data.get("reason") or ""),
                    warning_acceptances=data.get("warning_acceptances") if isinstance(data.get("warning_acceptances"), dict) else {},
                )
                self._json({"ok": True, "delivery_qa": result})
            elif path == "/api/project/delivery-qa/rerun":
                project_id = int(data.get("project_id", 0))
                self._json({"ok": True, "delivery_qa": rerun_current_delivery_qa(cfg, db, project_id)})
            elif path == "/api/project/approve":
                self._json(_approve_project_api(cfg, db, data))
            elif path == "/api/project/reject":
                set_review_status(cfg, db, int(data.get("project_id", 0)), "rejected", data.get("notes", ""), base_revision=_base_revision(data))
                self._json({"ok": True})
            elif path == "/api/project/render-dry-run":
                ok, reason = can_project_render(cfg, db, int(data.get("project_id", 0)))
                self._json({"ok": ok, "message": "已核准，可以進入輸出檢查" if ok else reason})
            else:
                self._video_post(path, data, db)

        def _video_post(self, path: str, data: dict, db: Path) -> None:
            video_id = int(data.get("video_id", 0))
            if path == "/api/approve":
                set_plan_status(cfg, video_id, "approved")
                set_video_status(db, video_id, "approved")
                self._json({"ok": True})
            elif path == "/api/reject":
                set_plan_status(cfg, video_id, "rejected", data.get("notes", ""))
                set_video_status(db, video_id, "rejected")
                self._json({"ok": True})
            elif path == "/api/revise":
                (video_dir(cfg, video_id) / "revision_prompt.txt").write_text(data.get("notes", ""), encoding="utf-8")
                revise_plan(cfg, video_id)
                set_video_status(db, video_id, "needs_review")
                self._json({"ok": True})
            elif path == "/api/render-dry-run":
                out = render_approved(cfg, video_id, dry_run=True)
                self._json({"ok": bool(out), "output": str(out) if out else ""})
            else:
                self.send_error(404)

        def _form_body(self) -> dict:
            length = parse_content_length(
                self.headers.get("content-length"),
                maximum=MAX_FORM_BODY,
            )
            body = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[-1] for k, v in parse_qs(body).items()}

        def _json_body(self) -> dict:
            length = parse_content_length(
                self.headers.get("content-length"),
                maximum=MAX_JSON_BODY,
            )
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _bounded_upload(self, callback):
            if not UPLOAD_SEMAPHORE.acquire(blocking=False):
                return {
                    "ok": False,
                    "code": "upload_busy",
                    "error": "同時上傳數已達上限，請稍後重試",
                }
            try:
                return callback()
            finally:
                UPLOAD_SEMAPHORE.release()

        def _redirect(self, project_id: int = 0, message: str = "") -> None:
            query = {}
            if project_id:
                query["project_id"] = str(project_id)
            if message:
                query["message"] = message
            self.send_response(303)
            self.send_header("location", "/" + ("?" + urlencode(query) if query else ""))
            self.end_headers()

        def _json(self, data: object, *, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)

        def _html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)

        def _file(self, path: Path) -> None:
            try:
                size = path.stat().st_size
                requested = parse_single_range(self.headers.get("range"), size)
            except (OSError, WebSecurityError) as exc:
                if isinstance(exc, WebSecurityError):
                    self.send_response(416)
                    self.send_header("content-type", "application/json; charset=utf-8")
                    self.send_header("content-range", f"bytes */{path.stat().st_size if path.exists() else 0}")
                    body = json.dumps(exc.as_dict(), ensure_ascii=False).encode("utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    if not getattr(self, "_head_only", False):
                        self.wfile.write(body)
                    return
                self.send_error(404)
                return
            start, end = requested or (0, max(0, size - 1))
            status = 206 if requested else 200
            self.send_response(status)
            self.send_header("content-type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("cache-control", "no-store")
            self.send_header("accept-ranges", "bytes")
            self.send_header("content-length", str(end - start + 1 if size else 0))
            if requested:
                self.send_header("content-range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if getattr(self, "_head_only", False) or size == 0:
                return
            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(MEDIA_STREAM_CHUNK, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, fmt: str, *args) -> None:
            return

    print(f"video-vault-ai UI: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _web_dist() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def registered_project_media_path(cfg: dict, db: Path, project_id: int, media_id: str) -> Path:
    """Resolve only a source file registered in the requested project."""

    token = str(media_id or "").strip()
    if not token or any(char in token for char in ("/", "\\", "..")):
        raise ValueError("invalid project media id")
    source_dir = project_dir(cfg, int(project_id)) / "source"
    source_dir = source_dir.resolve()
    for row in project_videos(db, int(project_id)):
        row = dict(row)
        project_media_id = str(row.get("project_media_uuid") or f"video_{row.get('id')}")
        if project_media_id != token:
            continue
        source = Path(str(row.get("current_path") or "")).expanduser().resolve()
        candidate = source if source.parent == source_dir else source_dir / f"media_{project_media_id}_{source.name}"
        candidate = candidate.resolve()
        if source_dir not in candidate.parents or candidate.is_symlink() or not candidate.is_file():
            raise FileNotFoundError("registered project media is unavailable")
        return candidate
    raise FileNotFoundError("registered project media not found")


PERCEPTION_EVIDENCE_FILES = {
    "contact_sheet.jpg",
    "window.json",
    "validation.json",
    "normalized.json",
}


def perception_evidence_path(
    cfg: dict,
    db: Path,
    project_id: int,
    run_uuid: str,
    window_uuid: str,
    file_name: str,
) -> Path:
    """Resolve only an evidence artifact from a run belonging to this project."""

    run_token = str(run_uuid or "").strip()
    window_token = str(window_uuid or "").strip()
    file_token = str(file_name or "").strip()
    token_pattern = re.compile(r"^[A-Za-z0-9_-]+$")
    if not token_pattern.fullmatch(run_token) or not token_pattern.fullmatch(window_token):
        raise ValueError("invalid perception evidence identity")
    if file_token not in PERCEPTION_EVIDENCE_FILES:
        raise ValueError("invalid perception evidence file")
    run = analysis_run(db, run_token)
    if int(run.get("project_id") or 0) != int(project_id):
        raise FileNotFoundError("perception evidence is not in this project")
    persisted_staging = Path(str(run.get("staging_path") or "")).expanduser().resolve()
    staging_root = (Path(cfg["library_root"]) / "05_index" / "perception_runs").resolve()
    if not persisted_staging.is_dir() or staging_root not in persisted_staging.parents:
        raise FileNotFoundError("perception run staging directory is unavailable")
    root = (persisted_staging / "evidence").resolve()
    candidate = (root / window_token / file_token).resolve()
    if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
        raise FileNotFoundError("perception evidence not found")
    return candidate


def _project_detail_for_api(cfg: dict, detail: dict) -> dict:
    """Expose project-scoped media URLs without leaking local filesystem paths."""

    result = deepcopy(detail or {})
    project = result.get("project") or {}
    project_id = int(project.get("id") or 0)

    def media_url(media_id: object) -> str:
        return f"/api/project/media?project_id={project_id}&media_id={str(media_id or '')}"

    def evidence_url(run_uuid: str, window_uuid: str, file_name: str) -> str:
        query = urlencode({
            "project_id": project_id,
            "run_uuid": run_uuid,
            "window_uuid": window_uuid,
            "file": file_name,
        })
        return f"/api/project/perception-evidence?{query}"

    def scrub(value: object) -> object:
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if not isinstance(value, dict):
            return value
        cleaned = {}
        for key, item in value.items():
            if key in {"source_file", "source_path", "original_source_path", "file_path", "frame_path", "evidence"}:
                continue
            cleaned[key] = scrub(item)
        if "project_media_id" in cleaned and "media_url" not in cleaned:
            cleaned["media_url"] = media_url(cleaned["project_media_id"])
        return cleaned

    segment_by_window: dict[str, dict] = {}
    segment_by_uuid: dict[str, dict] = {}
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_uuid = str(segment.get("segment_uuid") or segment.get("segment_id") or "")
        window_uuid = str(segment.get("window_uuid") or "")
        if segment_uuid:
            segment_by_uuid[segment_uuid] = segment
        if window_uuid and window_uuid not in segment_by_window:
            segment_by_window[window_uuid] = segment

    for clip in result.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        clip["media_url"] = media_url(clip.get("project_media_id") or clip.get("video_id"))
        perception = clip.get("perception_run")
        if isinstance(perception, dict):
            run_uuid = str(perception.get("current_analysis_run_uuid") or "")
            public_perception = scrub(perception)
            public_results = []
            for item in perception.get("current_window_results") or []:
                if not isinstance(item, dict):
                    continue
                public_item = scrub(item)
                public_item.pop("evidence", None)
                window_uuid = str(item.get("window_uuid") or "")
                segment_uuid = str(item.get("segment_uuid") or "")
                segment = segment_by_uuid.get(segment_uuid) or segment_by_window.get(window_uuid)
                if not segment_uuid and segment:
                    segment_uuid = str(segment.get("segment_uuid") or segment.get("segment_id") or "")
                    if segment_uuid:
                        public_item["segment_uuid"] = segment_uuid
                if segment:
                    for key in ("include", "user_notes", "locked", "action", "shot_role", "technical_quality_json", "duplicate_group", "natural_audio_recommendation"):
                        if key not in public_item and key in segment:
                            public_item[key] = segment[key]
                if run_uuid and window_uuid:
                    public_item["evidence_urls"] = {
                        "contact_sheet": evidence_url(run_uuid, window_uuid, "contact_sheet.jpg"),
                        "window": evidence_url(run_uuid, window_uuid, "window.json"),
                        "validation": evidence_url(run_uuid, window_uuid, "validation.json"),
                        "normalized": evidence_url(run_uuid, window_uuid, "normalized.json"),
                    }
                public_results.append(public_item)
            public_perception["current_window_results"] = public_results
            clip["perception_run"] = public_perception
        for key in ("source_path", "original_source_path", "physical_filename"):
            clip.pop(key, None)
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment["media_url"] = media_url(segment.get("project_media_id") or segment.get("video_id"))
        segment.pop("source_file", None)

    result["plan"] = scrub(result.get("plan"))
    result["perception_runs"] = scrub(result.get("perception_runs"))
    result.pop("folder", None)
    return result


def _static_file(url_path: str) -> Path | None:
    dist = _web_dist().resolve()
    path = (dist / url_path.lstrip("/")).resolve()
    return path if path.exists() and path.is_file() and dist in path.parents else None


def render_page(cfg: dict, db: Path, project_id: int = 0, message: str = "", render_manager: RenderJobManager | None = None) -> str:
    projects = list_projects(db)
    if not project_id and projects:
        project_id = int(projects[0]["id"])
    detail = project_detail(cfg, db, project_id) if project_id else {}
    bgm = list_bgm(db)
    jobs = project_jobs(project_id, render_manager, db) if project_id else []
    refresh = '<meta http-equiv="refresh" content="3">' if any(j.get("status") in {"running", "queued"} for j in jobs) else ""
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>video-vault-ai</title>
  <style>{_css()}</style>
</head>
<body>
  <header>video-vault-ai 專案工作台</header>
  <main>
    <aside>{_nav()}{_create_project_form()}{_project_list(projects, project_id)}</aside>
    <section>
      {f'<div class="card"><b>{h(message)}</b></div>' if message else ''}
      {_jobs_panel(jobs)}
      {_project_panel(detail)}
      {_hyperframes_panel(detail)}
      <div class="grid">{_clips_panel(detail)}{_project_bgm_panel(project_id, detail, bgm)}</div>
      {_script_panel(detail)}
    </section>
  </main>
</body>
</html>"""
    return _inject_csrf_fields(html)


def render_bgm_page(db: Path, message: str = "") -> str:
    return _inject_csrf_fields(f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BGM 資料庫</title>
  <style>{_css()}</style>
</head>
<body>
  <header>video-vault-ai BGM 資料庫</header>
  <main>
    <aside>{_nav()}</aside>
    <section>
      {f'<div class="card"><b>{h(message)}</b></div>' if message else ''}
      {_bgm_library_panel(list_bgm(db))}
    </section>
  </main>
</body>
</html>""")


def _inject_csrf_fields(html: str) -> str:
    if not FORM_CSRF_TOKEN:
        return html
    hidden = f'<input type="hidden" name="csrf_token" value="{h(FORM_CSRF_TOKEN)}">'
    return re.sub(r"(<form\b[^>]*>)", rf"\1{hidden}", html)


def _nav() -> str:
    return '<div class="card"><h3>導覽</h3><a class="item" href="/">專案工作台</a><a class="item" href="/bgm">BGM 資料庫</a></div>'


def _enqueue_legacy_job(db: Path, project_id: int, kind: str, label: str, capabilities: set[str], resources: set[str], base_revision: int | None = None):
    coordinated = JOB_COORDINATOR.submit(project_id, kind, capabilities, current_revision(db, project_id) if base_revision is None else int(base_revision), resources=resources)
    JOB_TOKENS[(int(project_id), kind)] = CancellationToken()
    return coordinated


def _wait_for_coordinated_job(project_id: int, kind: str) -> bool:
    key = (int(project_id), str(kind))
    while True:
        with JOBS_LOCK:
            job = JOBS.get(key, {})
            coordinator_id = job.get("coordinator_job_id")
            token = JOB_TOKENS.get(key)
        if token is None or token.cancelled or _job_stopped(project_id, kind):
            return False
        coordinated = JOB_COORDINATOR.get(str(coordinator_id)) if coordinator_id else None
        if coordinated is None:
            return True
        if coordinated.state == JobState.QUEUED:
            with JOBS_LOCK:
                if key in JOBS:
                    JOBS[key].update(state=coordinated.state.value, queue_reason=coordinated.queue_reason, message=coordinated.queue_reason or "等待開始")
            time.sleep(0.1)
            try:
                JOB_COORDINATOR.start(coordinated.job_id)
            except RuntimeError:
                continue
            continue
        if coordinated.state == JobState.CANCELLED:
            return False
        with JOBS_LOCK:
            if key in JOBS:
                JOBS[key].update(state=coordinated.state.value, queue_reason="")
        return coordinated.state == JobState.RUNNING


def _finish_coordinated_job(project_id: int, kind: str, status: str, error: str = "") -> None:
    with JOBS_LOCK:
        coordinator_id = JOBS.get((int(project_id), str(kind)), {}).get("coordinator_job_id")
    if not coordinator_id:
        return
    if status in {"stopped", "cancelled", "cancelling"}:
        JOB_COORDINATOR.finish_cancel(str(coordinator_id))
    elif status in {"done", "succeeded", "completed"}:
        JOB_COORDINATOR.complete(str(coordinator_id))
    elif status == "superseded":
        JOB_COORDINATOR.supersede(str(coordinator_id), error)
    elif status in {"failed", "failed_qc"}:
        JOB_COORDINATOR.fail(str(coordinator_id), error)


def _css() -> str:
    return """
    body{margin:0;font-family:Segoe UI,'Microsoft JhengHei',sans-serif;background:#f5f7fa;color:#17212b}
    header{height:56px;display:flex;align-items:center;padding:0 20px;background:#17212b;color:white;font-weight:700}
    main{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 56px)} aside{background:white;border-right:1px solid #d8dee6;overflow:auto}
    section{padding:18px} .card{background:white;border:1px solid #d8dee6;border-radius:8px;padding:14px;margin-bottom:12px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px} .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center} .stack{display:grid;gap:8px}
    .item{display:block;padding:12px 14px;border-bottom:1px solid #edf0f3;color:inherit;text-decoration:none} .item:hover,.active{background:#edf6ff}
    button,input,select,textarea{font:inherit} button,.buttonlink{border:1px solid #b8c0cc;background:white;border-radius:6px;padding:8px 10px;cursor:pointer;color:inherit;text-decoration:none;display:inline-block}
    .primary{background:#1769aa;color:white;border-color:#1769aa} .good{background:#16794c;color:white;border-color:#16794c} .danger{background:#b42318;color:white;border-color:#b42318}
    input,select,textarea{border:1px solid #b8c0cc;border-radius:6px;padding:8px;box-sizing:border-box} textarea{width:100%;min-height:72px}
    .muted{font-size:13px;color:#64748b} .pill{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700} .ok{background:#dcfce7;color:#166534} .wait{background:#e2e8f0;color:#475569}
    .bar{display:block;width:100%;height:8px;background:#e2e8f0;border-radius:999px;overflow:hidden;margin-top:6px}.bar span{display:block;height:100%;background:#1769aa}
    pre{white-space:pre-wrap;background:#fbfcfd;border:1px solid #d8dee6;border-radius:8px;padding:12px;line-height:1.45;max-height:420px;overflow:auto}
    @media(max-width:900px){main{grid-template-columns:1fr} aside{max-height:320px} .grid{grid-template-columns:1fr}}
  """


def start_analyze_job(cfg: dict, db: Path, project_id: int, force: bool, *, base_revision: int | None = None) -> bool:
    key = (project_id, "analyze")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        coordinated = _enqueue_legacy_job(db, project_id, "analyze", "內容感知", {"source_analysis_writer"}, {"ai_provider"}, base_revision)
        JOBS[key] = {"kind": "內容感知", "status": "queued", "state": coordinated.state.value, "queue_reason": coordinated.queue_reason, "coordinator_job_id": coordinated.job_id, "base_revision": coordinated.base_revision, "message": coordinated.queue_reason or "等待開始", "done": 0, "total": 0, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_analyze_job, args=(cfg, db, project_id, force), daemon=True)
    thread.start()
    return True


def start_analyze_video_job(
    cfg: dict,
    db: Path,
    project_id: int,
    video_id: int,
    *,
    base_revision: int | None = None,
    sampling_override: dict | None = None,
) -> bool:
    key = (project_id, "analyze")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        coordinated = _enqueue_legacy_job(db, project_id, "analyze", "內容感知", {"source_analysis_writer"}, {"ai_provider"}, base_revision)
        JOBS[key] = {"kind": "內容感知", "status": "queued", "state": coordinated.state.value, "queue_reason": coordinated.queue_reason, "coordinator_job_id": coordinated.job_id, "base_revision": coordinated.base_revision, "message": coordinated.queue_reason or "等待開始", "done": 0, "total": 1, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(
        target=_analyze_video_job,
        args=(cfg, db, project_id, video_id, sampling_override),
        daemon=True,
    )
    thread.start()
    return True


def start_color_job(cfg: dict, db: Path, project_id: int, mode: str, *, base_revision: int | None = None) -> bool:
    key = (project_id, "color")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        coordinated = _enqueue_legacy_job(db, project_id, "color", "調色預覽", {"project_state_writer", "gpu_heavy"}, {"gpu_heavy", "ffmpeg_heavy"}, base_revision)
        JOBS[key] = {"kind": "調色預覽", "status": "queued", "state": coordinated.state.value, "queue_reason": coordinated.queue_reason, "coordinator_job_id": coordinated.job_id, "base_revision": coordinated.base_revision, "message": coordinated.queue_reason or "等待開始", "done": 0, "total": 0, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_color_job, args=(cfg, db, project_id, mode), daemon=True)
    thread.start()
    return True


def start_opencut_job(
    cfg: dict,
    db: Path,
    project_id: int,
    render_clips: bool,
    max_segments: int,
    *,
    base_revision: int | None = None,
    confirm_local_action: bool = False,
) -> bool:
    _require_local_action_confirmation({"confirm_local_action": confirm_local_action})
    key = (project_id, "opencut")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        caps = {"external_handoff"} | ({"ffmpeg_heavy", "gpu_heavy"} if render_clips else set())
        resources = {"ffmpeg_heavy", "gpu_heavy"} if render_clips else set()
        coordinated = _enqueue_legacy_job(db, project_id, "opencut", "OpenCut 交接", caps, resources, base_revision)
        JOBS[key] = {"kind": "OpenCut 交接", "status": "queued", "state": coordinated.state.value, "queue_reason": coordinated.queue_reason, "coordinator_job_id": coordinated.job_id, "base_revision": coordinated.base_revision, "message": coordinated.queue_reason or "等待開始", "done": 0, "total": 3, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_opencut_job, args=(cfg, db, project_id, render_clips, max_segments, coordinated.base_revision), daemon=True)
    thread.start()
    return True


def start_hyperframes_job(
    cfg: dict,
    db: Path,
    project_id: int,
    render: bool,
    max_segments: int,
    *,
    base_revision: int | None = None,
    confirm_local_action: bool = False,
) -> bool:
    _require_local_action_confirmation({"confirm_local_action": confirm_local_action})
    key = (project_id, "hyperframes")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        caps = {"external_handoff"} | ({"ffmpeg_heavy", "gpu_heavy"} if render else set())
        resources = {"ffmpeg_heavy", "gpu_heavy"} if render else set()
        coordinated = _enqueue_legacy_job(db, project_id, "hyperframes", "HyperFrames 初剪", caps, resources, base_revision)
        JOBS[key] = {"kind": "HyperFrames 初剪", "status": "queued", "state": coordinated.state.value, "queue_reason": coordinated.queue_reason, "coordinator_job_id": coordinated.job_id, "base_revision": coordinated.base_revision, "message": coordinated.queue_reason or "等待開始", "done": 0, "total": 3 if render else 2, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_hyperframes_job, args=(cfg, db, project_id, render, max_segments, coordinated.base_revision), daemon=True)
    thread.start()
    return True


def project_jobs(project_id: int, render_manager: RenderJobManager | None = None, db: Path | None = None) -> list[dict]:
    with JOBS_LOCK:
        legacy = [
            dict(job, project_id=pid, legacy_job_key=name)
            for (pid, name), job in JOBS.items()
            if project_id == 0 or pid == project_id
        ]
    persistent = []
    if db is not None and project_id:
        persistent.extend(perception_jobs(db, project_id))
    if render_manager is not None:
        persistent.extend(
            dict(job, kind="正式輸出")
            for job in render_manager.list(None if project_id == 0 else project_id)
        )
    if any(job.get("kind") == "內容感知" for job in persistent):
        legacy = [job for job in legacy if job.get("legacy_job_key") != "analyze"]
    return legacy + persistent


def stop_project_jobs(project_id: int, render_manager: RenderJobManager | None = None) -> None:
    with JOBS_LOCK:
        for (pid, name), job in JOBS.items():
            if pid == project_id and job.get("status") in {"queued", "running"}:
                job.update(status="stopped", state="cancelled", message="已由使用者停止", updated_at=time.time())
                token = JOB_TOKENS.get((pid, name))
                if token:
                    token.cancel()
                if job.get("coordinator_job_id"):
                    JOB_COORDINATOR.finish_cancel(str(job["coordinator_job_id"]))
    if render_manager is not None:
        render_manager.cancel_project(project_id)


def cancel_legacy_job(project_id: int, legacy_job_key: str) -> dict:
    """Stop one in-memory legacy job without touching persistent Render Jobs."""
    key = (int(project_id), str(legacy_job_key or "").strip())
    if not key[1]:
        return {"ok": False, "error": "缺少 legacy_job_key"}
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job is None:
            return {"ok": False, "error": "找不到指定背景工作"}
        if job.get("status") in {"queued", "running"}:
            job.update(status="stopped", state="cancelled", message="已由使用者停止", updated_at=time.time())
            token = JOB_TOKENS.get(key)
            if token:
                token.cancel()
            if job.get("coordinator_job_id"):
                JOB_COORDINATOR.finish_cancel(str(job["coordinator_job_id"]))
        return {
            "ok": True,
            "message": "已停止指定背景工作" if job.get("status") == "stopped" else "工作已結束",
            "job": dict(job, project_id=key[0], legacy_job_key=key[1]),
        }


def _kill_video_vault_processes() -> None:
    """Compatibility no-op; render cancellation is manager/PID scoped."""
    return None


def _set_job(project_id: int, name: str, **changes: object) -> None:
    terminal_status = str(changes.get("status") or "")
    terminal_message = str(changes.get("message") or "")
    with JOBS_LOCK:
        job = JOBS.setdefault((project_id, name), {})
        if job.get("status") in {"stopped", "cancelled"} and terminal_status not in {"stopped", "cancelled"}:
            return
        job.update(changes)
        if "percent" not in changes:
            done = int(job.get("done") or 0)
            total = int(job.get("total") or 0)
            if total:
                job["percent"] = min(100, int(done * 100 / total))
        if terminal_status == "done":
            job["state"] = JobState.SUCCEEDED.value
        elif terminal_status == "failed":
            job["state"] = JobState.FAILED.value
        elif terminal_status == "superseded":
            job["state"] = JobState.SUPERSEDED.value
        elif terminal_status == "stopped":
            job["state"] = JobState.CANCELLED.value
        job["updated_at"] = time.time()
    if terminal_status in {"done", "failed", "stopped"}:
        _finish_coordinated_job(project_id, name, terminal_status, terminal_message)


def _analyze_job(cfg: dict, db: Path, project_id: int, force: bool) -> None:
    try:
        if not _wait_for_coordinated_job(project_id, "analyze"):
            _set_job(project_id, "analyze", status="stopped", message="內容感知已由使用者停止")
            return
        rows = [dict(row) for row in project_videos(db, project_id)]
        with JOBS_LOCK:
            base_revision = JOBS.get((project_id, "analyze"), {}).get("base_revision")
        todo = [row for row in rows if force or row.get("status") != "perceived"]
        _set_job(project_id, "analyze", status="running", message="正在準備內容感知", done=0, total=len(todo), percent=0)
        processed = []
        for index, video in enumerate(todo, 1):
            if _job_stopped(project_id, "analyze"):
                return
            _set_job(project_id, "analyze", message=f"正在分析 {video.get('filename', '')}", done=index - 1)
            result = run_project_perception(
                cfg,
                db,
                project_id,
                video,
                _analyze_progress(project_id, video.get("filename", ""), index - 1, max(len(todo), 1)),
                should_cancel=lambda: _job_stopped(project_id, "analyze"),
                base_revision=base_revision,
            )
            base_revision = current_revision(db, project_id)
            processed.extend(result.get("processed", []))
            _set_job(project_id, "analyze", message=f"已完成 {video.get('filename', '')}", done=index)
        if todo:
            build_project_plan(cfg, db, project_id, base_revision=current_revision(db, project_id))
        _set_job(project_id, "analyze", status="done", message=f"內容感知完成：{len(processed)} 支", processed=processed, percent=100)
    except PerceptionCancelled:
        _set_job(project_id, "analyze", status="stopped", message="內容感知已由使用者停止")
    except ProjectRevisionConflict as exc:
        _set_job(project_id, "analyze", status="superseded", message=f"內容感知未發布：{exc}")
    except Exception as exc:
        _set_job(project_id, "analyze", status="failed", message=f"內容感知失敗：{exc}")


def _analyze_video_job(
    cfg: dict,
    db: Path,
    project_id: int,
    video_id: int,
    sampling_override: dict | None = None,
) -> None:
    try:
        if not _wait_for_coordinated_job(project_id, "analyze"):
            _set_job(project_id, "analyze", status="stopped", message="單支素材感知已由使用者停止")
            return
        video = next((dict(v) for v in project_videos(db, project_id) if int(v["id"]) == video_id), None)
        if not video:
            _set_job(project_id, "analyze", status="failed", message="找不到這支素材", percent=100)
            return
        _set_job(project_id, "analyze", status="running", message=f"正在分析 {video.get('filename', '')}", done=0, total=1, percent=0)
        with JOBS_LOCK:
            base_revision = JOBS.get((project_id, "analyze"), {}).get("base_revision")
        analyze_project_video(
            cfg,
            db,
            project_id,
            video_id,
            _analyze_progress(project_id, video.get("filename", ""), 0, 1),
            should_cancel=lambda: _job_stopped(project_id, "analyze"),
            base_revision=base_revision,
            sampling_override=sampling_override,
        )
        _set_job(project_id, "analyze", status="done", message=f"單支素材感知完成：{video.get('filename', '')}", done=1, total=1, percent=100)
    except PerceptionCancelled:
        _set_job(project_id, "analyze", status="stopped", message="單支素材感知已由使用者停止")
    except ProjectRevisionConflict as exc:
        _set_job(project_id, "analyze", status="superseded", message=f"單支素材感知未發布：{exc}")
    except Exception as exc:
        _set_job(project_id, "analyze", status="failed", message=f"單支素材感知失敗：{exc}")


def _analyze_progress(project_id: int, filename: str, base: int, total_videos: int):
    def update(frame_index: int, frame_total: int, frame: dict) -> None:
        if not frame_total:
            return
        percent = int(((base + frame_index / frame_total) / total_videos) * 100)
        _set_job(project_id, "analyze", message=f"正在分析 {filename}：frame {frame_index}/{frame_total}", percent=min(99, percent))

    return update


def _color_job(cfg: dict, db: Path, project_id: int, mode: str) -> None:
    try:
        if not _wait_for_coordinated_job(project_id, "color"):
            _set_job(project_id, "color", status="stopped", message="調色預覽已由使用者停止")
            return
        _set_job(project_id, "color", status="running", message="正在分析色彩基準並產生 Before/After 預覽", done=0, total=1, percent=5)
        with JOBS_LOCK:
            base_revision = JOBS.get((project_id, "color"), {}).get("base_revision")
        result = render_project_color_previews(cfg, db, project_id, base_revision=base_revision)
        if _job_stopped(project_id, "color"):
            return
        files = result.get("files", [])
        _set_job(project_id, "color", status="done", message=f"調色預覽完成：{len(files)} 支", files=files, previews=result.get("previews", []), state=color_state_for_api(cfg, project_id, result.get("state", {})), done=1, total=1, percent=100)
    except Exception as exc:
        if _job_stopped(project_id, "color"):
            return
        _set_job(project_id, "color", status="failed", message=f"調色預覽失敗：{exc}")


def _opencut_job(cfg: dict, db: Path, project_id: int, render_clips: bool, max_segments: int, base_revision: int) -> None:
    try:
        if not _wait_for_coordinated_job(project_id, "opencut"):
            _set_job(project_id, "opencut", status="stopped", message="OpenCut 工作已由使用者停止")
            return
        if render_clips:
            ok, reason = can_project_render(cfg, db, project_id)
            if not ok:
                _set_job(project_id, "opencut", status="failed", message=f"正式輸出被擋下：{reason}", percent=100)
                return
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", status="running", message="正在檢查並啟動 OpenCut", done=0, percent=5)
        _audit_local_action(cfg, project_id, "start_opencut", OPENCUT_URL)
        status = start_opencut()
        if not status.get("running"):
            _set_job(project_id, "opencut", status="failed", message=f"OpenCut 啟動失敗：{status.get('error', '')}", done=0)
            return
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", message="正在產生 OpenCut 素材包", done=1)
        out = export_opencut_handoff(cfg, db, project_id, render_clips, max_segments, base_revision=base_revision)
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", message="正在打開素材包資料夾", done=2)
        _open_folder(cfg, project_id, out, "open_opencut_folder")
        _set_job(project_id, "opencut", status="done", message=f"OpenCut 已啟動，素材包已完成：{out}", done=3, folder=str(out), percent=100)
    except Exception as exc:
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", status="failed", message=f"OpenCut 交接失敗：{exc}")


def _hyperframes_job(cfg: dict, db: Path, project_id: int, render: bool, max_segments: int, base_revision: int) -> None:
    try:
        if not _wait_for_coordinated_job(project_id, "hyperframes"):
            _set_job(project_id, "hyperframes", status="stopped", message="HyperFrames 工作已由使用者停止")
            return
        if render:
            ok, reason = can_project_render(cfg, db, project_id)
            if not ok:
                _set_job(project_id, "hyperframes", status="failed", message=f"正式輸出被擋下：{reason}", percent=100)
                return
            _set_job(project_id, "hyperframes", status="running", message="正在卸載本機模型以釋放顯存", done=0, percent=3)
        _set_job(project_id, "hyperframes", status="running", message="正在產生 HyperFrames timeline" + (" 與調色片段" if render else ""), done=0, percent=5)
        out = export_hyperframes_project(cfg, db, project_id, render, max_segments, base_revision=base_revision)
        if _job_stopped(project_id, "hyperframes"):
            return
        _set_job(project_id, "hyperframes", message="正在打開初剪資料夾", done=1, percent=60 if render else 80)
        _open_folder(cfg, project_id, out, "open_hyperframes_folder")
        if render:
            _set_job(project_id, "hyperframes", message="正在快速輸出 story_draft_fast.mp4", done=2, percent=75)
            result = render_fast_draft(out, cfg, db=db, project_id=project_id)
            if not result["ok"]:
                _set_job(project_id, "hyperframes", status="failed", message=f"快速初剪輸出失敗：{result['stderr'][-500:]}", done=2, folder=str(out))
                return
        _set_job(project_id, "hyperframes", status="done", message=f"HyperFrames 初剪已完成：{out}", done=3 if render else 2, folder=str(out), percent=100)
    except Exception as exc:
        if _job_stopped(project_id, "hyperframes"):
            return
        _set_job(project_id, "hyperframes", status="failed", message=f"HyperFrames 初剪失敗：{exc}")


def _job_stopped(project_id: int, name: str) -> bool:
    with JOBS_LOCK:
        return JOBS.get((project_id, name), {}).get("status") == "stopped"


def _jobs_panel(jobs: list[dict]) -> str:
    if not jobs:
        return ""
    lines = []
    for job in sorted(jobs, key=lambda j: j.get("updated_at", 0), reverse=True):
        status = job.get("status", "")
        done = int(job.get("done") or 0)
        total = int(job.get("total") or 0)
        percent = int(job.get("percent") or 0)
        progress = f"{done}/{total}｜{percent}%" if total else f"{percent}%"
        cls = "ok" if status == "done" else "wait"
        if status == "failed":
            cls = "danger"
        lines.append(f'<p><b>{h(job.get("kind", "工作"))}</b> <span class="pill {cls}">{h(status)}</span><br>{h(job.get("message", ""))}<br><span class="muted">{h(progress)}</span><span class="bar"><span style="width:{percent}%"></span></span></p>')
    stop = _button('/ui/stop-jobs', int(jobs[0].get("project_id") or 0), '停止目前工作', 'danger') if any(j.get("status") in {"queued", "running"} for j in jobs) else ""
    return '<div class="card"><h3>工作狀態</h3>' + "".join(lines) + stop + "</div>"


def _create_project_form() -> str:
    return """<div class="card"><h3>新增專案</h3>
<form class="stack" method="post" action="/ui/create">
<input name="name" placeholder="專案名稱">
<div class="row"><select name="category"><option value="coffee">咖啡</option><option value="matcha">抹茶</option><option value="travel">旅行</option><option value="mixed">混合</option><option value="unknown">未分類</option></select>
<select name="content_type"><option value="diary_montage">日常紀錄</option><option value="process_montage">過程剪輯</option><option value="travel_diary">旅行日記</option><option value="highlight">精華</option></select></div>
<button class="primary">建立專案</button>
</form></div>"""


def _project_list(projects: list[dict], current: int) -> str:
    items = []
    for p in projects:
        cls = "item active" if int(p["id"]) == current else "item"
        items.append(f'<a class="{cls}" href="/?project_id={p["id"]}"><b>{h(p["name"])}</b><div class="muted">#{p["id"]} | {h(p["status"])} | {p.get("video_count", 0)} clips</div></a>')
    return '<div class="card"><h3>專案</h3>' + ("".join(items) or '<p class="muted">目前沒有專案</p>') + "</div>"


def _project_panel(detail: dict) -> str:
    if not detail:
        return '<div class="card"><h2>尚未選擇專案</h2></div>'
    p = detail["project"]
    pid = int(p["id"])
    return f"""<div class="card">
<h2>{h(p['name'])}</h2>
<p class="muted">{h(detail['folder'])}</p>
<p class="muted">狀態：{h(p['status'])}</p>
<div class="row">
{_button('/ui/build-plan', pid, '更新故事整理')}
{_button('/ui/approve', pid, '核准專案', 'good')}
{_button('/ui/reject', pid, '退回修改', 'danger')}
</div>
<form method="post" action="/ui/reject"><input type="hidden" name="project_id" value="{pid}"><textarea name="notes" placeholder="審核備註或退回原因"></textarea></form>
</div>"""


def _clips_panel(detail: dict) -> str:
    if not detail:
        return '<div class="card"><h3>專案素材</h3><p class="muted">先建立或選擇專案。</p></div>'
    pid = int(detail["project"]["id"])
    clips = "".join(_clip_item(pid, c) for c in detail.get("clips", [])) or '<p class="muted">此專案尚無素材。</p>'
    return f"""<div class="card"><h3>專案素材</h3>
<form method="post" action="/ui/upload-project" enctype="multipart/form-data" class="stack"><input type="hidden" name="project_id" value="{pid}"><input name="file" type="file" multiple accept=".mp4,.mov,.m4v,video/*"><button>匯入到此專案</button></form>
<div class="row">{_button('/ui/analyze-project', pid, '跑待感知素材', 'primary')}{_button('/ui/analyze-project', pid, '全部重跑感知', '', {'force': '1'})}</div>
<form method="post" action="/ui/color-preview" class="row"><input type="hidden" name="project_id" value="{pid}"><select name="mode"><option value="dji_lut">DJI LUT</option><option value="safe_restore">保守修正</option><option value="warm_food">咖啡/食物暖色</option><option value="none">不調色</option></select><button>產生調色預覽</button></form>
{clips}</div>"""


def _opencut_panel(detail: dict) -> str:
    if not detail:
        return ""
    pid = int(detail["project"]["id"])
    status = opencut_status()
    state = "已啟動" if status["running"] else "未啟動"
    folder = Path(detail["folder"]) / "output" / "opencut_handoff"
    open_link = f'<a class="buttonlink primary" href="{OPENCUT_URL}" target="_blank">開啟 OpenCut</a>' if status["running"] else ""
    folder_button = _button('/ui/opencut-folder', pid, '確認並打開素材包資料夾', '', {'folder': str(folder), 'confirm_local_action': '1'}) if folder.exists() else ""
    return f"""<div class="card"><h3>OpenCut 剪輯</h3>
<p class="muted">狀態：{state}</p>
<div class="item"><b>建議流程</b><p class="muted">按「一鍵交給 OpenCut」後，OpenCut 會開在專案列表，Windows 也會打開素材包資料夾；把資料夾內影片拖進 OpenCut 即可。</p>
{_button('/ui/opencut-handoff', pid, '確認並一鍵交給 OpenCut', 'primary', {'render_clips': '1', 'confirm_local_action': '1'})}</div>
<div class="row">
{_button('/ui/opencut-export', pid, '只產生素材包')}
{_button('/ui/opencut-export', pid, '只產生調色片段', '', {'render_clips': '1'})}
{folder_button}
{_button('/ui/opencut-start', pid, '確認並啟動 OpenCut', '', {'confirm_local_action': '1'})}
{open_link}
</div>
<p class="muted">匯出資料夾：{h(folder)}</p>
</div>"""


def _hyperframes_panel(detail: dict) -> str:
    if not detail:
        return ""
    pid = int(detail["project"]["id"])
    folder = Path(detail["folder"]) / "output" / "hyperframes"
    folder_button = _button('/ui/hyperframes-folder', pid, '確認並打開初剪資料夾', '', {'confirm_local_action': '1'}) if folder.exists() else ""
    preview = f'<span class="pill ok">已輸出 story_draft_fast.mp4</span>' if (folder / "story_draft_fast.mp4").exists() else ""
    return f"""<div class="card"><h3>HyperFrames 初剪</h3>
<div class="item"><b>主流程</b><p class="muted">照行程腳本自動串片、加地點字卡、套 BGM。先產生可預覽的 HTML timeline，需要成片再 render MP4。</p>
<div class="row">
{_button('/ui/hyperframes-export', pid, '確認並產生初剪專案', 'primary', {'confirm_local_action': '1'})}
{_button('/ui/hyperframes-export', pid, '確認並快速輸出 MP4', 'good', {'render': '1', 'confirm_local_action': '1'})}
{folder_button}
{preview}
</div></div>
<p class="muted">輸出資料夾：{h(folder)}</p>
</div>"""


def _require_local_action_confirmation(data: dict) -> None:
    if data.get("confirm_local_action") not in {True, "1", 1}:
        raise ValueError("此操作會啟動本機應用程式或開啟資料夾，必須先明確確認")


def _audit_local_action(cfg: dict, project_id: int, action: str, target: object) -> None:
    audit_path = project_dir(cfg, project_id) / "decisions" / "local_actions.jsonl"
    record = {
        "timestamp": time.time(),
        "project_id": project_id,
        "action": action,
        "target": str(target),
        "confirmed": True,
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _open_folder(cfg: dict, project_id: int, path: Path, action: str) -> None:
    _audit_local_action(cfg, project_id, action, path)
    subprocess.Popen(["explorer", str(path)])


def _clip_item(project_id: int, clip: dict) -> str:
    done = clip.get("status") == "perceived"
    segment_count = int(clip.get("segment_count") or 0)
    label = f"已感知｜{segment_count} 段" if done and segment_count else ("已感知｜無推薦片段" if done else "待感知")
    pill = f'<span class="pill {"ok" if done else "wait"}">{label}</span>'
    return f"""<div class="item"><div class="row"><b>{h(clip['clip_id'])}</b>{pill}{_button('/ui/analyze-video', project_id, '跑感知', '', {'video_id': clip['video_id']})}</div>
<div>{h(clip['filename'])}</div><div class="muted">{round(float(clip.get('duration_seconds') or 0))}s | {h(clip.get('detected_category', 'unknown'))} | {h(clip.get('time_of_day', ''))}</div></div>"""


def _project_bgm_panel(project_id: int, detail: dict, bgm: list[dict]) -> str:
    used = detail.get("bgm", []) if detail else []
    options = "".join(f'<option value="{track["id"]}">#{track["id"]} {h(track["title"])} - {h(track.get("artist", ""))}</option>' for track in bgm)
    lines = "\n\n".join(f"#{track['id']} {track['title']} - {track.get('artist', '')}\n{track.get('attribution_text', '')}" for track in used) or "未指定 BGM"
    return f"""<div class="card"><h3>本專案 BGM</h3>
<form method="post" action="/ui/project-bgm" class="row"><input type="hidden" name="project_id" value="{project_id}"><select name="bgm_id">{options}</select><button>加入本專案</button></form>
<pre>{h(lines)}</pre></div>"""


def _script_panel(detail: dict) -> str:
    text = detail.get("script") if detail else ""
    return f'<div class="card"><h3>故事整理</h3><pre>{h(text or "尚未產生故事整理。")}</pre></div>'


def _bgm_library_panel(bgm: list[dict]) -> str:
    rows = "\n\n".join(f"#{t['id']} {t['title']} - {t.get('artist', '')}\n{t.get('license_name', '')} {t.get('source_url', '')}" for t in bgm) or "尚無 BGM"
    return f"""<div class="card"><h3>BGM 資料庫總覽</h3>
<form method="post" action="/ui/upload-bgm" enctype="multipart/form-data" class="stack"><div class="row"><input name="file" type="file" accept="audio/*"><input name="title" placeholder="曲名"><input name="artist" placeholder="作者"><input name="source_url" placeholder="來源 URL"><input name="license_url" placeholder="授權 URL"><input name="license_name" placeholder="授權名稱"><select name="attribution_status"><option value="unknown">署名狀態：未確認</option><option value="required">需要署名</option><option value="not_required">不需署名</option></select><button>登錄 BGM</button></div><textarea name="attribution_text" placeholder="署名文字"></textarea></form>
<pre>{h(rows)}</pre></div>"""


def _button(action: str, project_id: int, label: str, cls: str = "", fields: dict | None = None) -> str:
    hidden = f'<input type="hidden" name="project_id" value="{project_id}">'
    for key, value in (fields or {}).items():
        hidden += f'<input type="hidden" name="{h(str(key))}" value="{h(str(value))}">'
    return f'<form method="post" action="{action}">{hidden}<button class="{cls}">{h(label)}</button></form>'


def h(value: object) -> str:
    return escape(str(value or ""), quote=True)


def _stage_upload(item: _UploadPart, directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix
    handle = tempfile.NamedTemporaryFile(prefix=".video-vault-upload-", suffix=suffix, dir=directory, delete=False)
    staged = Path(handle.name)
    try:
        item.file.seek(0)
        with handle:
            while chunk := item.file.read(UPLOAD_READ_CHUNK):
                handle.write(chunk)
        return staged
    except BaseException:
        handle.close()
        staged.unlink(missing_ok=True)
        raise


def _parse_upload_form(handler: BaseHTTPRequestHandler) -> tuple[dict[str, list[_UploadPart]] | None, dict | None]:
    try:
        return _multipart_form(handler), None
    except MultipartFormError as exc:
        return None, {"ok": False, "error": str(exc)}


def _verify_form_csrf(form: dict[str, list[_UploadPart]]) -> dict | None:
    if FORM_CSRF_TOKEN and _form_value(form, "csrf_token") != FORM_CSRF_TOKEN:
        return {"ok": False, "code": "csrf_failed", "error": "CSRF token 無效或缺失，已拒絕變更操作", "action": "重新整理頁面後再試"}
    return None


class DuplicateUploadError(ValueError):
    """The requested project upload would clobber an existing source."""


def _publish_upload_no_clobber(staged: Path, destination: Path) -> None:
    """Publish one staged file without ever replacing an existing path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DuplicateUploadError(f"同名素材已存在：{destination.name}")
    created = False
    try:
        try:
            # Same-directory hard-link creation is atomic and O_EXCL-like.
            os.link(staged, destination)
            created = True
        except FileExistsError as exc:
            raise DuplicateUploadError(f"同名素材已存在：{destination.name}") from exc
        except OSError:
            # Filesystems without hard-link support still get an exclusive
            # create; a concurrent destination cannot be overwritten.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(destination, flags, 0o600)
            except FileExistsError as exc:
                raise DuplicateUploadError(f"同名素材已存在：{destination.name}") from exc
            created = True
            try:
                with staged.open("rb") as source, os.fdopen(fd, "wb") as output:
                    fd = -1
                    while chunk := source.read(UPLOAD_READ_CHUNK):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                raise
        staged.unlink()
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _snapshot_project_clips(folder: Path) -> dict[Path, bytes]:
    root = folder / "clips"
    if not root.exists():
        return {}
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _restore_project_clips(folder: Path, snapshot: dict[Path, bytes]) -> None:
    root = folder / "clips"
    current = {path.relative_to(root) for path in root.rglob("*") if path.is_file()} if root.exists() else set()
    for relative in current - set(snapshot):
        (root / relative).unlink(missing_ok=True)
    for relative, content in snapshot.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _restore_project_registration(
    db: Path,
    project_id: int,
    previous_video_ids: list[int],
    new_video_ids: list[int],
    previous_status: str | None,
    previous_updated_at: str | None,
) -> None:
    """Restore complete project-media rows and remove only request-created rows."""
    with connect(db) as con:
        previous_rows = {
            int(row["video_id"]): dict(row)
            for row in con.execute(
                "select * from project_videos where project_id=?",
                (project_id,),
            ).fetchall()
        }
        con.execute("delete from project_videos where project_id=?", (project_id,))
        for order, video_id in enumerate(previous_video_ids, 1):
            snapshot = previous_rows.get(int(video_id))
            if snapshot is None:
                con.execute(
                    "insert into project_videos(project_id, video_id, sort_order) values(?, ?, ?)",
                    (project_id, video_id, order),
                )
                continue
            snapshot["project_id"] = project_id
            snapshot["video_id"] = int(video_id)
            snapshot["sort_order"] = order
            columns = list(snapshot)
            con.execute(
                f"insert into project_videos({', '.join(columns)}) values({', '.join('?' for _ in columns)})",
                tuple(snapshot[column] for column in columns),
            )
        if previous_status is not None:
            if previous_updated_at is None:
                con.execute("update projects set status=? where id=?", (previous_status, project_id))
            else:
                con.execute("update projects set status=?, updated_at=? where id=?", (previous_status, previous_updated_at, project_id))
        for video_id in new_video_ids:
            con.execute("delete from project_videos where video_id=?", (video_id,))
            con.execute("delete from segments where video_id=?", (video_id,))
            con.execute("delete from frames where video_id=?", (video_id,))
            con.execute("delete from analysis_runs where video_id=?", (video_id,))
            con.execute("delete from videos where id=?", (video_id,))


def _upload_project_failure(
    *,
    error: str,
    project_id: int,
    failed_files: list[str],
    staged_paths: list[Path],
    published_paths: list[Path],
    source_dir: Path,
    source_dir_existed: bool,
    db: Path,
    previous_video_ids: list[int],
    new_video_ids: list[int],
    previous_status: str | None,
    previous_updated_at: str | None,
    clips_folder: Path,
    clips_snapshot: dict[Path, bytes],
    review_path: Path,
    review_snapshot: bytes | None,
    plan_path: Path,
    plan_snapshot: bytes | None,
) -> dict:
    cleanup_errors = []
    for path in staged_paths + published_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(str(exc))
    try:
        _restore_project_registration(db, project_id, previous_video_ids, new_video_ids, previous_status, previous_updated_at)
    except Exception as exc:  # noqa: BLE001 - preserve the original upload failure.
        cleanup_errors.append(f"DB rollback: {exc}")
    try:
        _restore_project_clips(clips_folder.parent, clips_snapshot)
    except OSError as exc:
        cleanup_errors.append(f"clips rollback: {exc}")
    for path, snapshot in ((review_path, review_snapshot), (plan_path, plan_snapshot)):
        try:
            if snapshot is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snapshot)
        except OSError as exc:
            cleanup_errors.append(f"project metadata rollback: {exc}")
    if not source_dir_existed:
        try:
            source_dir.rmdir()
        except OSError:
            pass
    result = {"ok": False, "error": error, "project_id": project_id, "files": [], "failed_files": failed_files}
    if staged_paths or published_paths:
        result["rolled_back_files"] = list(failed_files)
    if cleanup_errors:
        result["rollback_warnings"] = cleanup_errors
    return result


def video_list(cfg: dict, db: Path) -> list[dict]:
    result = []
    for video in videos(db):
        folder = video_dir(cfg, int(video["id"]))
        review = _read_json(folder / "review_status.json")
        result.append({"id": video["id"], "filename": video["filename"], "category": video["category"], "duration_seconds": video["duration_seconds"], "status": review.get("status") or video["status"], "approved": review.get("approved_by_user", False)})
    return result


def upload(handler: BaseHTTPRequestHandler, cfg: dict) -> dict:
    form, error = _parse_upload_form(handler)
    if error:
        return error | {"files": []}
    assert form is not None
    csrf_error = _verify_form_csrf(form)
    if csrf_error:
        _close_form(form)
        return csrf_error | {"files": []}
    saved = []
    try:
        items = _form_items(form, "file")
        if not items:
            return {"ok": False, "error": "缺少 file 欄位", "files": []}
        for item in items:
            name = Path(item.filename).name
            if Path(name).suffix.lower() not in {".mp4", ".mov", ".m4v"}:
                continue
            out = Path(cfg["library_root"]) / cfg["inbox_dir"] / name
            staged = _stage_upload(item, out.parent, name)
            try:
                os.replace(staged, out)
            finally:
                staged.unlink(missing_ok=True)
            saved.append(str(out))
        if not saved:
            return {"ok": False, "error": "沒有可匯入的影片檔案", "files": []}
        return {"ok": True, "files": saved}
    except Exception as exc:
        return {"ok": False, "error": f"匯入素材失敗：{exc}", "files": saved}
    finally:
        _close_form(form)


def upload_project(handler: BaseHTTPRequestHandler, cfg: dict, db: Path) -> dict:
    form, error = _parse_upload_form(handler)
    if error:
        return error | {"project_id": 0, "files": []}
    assert form is not None
    csrf_error = _verify_form_csrf(form)
    if csrf_error:
        _close_form(form)
        return csrf_error | {"project_id": 0, "files": []}
    saved = []
    try:
        try:
            project_id = int(_form_value(form, "project_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "project_id 無效", "project_id": 0, "files": []}
        if not project_id:
            return {"ok": False, "error": "project_id required", "project_id": 0, "files": []}
        raw_base_revision = _form_value(form, "base_revision")
        try:
            base_revision = None if raw_base_revision in (None, "") else int(raw_base_revision)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": "base_revision 必須是整數", "project_id": project_id, "files": []}
        project_row = db_project(db, project_id)
        if not project_row:
            return {"ok": False, "error": "找不到指定專案", "project_id": project_id, "files": []}
        if base_revision is not None:
            try:
                check_base_revision(db, project_id, base_revision)
            except ProjectRevisionConflict as exc:
                return {"ok": False, "code": exc.code, "error": str(exc), "project_revision": exc.current, "project_id": project_id, "files": []}
        project_folder_path = Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}"
        source_dir = project_folder_path / "source"
        source_dir_existed = source_dir.exists()
        items = _form_items(form, "file")
        if not items:
            return {"ok": False, "error": "缺少 file 欄位", "project_id": project_id, "files": []}
        project_folder = project_dir(cfg, project_id)
        clips_folder = project_folder / "clips"
        clips_snapshot = _snapshot_project_clips(project_folder)
        review_path = project_folder / "review_status.json"
        plan_path = project_folder / "project_plan.json"
        review_snapshot = review_path.read_bytes() if review_path.exists() else None
        plan_snapshot = plan_path.read_bytes() if plan_path.exists() else None
        previous_project = dict(project_row)
        previous_video_ids = [int(v["id"]) for v in project_videos(db, project_id)]
        before_video_ids = {int(v["id"]) for v in videos(db)}
        existing_paths = {
            str(Path(value).expanduser().resolve(strict=False)).casefold()
            for row in videos(db)
            for value in (row["original_path"], row["current_path"])
            if value
        }
        records = []
        seen_names: set[str] = set()
        staged_paths: list[Path] = []
        published_paths: list[Path] = []

        # Validate the whole request before creating any staged file.
        for item in items:
            name = Path(item.filename).name
            if not name:
                return {"ok": False, "error": "缺少檔名", "project_id": project_id, "files": []}
            if Path(name).suffix.lower() not in {".mp4", ".mov", ".m4v"}:
                return {"ok": False, "error": f"不支援的影片副檔名：{name}", "project_id": project_id, "files": [], "failed_files": [name]}
            if name.casefold() in seen_names:
                return {"ok": False, "error": f"上傳內容包含重複檔名：{name}", "project_id": project_id, "files": [], "failed_files": [name]}
            seen_names.add(name.casefold())
            out = source_dir / name
            if out.exists() or str(out.resolve(strict=False)).casefold() in existing_paths:
                return {"ok": False, "error": f"同名素材已存在：{name}", "code": "duplicate_filename", "project_id": project_id, "files": [], "failed_files": [name]}
            records.append({"item": item, "name": name, "out": out})

        # Stage and probe every file before any formal destination is created.
        try:
            for record in records:
                staged = _stage_upload(record["item"], source_dir, record["name"])
                staged_paths.append(staged)
                record["staged"] = staged
                try:
                    record["info"] = {"original_path": str(record["out"]), "current_path": str(record["out"]), "filename": record["out"].name, "category": "unknown", **metadata(staged, cfg), "status": "uploaded"}
                except Exception as exc:
                    return _upload_project_failure(
                        error=f"素材 metadata 失敗：{exc}", project_id=project_id, failed_files=[item["name"] for item in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=[], previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
                    )
        except Exception as exc:
            return _upload_project_failure(
                error=f"素材暫存失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=[], previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )

        # Publish with no-clobber semantics. A race with another upload still
        # rolls back the files already published by this request.
        try:
            for record in records:
                _publish_upload_no_clobber(record["staged"], record["out"])
                staged_paths.remove(record["staged"])
                published_paths.append(record["out"])
        except DuplicateUploadError as exc:
            failure = _upload_project_failure(
                error=str(exc), project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=[], previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )
            failure["code"] = "duplicate_filename"
            return failure
        except OSError as exc:
            return _upload_project_failure(
                error=f"素材發布失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=[], previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )

        registered_ids: list[int] = []
        try:
            for record in records:
                registered_ids.append(upsert_video(db, record["info"]))
        except Exception as exc:
            new_ids = [video_id for video_id in registered_ids if video_id not in before_video_ids]
            return _upload_project_failure(
                error=f"素材資料庫登記失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=new_ids, previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )

        new_ids = [video_id for video_id in registered_ids if video_id not in before_video_ids]
        try:
            set_project_videos(db, project_id, previous_video_ids + registered_ids)
        except Exception as exc:
            return _upload_project_failure(
                error=f"專案素材關聯失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=new_ids, previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )
        try:
            sync_project_files(cfg, db, project_id)
        except Exception as exc:
            return _upload_project_failure(
                error=f"專案檔案同步失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=new_ids, previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )
        try:
            mark_project_needs_review(cfg, db, project_id, base_revision=base_revision)
        except Exception as exc:
            return _upload_project_failure(
                error=f"專案狀態更新失敗：{exc}", project_id=project_id, failed_files=[record["name"] for record in records], staged_paths=staged_paths, published_paths=published_paths, source_dir=source_dir, source_dir_existed=source_dir_existed, db=db, previous_video_ids=previous_video_ids, new_video_ids=new_ids, previous_status=previous_project.get("status"), previous_updated_at=previous_project.get("updated_at"), clips_folder=clips_folder, clips_snapshot=clips_snapshot, review_path=review_path, review_snapshot=review_snapshot, plan_path=plan_path, plan_snapshot=plan_snapshot,
            )
        saved = [str(record["out"]) for record in records]
        return {"ok": True, "files": saved, "project_id": project_id}
    finally:
        _close_form(form)


def upload_bgm(handler: BaseHTTPRequestHandler, cfg: dict, db: Path) -> dict:
    form, error = _parse_upload_form(handler)
    if error:
        return error
    assert form is not None
    csrf_error = _verify_form_csrf(form)
    if csrf_error:
        _close_form(form)
        return csrf_error
    staged: Path | None = None
    try:
        items = _form_items(form, "file")
        if not items or not items[0].filename:
            return {"ok": False, "error": "缺少 file 欄位"}
        item = items[0]
        name = Path(item.filename).name
        staged = _stage_upload(item, Path(cfg["library_root"]) / "04_audio" / "_incoming_bgm", name)
        info = {k: _form_value(form, k) for k in ("title", "artist", "source_url", "license_name", "license_url", "license_source_url", "attribution_text", "attribution_status", "license_status", "verification_source", "verification_provenance", "mood")}
        info["attribution_required"] = _form_value(form, "attribution_required") == "on"
        return {"ok": True, "id": import_bgm(cfg, db, staged, info)}
    except Exception as exc:
        return {"ok": False, "error": f"BGM 匯入失敗：{exc}"}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        _close_form(form)


def process_inbox(cfg: dict, db: Path) -> dict:
    processed = []
    for path in scan_inbox(cfg):
        video_id = upsert_video(db, {"original_path": str(path), "current_path": str(path), "filename": path.name, "category": "unknown", **metadata(path, cfg), "status": "ingested"})
        video = dict(next(v for v in videos(db) if int(v["id"]) == video_id))
        analyze_ui_video(cfg, db, video)
        video = rename_after_perception(cfg, db, video)
        perceive_output(cfg, db, video)
        write_plan_files(cfg, draft_plan(cfg, db, video))
        set_video_status(db, video_id, "perceived")
        processed.append({"id": video_id, "filename": video["filename"]})
    return {"ok": True, "processed": processed}


def analyze_project(cfg: dict, db: Path, project_id: int, force: bool = False, *, base_revision: int | None = None) -> dict:
    processed = []
    for row in project_videos(db, project_id):
        video = dict(row)
        if not force and video.get("status") == "perceived":
            continue
        result = run_project_perception(cfg, db, project_id, video, base_revision=base_revision)
        processed.extend(result.get("processed", []))
        base_revision = current_revision(db, project_id)
    if processed:
        build_project_plan(cfg, db, project_id, base_revision=current_revision(db, project_id))
    return {"ok": True, "processed": processed}


def analyze_project_video(
    cfg: dict,
    db: Path,
    project_id: int,
    video_id: int,
    progress=None,
    should_cancel=None,
    *,
    base_revision: int | None = None,
    sampling_override: dict | None = None,
) -> dict:
    video = next((dict(v) for v in project_videos(db, project_id) if int(v["id"]) == video_id), None)
    if not video:
        return {"ok": False, "error": "video not found in project"}
    result = run_project_perception(
        cfg,
        db,
        project_id,
        video,
        progress,
        should_cancel,
        base_revision=base_revision,
        sampling_override=sampling_override,
    )
    return {"ok": True, **result}


def update_clip_summary(cfg: dict, db: Path, project_id: int, video_id: int, summary: str, *, base_revision: int | None = None) -> bool:
    if not any(int(v["id"]) == video_id for v in project_videos(db, project_id)):
        return False
    with project_commit(db, project_id, base_revision) as commit:
        ok = update_video_summary(db, video_id, summary.strip(), project_id=project_id)
        if ok:
            build_project_plan(cfg, db, project_id)
        commit.record_changed(bool(ok))
    return ok


def color_preview_project(cfg: dict, db: Path, project_id: int, mode: str, *, force: bool = False, base_revision: int | None = None) -> dict:
    result = render_project_color_previews(cfg, db, project_id, force=force, base_revision=base_revision)
    return {**result, "state": color_state_for_api(cfg, project_id, result.get("state", {})), "mode": mode}


def analyze_ui_video(cfg: dict, db: Path, video: dict, progress=None) -> None:
    if not db_frames(db, int(video["id"])):
        out_dir = Path(cfg["library_root"]) / "03_frames" / Path(video["filename"]).stem
        for frame in extract_frames(Path(video["current_path"]), out_dir, cfg):
            add_frame(db, int(video["id"]), frame, frame_timestamp(frame, cfg))
    analyze_video_frames(db, video, cfg, progress)


def video_detail(cfg: dict, video_id: int) -> dict:
    folder = video_dir(cfg, video_id)
    return {"plan": _read_json(folder / "edit_plan.json"), "review": _read_json(folder / "review_status.json"), "script": review_text(cfg, video_id) if (folder / "edit_script.md").exists() else "", "perception": _read_json(folder / "perception.json")}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
