"""Versioned, evidence-backed Delivery QA for formal project renders.

The automated checks are deliberately a gate before human delivery approval,
not a replacement for watching the final output.  Reports contain stable IDs
and fingerprints only; local paths and source media names never cross the
report/API boundary.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any, Mapping
import uuid

from .artifact_retention import register_artifact
from .bgm_pipeline import bgm_fingerprint
from .final_qc import sha256_file
from .media_probe import MediaProbe, probe_media
from .project import project_dir
from .segment_cache import build_segment_cache_key


QA_RUN_SCHEMA_VERSION = 1
QA_CHECK_SCHEMA_VERSION = 1
QA_CONTRACT_NAME = "formal-delivery-qa"
QA_CONTRACT_VERSION = "delivery-qa-v1"
QA_STATUSES = frozenset({"pass", "warning", "blocked", "skipped"})
QA_LIFECYCLE_STATES = frozenset({"needs_qa", "qa_blocked", "qa_needs_review", "deliverable_ready"})

_QA_LOCK = threading.RLock()
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")

_GENERAL_THRESHOLDS: dict[str, float | int] = {
    "duration_tolerance_seconds": 0.15,
    "black_block_seconds": 1.0,
    "black_cluster_window_seconds": 4.0,
    "black_cluster_count_warning": 3,
    "edge_fade_tolerance_seconds": 1.5,
    "flash_cluster_window_seconds": 1.0,
    "flash_cluster_count_warning": 2,
    "flash_brightness_delta": 45.0,
    "flash_reversal_window_seconds": 0.75,
    "scene_change_threshold": 0.70,
    "freeze_warning_seconds": 2.5,
    "silence_warning_seconds": 4.0,
    "freeze_silence_block_seconds": 4.0,
    "border_tolerance_pixels": 8,
    "loudness_min_lufs": -24.0,
    "loudness_max_lufs": -10.0,
    "true_peak_limit_db": -1.0,
    "clipping_peak_warning_db": -0.05,
    "av_tail_tolerance_seconds": 0.20,
    "bgm_tail_tolerance_seconds": 0.25,
    "repeat_source_warning_count": 2,
}

_PROFILE_THRESHOLDS: dict[str, dict[str, float | int]] = {
    "general_diary": dict(_GENERAL_THRESHOLDS),
    "travel_diary": {
        **_GENERAL_THRESHOLDS,
        "freeze_warning_seconds": 4.0,
        "silence_warning_seconds": 8.0,
        "freeze_silence_block_seconds": 6.0,
        "repeat_source_warning_count": 3,
    },
    "coffee_matcha_diary": {
        **_GENERAL_THRESHOLDS,
        "freeze_warning_seconds": 5.0,
        "silence_warning_seconds": 6.0,
        "freeze_silence_block_seconds": 8.0,
        "repeat_source_warning_count": 3,
    },
}


class DeliveryQAError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message, "details": _safe_value(self.details)}


class QAReviewVersionConflict(DeliveryQAError):
    def __init__(self, expected: int, current: int):
        super().__init__(
            "stale_delivery_qa_review",
            f"Delivery QA review version 已更新（expected {expected}, current {current}）",
            status=409,
            details={"expected_version": int(expected), "current_version": int(current)},
        )
        self.expected = int(expected)
        self.current = int(current)


def delivery_qa_root(cfg: Mapping[str, Any], project_id: int) -> Path:
    return project_dir(dict(cfg), int(project_id)) / "qa"


def run_delivery_qa(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    *,
    render_job_uuid: str,
    output_path: str | Path,
    approval_snapshot: Mapping[str, Any] | None = None,
    render_manifest_hash: str = "",
    runner: Any | None = None,
) -> dict[str, Any]:
    """Run all checks and atomically publish a new current QA report.

    A check failure is represented inside the report.  Only an inability to
    persist the QA contract raises, so callers can keep render success
    authoritative even when QA itself is blocked.
    """

    project_id = int(project_id)
    if project_id <= 0:
        raise DeliveryQAError("invalid_project", "project_id 必須是正整數")
    job_id = str(render_job_uuid or "").strip()
    if not job_id or not _TOKEN.fullmatch(job_id):
        raise DeliveryQAError("invalid_render_job", "render_job_uuid 格式無效")

    output = Path(output_path).expanduser().resolve()
    run_uuid = uuid.uuid4().hex
    run_root = delivery_qa_root(cfg, project_id) / run_uuid
    for child in ("dense-contact-sheets", "event-strips", "waveform-or-audio-summary", "evidence"):
        (run_root / child).mkdir(parents=True, exist_ok=False if child == "dense-contact-sheets" else True)

    started_at = _now()
    profile = resolve_qa_profile(cfg, db, project_id)
    snapshot = dict(approval_snapshot or {})
    manifest = _mapping(snapshot.get("manifest"))
    if not manifest:
        manifest = _read_json(project_dir(dict(cfg), project_id) / "render_manifest.json")
    expected_manifest_hash = str(render_manifest_hash or snapshot.get("manifest_hash") or manifest.get("manifest_hash") or "")

    probe: MediaProbe | None = None
    probe_error = ""
    if not output.is_file() or output.is_symlink() or output.stat().st_size <= 0:
        probe_error = "output_missing"
    else:
        try:
            probe = probe_media(str(cfg.get("ffprobe_path") or "ffprobe"), output)
        except Exception as exc:  # fail closed into a structured check
            probe_error = _safe_error(exc)

    fingerprint = {
        "sha256": sha256_file(output) if output.is_file() and not output.is_symlink() else "",
        "size_bytes": int(output.stat().st_size) if output.is_file() and not output.is_symlink() else 0,
        "mtime_ns": int(output.stat().st_mtime_ns) if output.is_file() and not output.is_symlink() else 0,
        "duration_seconds": round(float(probe.duration_seconds), 6) if probe else 0.0,
    }
    render_report, render_report_status = _load_render_report(output.with_name(output.name + ".render.json")) if output.is_file() else ({}, "missing")
    stream_details = _probe_stream_details(str(cfg.get("ffprobe_path") or "ffprobe"), output) if probe else {"ok": False, "error_code": "probe_unavailable"}
    analysis = _analyze_output(cfg, output, profile["resolved_thresholds"], runner=runner) if probe else {"ok": False, "error_code": "probe_unavailable", "events": {}}

    checks = [
        _container_check(probe, probe_error, stream_details, manifest, render_report, expected_manifest_hash, fingerprint, profile["resolved_thresholds"], render_report_status=render_report_status),
        _threshold_config_check(profile),
        _black_flash_check(analysis, profile["resolved_thresholds"], float(probe.duration_seconds) if probe else 0.0),
        _freeze_silence_check(analysis, profile["resolved_thresholds"], profile["profile_id"]),
        _border_crop_check(analysis, probe, stream_details, manifest, profile["resolved_thresholds"]),
        _audio_check(analysis, probe, render_report, manifest, profile["resolved_thresholds"]),
        _continuity_check(manifest, snapshot, profile["resolved_thresholds"]),
    ]
    _attach_threshold_audit(checks, profile)
    _annotate_check_events(checks, manifest)

    artifacts: list[dict[str, Any]] = []
    overview = run_root / "overview-contact-sheet.jpg"
    overview_record: dict[str, Any] | None = None
    if probe and _generate_contact_sheet(str(cfg.get("ffmpeg_path") or "ffmpeg"), output, overview, probe.duration_seconds, runner=runner):
        overview_record = _artifact_record(run_uuid, run_root, overview, "overview_contact_sheet", "")
        artifacts.append(overview_record)

    finding_evidence_expected = 0
    finding_evidence_created = 0
    for check in checks:
        evidence_path = run_root / "evidence" / f"{check['check_id']}.json"
        _atomic_json(evidence_path, {
            "schema_version": QA_CHECK_SCHEMA_VERSION,
            "qa_run_uuid": run_uuid,
            "check_id": check["check_id"],
            "status": check["status"],
            "metrics": check["metrics"],
            "threshold_source": check["threshold_source"],
        })
        evidence_record = _artifact_record(run_uuid, run_root, evidence_path, "metrics", check["check_id"])
        artifacts.append(evidence_record)
        check["evidence_artifact_ids"] = [evidence_record["artifact_id"]]
        if check["status"] not in {"pass"}:
            if overview_record is not None:
                check["evidence_artifact_ids"].append(overview_record["artifact_id"])
            finding_events = _event_rows(check.get("metrics"))
            for index, event in enumerate(finding_events[:4], start=1):
                finding_evidence_expected += 1
                timestamp = float(event.get("start_seconds", event.get("timestamp_seconds", 0.0)) or 0.0)
                strip = run_root / "event-strips" / f"{check['check_id']}-{index:03d}.jpg"
                if probe and _generate_event_strip(str(cfg.get("ffmpeg_path") or "ffmpeg"), output, strip, timestamp, runner=runner):
                    record = _artifact_record(run_uuid, run_root, strip, "event_strip", check["check_id"], timestamp=timestamp)
                    artifacts.append(record)
                    check["evidence_artifact_ids"].append(record["artifact_id"])
                    finding_evidence_created += 1
            if finding_events:
                timestamp = float(finding_events[0].get("start_seconds", finding_events[0].get("timestamp_seconds", 0.0)) or 0.0)
                dense = run_root / "dense-contact-sheets" / f"{check['check_id']}.jpg"
                if probe and _generate_dense_contact_sheet(str(cfg.get("ffmpeg_path") or "ffmpeg"), output, dense, timestamp, runner=runner):
                    record = _artifact_record(run_uuid, run_root, dense, "dense_contact_sheet", check["check_id"], timestamp=timestamp)
                    artifacts.append(record)
                    check["evidence_artifact_ids"].append(record["artifact_id"])

    chapter_starts = _chapter_starts(manifest)
    chapter_artifact_ids: list[str] = []
    chapter_evidence_created = 0
    for index, chapter in enumerate(chapter_starts, start=1):
        dense = run_root / "dense-contact-sheets" / f"chapter-{index:03d}.jpg"
        timestamp = float(chapter["start_seconds"])
        if probe and _generate_dense_contact_sheet(str(cfg.get("ffmpeg_path") or "ffmpeg"), output, dense, timestamp, runner=runner):
            record = _artifact_record(run_uuid, run_root, dense, "chapter_contact_sheet", "continuity_repeat", timestamp=timestamp)
            artifacts.append(record)
            chapter_artifact_ids.append(record["artifact_id"])
            chapter_evidence_created += 1
    continuity = next(item for item in checks if item["check_id"] == "continuity_repeat")
    continuity["evidence_artifact_ids"].extend(chapter_artifact_ids)

    audio_summary = run_root / "waveform-or-audio-summary" / "audio-summary.json"
    audio_check = next(item for item in checks if item["check_id"] == "audio")
    _atomic_json(audio_summary, {
        "schema_version": QA_CHECK_SCHEMA_VERSION,
        "qa_run_uuid": run_uuid,
        "metrics": audio_check["metrics"],
    })
    audio_record = _artifact_record(run_uuid, run_root, audio_summary, "audio_summary", "audio")
    artifacts.append(audio_record)
    audio_check["evidence_artifact_ids"].append(audio_record["artifact_id"])

    evidence_failures: list[str] = []
    if overview_record is None:
        evidence_failures.append("overview contact sheet 未產生")
    if chapter_starts and chapter_evidence_created < len(chapter_starts):
        evidence_failures.append("部分章節 dense contact sheet 未產生")
    if finding_evidence_created < finding_evidence_expected:
        evidence_failures.append("部分 finding event strip 未產生")
    evidence_check = _check(
        "evidence_bundle",
        "blocked" if evidence_failures else "pass",
        "；".join(evidence_failures) if evidence_failures else "Overview、章節與事件 evidence bundle 已產生",
        {
            "overview_created": overview_record is not None,
            "chapter_evidence_expected": len(chapter_starts),
            "chapter_evidence_created": chapter_evidence_created,
            "finding_evidence_expected": finding_evidence_expected,
            "finding_evidence_created": finding_evidence_created,
        },
        severity="high" if evidence_failures else "info",
        remediation="修正 FFmpeg evidence extraction 後重新執行 Delivery QA" if evidence_failures else "",
    )
    _attach_threshold_audit([evidence_check], profile)
    evidence_path = run_root / "evidence" / "evidence_bundle.json"
    _atomic_json(evidence_path, {
        "schema_version": QA_CHECK_SCHEMA_VERSION,
        "qa_run_uuid": run_uuid,
        "check_id": evidence_check["check_id"],
        "status": evidence_check["status"],
        "metrics": evidence_check["metrics"],
        "threshold_source": evidence_check["threshold_source"],
    })
    evidence_record = _artifact_record(run_uuid, run_root, evidence_path, "metrics", "evidence_bundle")
    artifacts.append(evidence_record)
    evidence_check["evidence_artifact_ids"] = [evidence_record["artifact_id"]]
    if overview_record is not None:
        evidence_check["evidence_artifact_ids"].append(overview_record["artifact_id"])
    evidence_check["evidence_artifact_ids"].extend(chapter_artifact_ids)
    checks.append(evidence_check)

    summary = _check_summary(checks)
    automation_status = "blocked" if summary["blocked"] else "warning" if summary["warning"] or summary["skipped"] else "pass"
    lifecycle = "qa_blocked" if summary["blocked"] else "qa_needs_review"
    report = {
        "schema_version": QA_RUN_SCHEMA_VERSION,
        "check_schema_version": QA_CHECK_SCHEMA_VERSION,
        "contract": {"name": QA_CONTRACT_NAME, "version": QA_CONTRACT_VERSION},
        "qa_run_uuid": run_uuid,
        "project_id": project_id,
        "render_job_uuid": job_id,
        "render_manifest_hash": expected_manifest_hash,
        "approval_snapshot": {
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "snapshot_hash": str(snapshot.get("snapshot_hash") or ""),
            "approved_project_revision": snapshot.get("approved_project_revision"),
        },
        "output_fingerprint": fingerprint,
        "profile": profile,
        "started_at": started_at,
        "completed_at": _now(),
        "automation_status": automation_status,
        "lifecycle_status": lifecycle,
        "deliverable_ready": False,
        "summary": summary,
        "checks": checks,
        "evidence_index": [_public_artifact(item) for item in artifacts],
        "human_review": {
            "status": "pending",
            "review_version": 1,
            "operator": "",
            "reviewed_at": None,
            "reason": "",
            "warning_acceptances": [],
        },
        "sensitive_data_redacted": True,
    }
    _assert_safe_report(report)
    _atomic_json(run_root / "artifact-index.json", {
        "schema_version": 1,
        "qa_run_uuid": run_uuid,
        "artifacts": artifacts,
        "sensitive_data_redacted": True,
    })
    _atomic_json(run_root / "report.json", report)
    _atomic_text(run_root / "REPORT.md", _report_markdown(report))

    with _QA_LOCK:
        _atomic_json(delivery_qa_root(cfg, project_id) / "current.json", {
            "schema_version": QA_RUN_SCHEMA_VERSION,
            "qa_run_uuid": run_uuid,
            "render_job_uuid": job_id,
            "updated_at": _now(),
        })
    _register_bundle(cfg, project_id, run_root, run_uuid, job_id)
    return deepcopy(report)


def resolve_qa_profile(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    profile_id = "general_diary"
    source = "default:general_diary"
    try:
        from .story_profiles import load_project_story_settings

        story_settings = load_project_story_settings(cfg, Path(db), int(project_id))
        candidate = str(story_settings.get("profile_id") or "general_diary")
        if candidate == "roasting_diary":
            candidate = "coffee_matcha_diary"
        if candidate in _PROFILE_THRESHOLDS:
            profile_id = candidate
            source = "project_story_profile"
    except Exception:
        pass

    thresholds = dict(_PROFILE_THRESHOLDS[profile_id])
    threshold_sources = {key: f"{QA_CONTRACT_VERSION}:{profile_id}" for key in thresholds}
    threshold_validation: dict[str, Any] = {
        "status": "pass",
        "valid_overrides": [],
        "invalid_overrides": [],
        "unknown_thresholds": [],
    }
    configured = cfg.get("delivery_qa") if isinstance(cfg.get("delivery_qa"), Mapping) else {}
    profile_overrides = configured.get("profiles") if isinstance(configured.get("profiles"), Mapping) else {}
    configured_profiles = configured.get("profiles")
    if configured_profiles not in (None, {}) and not isinstance(configured_profiles, Mapping):
        _record_invalid_threshold_override(threshold_validation, "config.profiles", "profiles must be an object")
    _apply_threshold_override(
        thresholds,
        threshold_sources,
        profile_overrides.get(profile_id) if isinstance(profile_overrides, Mapping) else None,
        "config.profile",
        threshold_validation,
    )
    _apply_threshold_override(
        thresholds,
        threshold_sources,
        configured.get("threshold_overrides"),
        "config.global",
        threshold_validation,
    )
    project_override = _read_json(project_dir(dict(cfg), int(project_id)) / "qa_settings.json")
    _apply_threshold_override(
        thresholds,
        threshold_sources,
        project_override.get("threshold_overrides"),
        "project.override",
        threshold_validation,
    )
    if threshold_validation["invalid_overrides"] or threshold_validation["unknown_thresholds"]:
        threshold_validation["status"] = "blocked"
    return {
        "profile_id": profile_id,
        "source": source,
        "resolved_thresholds": thresholds,
        "threshold_sources": threshold_sources,
        "threshold_validation": threshold_validation,
    }


def _threshold_config_check(profile: Mapping[str, Any]) -> dict[str, Any]:
    validation = _mapping(profile.get("threshold_validation"))
    status = "blocked" if str(validation.get("status") or "pass") != "pass" else "pass"
    invalid = list(validation.get("invalid_overrides") or [])
    unknown = list(validation.get("unknown_thresholds") or [])
    metrics = {
        "validation": _safe_value(validation),
        "resolved_thresholds": _safe_value(profile.get("resolved_thresholds") or {}),
        "threshold_sources": _safe_value(profile.get("threshold_sources") or {}),
    }
    if status == "blocked":
        reasons = []
        if invalid:
            reasons.append(f"{len(invalid)} 個 threshold override 無效")
        if unknown:
            reasons.append(f"{len(unknown)} 個 threshold key 未知")
        return _check(
            "threshold_config",
            "blocked",
            "；".join(reasons) or "threshold override validation failed",
            metrics,
            severity="high",
            remediation="修正已記錄的 threshold override；不要依賴 silent fallback default",
        )
    return _check("threshold_config", "pass", "threshold override 已驗證並記錄 resolved value/source", metrics)


def load_current_delivery_qa(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    pointer = _read_json(delivery_qa_root(cfg, int(project_id)) / "current.json")
    run_uuid = str(pointer.get("qa_run_uuid") or "")
    if not run_uuid or not _TOKEN.fullmatch(run_uuid):
        return _empty_qa(int(project_id))
    report = _read_json(delivery_qa_root(cfg, int(project_id)) / run_uuid / "report.json")
    if not report or int(report.get("schema_version") or 0) != QA_RUN_SCHEMA_VERSION:
        return _empty_qa(int(project_id))
    return report


def delivery_qa_for_api(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    report = deepcopy(load_current_delivery_qa(cfg, int(project_id)))
    if not report.get("exists", True):
        return report
    run_uuid = str(report.get("qa_run_uuid") or "")
    report["exists"] = True
    report["output_url"] = f"/api/project/delivery-qa/output?project_id={int(project_id)}&run_uuid={run_uuid}"
    artifact_urls = {
        str(item.get("artifact_id") or ""): f"/api/project/delivery-qa/artifact?project_id={int(project_id)}&run_uuid={run_uuid}&artifact_id={item.get('artifact_id')}"
        for item in report.get("evidence_index") or []
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    report["artifact_urls"] = artifact_urls
    report["currentity"] = "current"
    if not _approval_is_current(cfg, int(project_id), report) or not _output_metadata_is_current(cfg, int(project_id), report):
        report["currentity"] = "historical"
        report["lifecycle_status"] = "needs_qa"
        report["deliverable_ready"] = False
    _assert_safe_report(report)
    return report


def review_delivery_qa(
    cfg: Mapping[str, Any],
    project_id: int,
    run_uuid: str,
    *,
    action: str,
    expected_version: int,
    reason: str = "",
    warning_acceptances: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    project_id = int(project_id)
    run_uuid = str(run_uuid or "").strip()
    if not _TOKEN.fullmatch(run_uuid):
        raise DeliveryQAError("invalid_qa_run", "QA run identity 格式無效")
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"confirm", "reject"}:
        raise DeliveryQAError("invalid_qa_review", "QA review action 必須是 confirm 或 reject")
    clean_reason = _safe_user_text(reason)

    with _QA_LOCK:
        pointer = _read_json(delivery_qa_root(cfg, project_id) / "current.json")
        if str(pointer.get("qa_run_uuid") or "") != run_uuid:
            raise DeliveryQAError("stale_delivery_qa_run", "這不是目前的 Delivery QA run，請重新載入", status=409)
        report_path = delivery_qa_root(cfg, project_id) / run_uuid / "report.json"
        report = _read_json(report_path)
        if not report:
            raise DeliveryQAError("qa_report_missing", "Delivery QA report 不存在")
        review = _mapping(report.get("human_review"))
        current_version = int(review.get("review_version") or 1)
        if int(expected_version) != current_version:
            raise QAReviewVersionConflict(int(expected_version), current_version)
        if not _approval_is_current(cfg, project_id, report):
            raise DeliveryQAError("stale_approval_snapshot", "核准快照已變更，必須重新正式輸出並重跑 QA", status=409)
        _assert_output_unchanged(cfg, project_id, report)

        checks = [dict(item) for item in report.get("checks") or [] if isinstance(item, Mapping)]
        if normalized_action == "confirm":
            blocked = [item for item in checks if item.get("status") in {"blocked", "skipped"}]
            if blocked:
                raise DeliveryQAError(
                    "qa_not_deliverable",
                    "仍有 blocked 或 skipped 檢查；修正後必須重新執行 QA",
                    status=409,
                    details={"check_ids": [item.get("check_id") for item in blocked]},
                )
            supplied = {str(key): _safe_user_text(value) for key, value in dict(warning_acceptances or {}).items()}
            warning_ids = [str(item.get("check_id") or "") for item in checks if item.get("status") == "warning"]
            missing = [check_id for check_id in warning_ids if not supplied.get(check_id)]
            if missing:
                raise DeliveryQAError("warning_reason_required", "每個 warning 都必須填寫人工接受理由", details={"check_ids": missing})
            accepted = [
                {"check_id": check_id, "reason": supplied[check_id], "operator": "local_user", "accepted_at": _now()}
                for check_id in warning_ids
            ]
            next_review = {
                "status": "confirmed",
                "review_version": current_version + 1,
                "operator": "local_user",
                "reviewed_at": _now(),
                "reason": clean_reason,
                "warning_acceptances": accepted,
            }
            report["lifecycle_status"] = "deliverable_ready"
            report["deliverable_ready"] = True
        else:
            if not clean_reason:
                raise DeliveryQAError("review_reason_required", "退回 Delivery QA 必須填寫理由")
            next_review = {
                "status": "rejected",
                "review_version": current_version + 1,
                "operator": "local_user",
                "reviewed_at": _now(),
                "reason": clean_reason,
                "warning_acceptances": [],
            }
            report["lifecycle_status"] = "qa_needs_review"
            report["deliverable_ready"] = False
        report["human_review"] = next_review
        report["updated_at"] = _now()
        _assert_safe_report(report)
        _atomic_json(report_path, report)
        _atomic_text(report_path.with_name("REPORT.md"), _report_markdown(report))
    _register_bundle(cfg, project_id, report_path.parent, run_uuid, str(report.get("render_job_uuid") or ""))
    return delivery_qa_for_api(cfg, project_id)


def rerun_current_delivery_qa(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    from .render_job_store import RenderJobStore

    current = load_current_delivery_qa(cfg, int(project_id))
    preferred = str(current.get("render_job_uuid") or "")
    store = RenderJobStore(dict(cfg))
    jobs = store.list(int(project_id))
    job = next((item for item in jobs if str(item.get("job_id") or "") == preferred and item.get("status") == "succeeded"), None)
    job = job or next((item for item in jobs if item.get("status") == "succeeded" and item.get("output_path")), None)
    if not job:
        raise DeliveryQAError("formal_render_required", "找不到可重新檢查的正式輸出", status=409)
    return run_delivery_qa(
        cfg,
        Path(db),
        int(project_id),
        render_job_uuid=str(job.get("job_id") or ""),
        output_path=str(job.get("output_path") or ""),
        approval_snapshot=job.get("approval_snapshot") if isinstance(job.get("approval_snapshot"), Mapping) else None,
        render_manifest_hash=str(job.get("manifest_hash") or ""),
    )


def delivery_qa_artifact_path(cfg: Mapping[str, Any], project_id: int, run_uuid: str, artifact_id: str) -> Path:
    run_root = _validated_run_root(cfg, int(project_id), run_uuid)
    index = _read_json(run_root / "artifact-index.json")
    record = next((item for item in index.get("artifacts") or [] if isinstance(item, Mapping) and str(item.get("artifact_id") or "") == str(artifact_id or "")), None)
    if not record:
        raise FileNotFoundError("QA evidence artifact not found")
    relative = Path(str(record.get("relative_path") or ""))
    candidate = (run_root / relative).resolve()
    if run_root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
        raise FileNotFoundError("QA evidence artifact unavailable")
    return candidate


def delivery_qa_output_path(cfg: Mapping[str, Any], project_id: int, run_uuid: str) -> Path:
    from .render_job_store import RenderJobStore

    run_root = _validated_run_root(cfg, int(project_id), run_uuid)
    report = _read_json(run_root / "report.json")
    job = RenderJobStore(dict(cfg)).get(str(report.get("render_job_uuid") or ""))
    if not job or int(job.get("project_id") or 0) != int(project_id) or job.get("status") != "succeeded":
        raise FileNotFoundError("QA output job unavailable")
    output = Path(str(job.get("output_path") or "")).expanduser().resolve()
    expected = _mapping(report.get("output_fingerprint"))
    if not output.is_file() or output.is_symlink() or int(output.stat().st_size) != int(expected.get("size_bytes") or -1):
        raise FileNotFoundError("QA output changed or unavailable")
    if sha256_file(output) != str(expected.get("sha256") or ""):
        raise FileNotFoundError("QA output changed or unavailable")
    return output


def _container_check(
    probe: MediaProbe | None,
    probe_error: str,
    stream_details: Mapping[str, Any],
    manifest: Mapping[str, Any],
    render_report: Mapping[str, Any],
    expected_manifest_hash: str,
    fingerprint: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    *,
    render_report_status: str = "ok",
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "output_fingerprint": dict(fingerprint),
        "manifest_hash_expected": expected_manifest_hash,
        "manifest_hash_reported": str(render_report.get("manifest_hash") or ""),
        "render_report": {
            "status": str(render_report_status or "invalid"),
            "present": render_report_status != "missing",
            "parseable": render_report_status == "ok",
        },
        "stream_contract": _safe_value(stream_details),
    }
    failures: list[str] = []
    if render_report_status != "ok":
        failures.append(
            "Render Report 缺失" if render_report_status == "missing" else "Render Report 無法解析"
        )
    if probe is None:
        failures.append("正式輸出無法由 FFprobe 驗證")
        metrics["probe_error"] = probe_error or "probe_unavailable"
    else:
        metrics["video"] = {
            "codec": probe.video_codec,
            "width": probe.width,
            "height": probe.height,
            "pixel_format": probe.pixel_format,
            "fps": probe.fps,
            "frame_count": probe.frame_count,
        }
        metrics["audio"] = {"present": probe.has_audio, "codec": probe.audio_codec, "sample_rate": probe.sample_rate, "channels": probe.channels}
        metrics["duration_seconds"] = probe.duration_seconds
        expected_profile = _mapping(manifest.get("profile"))
        expected_duration = _expected_duration(manifest)
        fps = float(expected_profile.get("fps") or probe.fps or 30.0)
        duration_tolerance = max(float(thresholds["duration_tolerance_seconds"]), 3.0 / max(1.0, fps))
        duration_delta = abs(float(probe.duration_seconds) - expected_duration) if expected_duration > 0 else None
        metrics["duration_contract"] = {
            "expected_seconds": round(expected_duration, 6),
            "actual_seconds": round(float(probe.duration_seconds), 6),
            "delta_seconds": round(duration_delta, 6) if duration_delta is not None else None,
            "tolerance_seconds": round(duration_tolerance, 6),
        }
        if expected_duration <= 0:
            failures.append("核准 manifest 缺少可驗證的 timeline duration")
        elif duration_delta is not None and duration_delta > duration_tolerance:
            failures.append("duration 與核准 manifest/timeline 不一致")
        if not probe.has_audio:
            failures.append("正式輸出缺少音訊串流")
        expected = expected_profile
        comparisons = (
            (probe.width, expected.get("width"), "width"),
            (probe.height, expected.get("height"), "height"),
            (probe.pixel_format, expected.get("pixel_format"), "pixel_format"),
            (probe.video_codec, expected.get("video_codec"), "video_codec"),
            (probe.audio_codec, expected.get("audio_codec"), "audio_codec"),
            (probe.sample_rate, expected.get("audio_sample_rate"), "audio_sample_rate"),
            (probe.channels, expected.get("audio_channels"), "audio_channels"),
        )
        for actual, wanted, label in comparisons:
            if wanted not in (None, "") and actual != wanted:
                failures.append(f"{label} 與核准 manifest 不一致")
        if expected.get("fps") not in (None, "") and abs(float(probe.fps) - float(expected["fps"])) > 0.01:
            failures.append("fps 與核准 manifest 不一致")
        expected_layout = "stereo" if int(expected.get("audio_channels") or 0) == 2 else "mono" if int(expected.get("audio_channels") or 0) == 1 else ""
        actual_layout = str(stream_details.get("audio_channel_layout") or "")
        metrics["audio_channel_layout_contract"] = {"expected": expected_layout, "actual": actual_layout}
        if expected_layout and actual_layout != expected_layout:
            failures.append("audio channel layout 與核准 render contract 不一致")
    reported_manifest_hash = str(render_report.get("manifest_hash") or "")
    if not expected_manifest_hash:
        failures.append("核准 manifest_hash 缺失")
    elif reported_manifest_hash != expected_manifest_hash:
        failures.append("Render Report manifest hash 缺失或不一致")
    reported_sha = str(render_report.get("output_sha256") or "")
    actual_sha = str(fingerprint.get("sha256") or "")
    if not reported_sha or not actual_sha or reported_sha != actual_sha:
        failures.append("正式輸出 fingerprint 缺失或與 Render Report 不一致")
    if not bool(stream_details.get("ok")):
        failures.append("container/stream detail probe 未完成")
    faststart_required = bool(
        _mapping(manifest.get("profile")).get(
            "faststart_required",
            _mapping(manifest.get("settings")).get("faststart_required", True),
        )
    )
    metrics["faststart_contract"] = {"required": faststart_required, "actual": bool(stream_details.get("faststart"))}
    if faststart_required and not bool(stream_details.get("faststart")):
        failures.append("MP4 fast-start contract 未通過")
    qc = _mapping(render_report.get("qc"))
    measurements = _mapping(render_report.get("measurements"))
    decode = _mapping(measurements.get("decode"))
    metrics["render_qc"] = {
        "present": bool(qc),
        "passed": qc.get("passed"),
        "full_decode_ok": decode.get("ok"),
        "timestamp_monotonic": measurements.get("timestamp_monotonic"),
    }
    if qc.get("passed") is not True:
        failures.append("Render Report final QC 缺失或未通過")
    if decode.get("ok") is not True:
        failures.append("Render Report full decode 缺失或未通過")
    if measurements.get("timestamp_monotonic") is not True:
        failures.append("Render Report packet timestamp continuity 缺失或未通過")
    return _check(
        "container_manifest",
        "blocked" if failures else "pass",
        "；".join(failures) if failures else "Container、stream 與核准 manifest 一致",
        metrics,
        severity="high" if failures else "info",
        remediation="重新正式輸出，並確認 FFmpeg/FFprobe 與核准 manifest 未變更" if failures else "",
    )


def _black_flash_check(analysis: Mapping[str, Any], thresholds: Mapping[str, Any], duration_seconds: float = 0.0) -> dict[str, Any]:
    events = _mapping(analysis.get("events"))
    black = [dict(item) for item in events.get("black") or [] if isinstance(item, Mapping)]
    flashes = [
        dict(item) if isinstance(item, Mapping) else {"kind": "flash", "timestamp_seconds": float(item)}
        for item in events.get("flash") or []
    ]
    flash_timestamps = [
        float(item.get("timestamp_seconds") or item.get("start_seconds") or 0)
        for item in flashes
    ]
    scene_changes = [float(item) for item in events.get("scene_change") or []]
    metrics = {
        "events": black + flashes,
        "black_event_count": len(black),
        "flash_event_count": len(flashes),
        "scene_change_count": len(scene_changes),
        "max_scene_change_cluster": _max_cluster(scene_changes, float(thresholds["flash_cluster_window_seconds"])),
        "brightness_sample_count": int(analysis.get("brightness_sample_count") or 0),
        "brightness_range": _safe_value(analysis.get("brightness_range") or {}),
    }
    if not analysis.get("ok"):
        metrics["analysis_error"] = analysis.get("error_code") or "analysis_failed"
        return _check("black_flash", "blocked", "黑畫面與閃爍分析未完成", metrics, severity="high", remediation="修正 FFmpeg decode/filters 後重新執行 Delivery QA")
    long_black = [item for item in black if float(item.get("duration_seconds") or 0) >= float(thresholds["black_block_seconds"])]
    edge_tolerance = float(thresholds["edge_fade_tolerance_seconds"])
    likely_fades = [
        item for item in long_black
        if float(item.get("start_seconds") or 0) <= edge_tolerance
        or (duration_seconds > 0 and float(item.get("end_seconds") or 0) >= duration_seconds - edge_tolerance)
    ]
    interior_long_black = [item for item in long_black if item not in likely_fades]
    dense_black = _max_cluster([float(item.get("start_seconds") or 0) for item in black], float(thresholds["black_cluster_window_seconds"]))
    dense_flash = _max_cluster(flash_timestamps, float(thresholds["flash_cluster_window_seconds"]))
    metrics.update({"long_black_count": len(long_black), "interior_long_black_count": len(interior_long_black), "likely_edge_fade_count": len(likely_fades), "max_black_cluster": dense_black, "max_flash_cluster": dense_flash})
    if interior_long_black:
        return _check("black_flash", "blocked", f"偵測到 {len(interior_long_black)} 段非片頭/片尾的過長黑畫面", metrics, severity="high", remediation="檢查轉場、缺幀或空白片段，修正後重新輸出並重跑 QA")
    if likely_fades:
        return _check("black_flash", "warning", "偵測到片頭/片尾 fade-to-black，需人工確認是否為設計意圖", metrics, severity="low", remediation="從 evidence timecode 預覽片頭/片尾 fade；若為刻意設計，人工填寫接受理由")
    if dense_black >= int(thresholds["black_cluster_count_warning"]) or dense_flash >= int(thresholds["flash_cluster_count_warning"]):
        return _check("black_flash", "warning", "偵測到密集短黑畫面或高頻亮度變化", metrics, severity="medium", remediation="從 evidence timecode 預覽轉場；若為刻意設計，人工填寫接受理由")
    return _check("black_flash", "pass", "未偵測到超出 profile 門檻的黑畫面或閃爍", metrics)


def _freeze_silence_check(analysis: Mapping[str, Any], thresholds: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    events = _mapping(analysis.get("events"))
    freezes = [dict(item) for item in events.get("freeze") or [] if isinstance(item, Mapping)]
    silences = [dict(item) for item in events.get("silence") or [] if isinstance(item, Mapping)]
    overlaps = _interval_overlaps(freezes, silences)
    metrics = {"events": freezes + silences + overlaps, "freeze_count": len(freezes), "silence_count": len(silences), "profile_id": profile_id, "overlaps": overlaps}
    if not analysis.get("ok"):
        metrics["analysis_error"] = analysis.get("error_code") or "analysis_failed"
        return _check("freeze_silence", "blocked", "凍結畫面與靜音分析未完成", metrics, severity="high", remediation="修正 FFmpeg decode/filters 後重新執行 Delivery QA")
    blocked = [item for item in overlaps if float(item.get("duration_seconds") or 0) >= float(thresholds["freeze_silence_block_seconds"])]
    long_freezes = [item for item in freezes if float(item.get("duration_seconds") or 0) >= float(thresholds["freeze_warning_seconds"])]
    long_silences = [item for item in silences if float(item.get("duration_seconds") or 0) >= float(thresholds["silence_warning_seconds"])]
    if blocked:
        return _check("freeze_silence", "blocked", "長時間凍結畫面同時沒有有效音訊", metrics, severity="high", remediation="檢查來源片段、等待鏡頭與音訊配置，修正後重新輸出並重跑 QA")
    if long_freezes or long_silences:
        return _check("freeze_silence", "warning", "偵測到 profile 容許邊界附近的長凍結或安靜段落", metrics, severity="medium", remediation="人工預覽 evidence；旅行氣氛或沖煮等待若屬刻意節奏，可填寫理由接受")
    return _check("freeze_silence", "pass", "凍結與靜音長度符合內容 profile", metrics)


def _border_crop_check(analysis: Mapping[str, Any], probe: MediaProbe | None, stream_details: Mapping[str, Any], manifest: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    crops = [tuple(int(value) for value in item) for item in analysis.get("crop_observations") or [] if isinstance(item, (list, tuple)) and len(item) == 4]
    common = Counter(crops).most_common(1)[0] if crops else None
    rotation = int(stream_details.get("rotation_degrees") or 0)
    visual = _mapping(manifest.get("visual_timeline"))
    visual_items = [dict(item) for item in visual.get("resolved_items", visual.get("items", [])) if isinstance(item, Mapping)]
    from .visual_compositor import STYLE_CONTRACTS

    style_ids = [str(item.get("style_id") or "") for item in visual_items]
    unknown_styles = sorted(set(style_ids) - set(STYLE_CONTRACTS))
    safe_area_contracts = {
        style_id: {key: STYLE_CONTRACTS[style_id].get(key) for key in ("x", "y", "align")}
        for style_id in sorted(set(style_ids))
        if style_id in STYLE_CONTRACTS
    }
    metrics: dict[str, Any] = {"crop_observation_count": len(crops), "rotation_degrees": rotation, "sample_aspect_ratio": stream_details.get("sample_aspect_ratio"), "display_aspect_ratio": stream_details.get("display_aspect_ratio"), "visual_item_count": len(visual_items), "safe_area_style_contracts": safe_area_contracts, "unknown_safe_area_style_count": len(unknown_styles)}
    if not analysis.get("ok") or probe is None:
        metrics["analysis_error"] = analysis.get("error_code") or "analysis_failed"
        return _check("border_crop_safe_area", "blocked", "邊框、裁切與方向分析未完成", metrics, severity="high", remediation="修正輸出 decode 後重新執行 Delivery QA")
    if not common:
        return _check("border_crop_safe_area", "skipped", "沒有足夠 cropdetect 樣本可驗證邊框與裁切", metrics, severity="low", remediation="以可解碼的完整正式輸出重新執行 QA，並人工確認安全區")
    crop, count = common
    metrics["dominant_crop"] = {"width": crop[0], "height": crop[1], "x": crop[2], "y": crop[3], "observations": count}
    tolerance = int(thresholds["border_tolerance_pixels"])
    border = abs(crop[0] - probe.width) > tolerance or abs(crop[1] - probe.height) > tolerance or crop[2] > tolerance or crop[3] > tolerance
    expected_orientation = "portrait" if int(_mapping(manifest.get("profile")).get("height") or 0) > int(_mapping(manifest.get("profile")).get("width") or 0) else "landscape"
    actual_orientation = "portrait" if probe.height > probe.width else "landscape"
    sar = str(stream_details.get("sample_aspect_ratio") or "")
    dar = str(stream_details.get("display_aspect_ratio") or "")
    aspect_risk = sar not in {"", "1:1", "1/1"}
    expected_profile = _mapping(manifest.get("profile"))
    expected_ratio = float(expected_profile.get("width") or probe.width) / max(1.0, float(expected_profile.get("height") or probe.height))
    actual_ratio = probe.width / max(1, probe.height)
    if abs(actual_ratio - expected_ratio) > 0.02:
        aspect_risk = True
    try:
        if ":" in dar:
            left, right = dar.split(":", 1)
            aspect_risk = aspect_risk or abs(float(left) / float(right) - expected_ratio) > 0.02
    except (TypeError, ValueError, ZeroDivisionError):
        aspect_risk = True
    metrics.update({"expected_orientation": expected_orientation, "actual_orientation": actual_orientation, "expected_aspect_ratio": round(expected_ratio, 6), "actual_aspect_ratio": round(actual_ratio, 6), "aspect_ratio_risk": aspect_risk})
    declared = bool(_mapping(manifest.get("settings")).get("declared_design_frame"))
    if rotation not in {0, 360} or expected_orientation != actual_orientation or border or unknown_styles or aspect_risk:
        label = "已宣告的設計邊框需要人工確認" if declared else "偵測到邊框、裁切或方向風險"
        return _check("border_crop_safe_area", "warning", label, metrics, severity="medium", remediation="預覽 overview/contact sheet，確認 letterbox、crop 與 title/lower-third safe area 是否符合設計")
    return _check("border_crop_safe_area", "pass", "邊框、裁切、方向與安全區檢查符合門檻", metrics)


def _audio_check(analysis: Mapping[str, Any], probe: MediaProbe | None, render_report: Mapping[str, Any], manifest: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    loudness = _mapping(_mapping(render_report.get("loudness")).get("final"))
    measured_lufs = _finite(loudness.get("measured_I"))
    measured_peak = _finite(loudness.get("measured_TP"))
    max_volume = _finite(analysis.get("max_volume_db"))
    invalid_sample_count = analysis.get("invalid_sample_count")
    roles = [str(_mapping(_mapping(item).get("audio")).get("role") or _mapping(item).get("audio_role") or "") for item in manifest.get("segments") or []]
    segment_audio_fades = [
        {
            "fade_in_seconds": _mapping(_mapping(item).get("audio")).get("fade_in_seconds"),
            "fade_out_seconds": _mapping(_mapping(item).get("audio")).get("fade_out_seconds"),
        }
        for item in manifest.get("segments") or []
        if isinstance(item, Mapping) and isinstance(item.get("audio"), Mapping)
    ]
    metrics: dict[str, Any] = {
        "integrated_lufs": measured_lufs,
        "true_peak_dbtp": measured_peak,
        "max_volume_db_approx": max_volume,
        "invalid_sample_count": invalid_sample_count,
        "audio_roles": dict(Counter(roles)),
        "segment_audio_fade_contracts": segment_audio_fades,
        "events": [dict(item) for item in _mapping(analysis.get("events")).get("silence") or [] if isinstance(item, Mapping)],
    }
    failures: list[str] = []
    warnings: list[str] = []
    provenance = _audio_render_provenance(manifest, render_report)
    metrics["render_audio_provenance"] = provenance["metrics"]
    failures.extend(provenance["failures"])
    if probe is None or not probe.has_audio:
        failures.append("正式輸出缺少可驗證的音訊")
    if not analysis.get("ok"):
        failures.append("音訊 decode/分析未完成")
        metrics["analysis_error"] = analysis.get("error_code") or "analysis_failed"
    if probe is not None and probe.has_audio:
        drift = abs((probe.video_end_seconds - probe.video_start_seconds) - (probe.audio_end_seconds - probe.audio_start_seconds))
        metrics["av_tail_drift_seconds"] = round(drift, 6)
        if drift > float(thresholds["av_tail_tolerance_seconds"]):
            failures.append("音訊與影像尾端不一致")
    if measured_peak is not None and measured_peak > float(thresholds["true_peak_limit_db"]) + 0.1:
        failures.append("true peak 超過交付門檻")
    if invalid_sample_count is None:
        warnings.append("缺少 invalid audio sample 證據")
    elif int(invalid_sample_count) > 0:
        failures.append("音訊含 NaN/Inf invalid samples")
    if max_volume is None:
        warnings.append("缺少 clipping sample-peak 證據")
    elif max_volume >= 0:
        failures.append("音訊 sample peak 已 clipping")
    elif max_volume >= float(thresholds["clipping_peak_warning_db"]):
        warnings.append("音訊 sample peak 接近 clipping")
    if measured_lufs is None:
        warnings.append("缺少 integrated loudness 證據")
    elif measured_lufs < float(thresholds["loudness_min_lufs"]) or measured_lufs > float(thresholds["loudness_max_lufs"]):
        warnings.append("integrated loudness 超出內容 profile 範圍")
    invalid_roles = sorted(set(roles) - {"keep", "keep_original", "lower", "lower_original", "mute", "bgm_only"})
    if invalid_roles:
        failures.append("manifest 含無效 audio role")
        metrics["invalid_audio_role_count"] = len(invalid_roles)
    bgm = next((_mapping(item) for item in manifest.get("bgm") or [] if isinstance(item, Mapping)), {})
    if bgm:
        expected_duration = _expected_duration(manifest)
        bgm_duration = _finite(bgm.get("duration_seconds"))
        bgm_start = max(0.0, float(bgm.get("start_seconds") or 0))
        loop = bool(bgm.get("loop"))
        coverage_end = expected_duration if loop else (bgm_start + bgm_duration if bgm_duration is not None else None)
        metrics["bgm"] = {
            "used": True,
            "loop": loop,
            "start_seconds": bgm_start,
            "source_duration_seconds": bgm_duration,
            "expected_timeline_seconds": expected_duration,
            "expected_coverage_end_seconds": coverage_end,
            "fade_in_seconds": bgm.get("fade_in_seconds"),
            "fade_out_seconds": bgm.get("fade_out_seconds"),
        }
        if float(bgm.get("fade_out_seconds") or 0) <= 0:
            warnings.append("BGM 未宣告 fade-out")
        if float(bgm.get("fade_in_seconds") or 0) <= 0:
            warnings.append("BGM 未宣告 fade-in")
        if not loop:
            if bgm_duration is None or bgm_duration <= 0:
                warnings.append("非循環 BGM 缺少 duration coverage 證據")
            elif coverage_end is not None and coverage_end < expected_duration - float(thresholds["bgm_tail_tolerance_seconds"]):
                warnings.append("非循環 BGM 在正式 timeline 結束前提前終止")
    else:
        metrics["bgm"] = {"used": False}
    if failures:
        return _check("audio", "blocked", "；".join(failures), metrics, severity="high", remediation="修正音訊 stream、loudness、尾端或 audio role 後重新正式輸出")
    if warnings:
        return _check("audio", "warning", "；".join(warnings), metrics, severity="medium", remediation="人工聆聽 evidence 與成片；確認屬刻意音量設計後填寫接受理由")
    return _check("audio", "pass", "音訊 stream、loudness、peak、尾端與 role 契約通過", metrics)


def _audio_render_provenance(manifest: Mapping[str, Any], render_report: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that the formal report proves the approved audio contract was rendered."""

    expected_segments = _ordered_segments(manifest)
    expected_keys: list[str] = []
    segment_failures: list[dict[str, Any]] = []
    for index, segment in enumerate(expected_segments):
        try:
            expected_keys.append(build_segment_cache_key(manifest, segment))
        except (OSError, TypeError, ValueError, KeyError) as exc:
            expected_keys.append("")
            segment_failures.append({"index": index, "reason": "approved segment cache key unavailable", "error": _safe_error(exc)})
    reported_segments = render_report.get("segments") if isinstance(render_report.get("segments"), list) else []
    reported_keys = [
        str(item.get("cache_key") or "")
        for item in reported_segments
        if isinstance(item, Mapping)
    ]
    if len(reported_segments) != len(expected_segments):
        segment_failures.append({
            "reason": "segment count mismatch",
            "approved_count": len(expected_segments),
            "reported_count": len(reported_segments),
        })
    for index, (expected, actual) in enumerate(zip(expected_keys, reported_keys)):
        if not expected or expected != actual:
            segment_failures.append({
                "index": index,
                "reason": "segment cache key mismatch",
                "approved_cache_key": expected,
                "reported_cache_key": actual,
            })

    expected_bgm = next(
        (_mapping(item) for item in manifest.get("bgm") or [] if isinstance(item, Mapping)),
        {},
    )
    reported_bgm = _mapping(render_report.get("bgm"))
    expected_bgm_used = bool(expected_bgm)
    reported_bgm_used = reported_bgm.get("used") is True
    expected_bgm_fp: dict[str, Any] = {}
    bgm_error = ""
    if expected_bgm_used:
        try:
            expected_bgm_fp = bgm_fingerprint(expected_bgm)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            bgm_error = _safe_error(exc)
    reported_bgm_fp = _mapping(reported_bgm.get("fingerprint"))
    bgm_fingerprint_match = bool(
        expected_bgm_used
        and not bgm_error
        and reported_bgm_used
        and reported_bgm_fp
        and reported_bgm_fp == expected_bgm_fp
    ) if expected_bgm_used else bool(
        not expected_bgm_used
        and "used" in reported_bgm
        and reported_bgm.get("used") is False
        and reported_bgm_fp == {}
    )
    provenance_metrics = {
        "approved_segment_cache_keys": expected_keys,
        "reported_segment_cache_keys": reported_keys,
        "segment_mismatches": segment_failures,
        "approved_audio_contract": [
            {
                "segment_id": str(segment.get("segment_id") or segment.get("segment_uuid") or ""),
                "role": str(_mapping(segment.get("audio")).get("role") or segment.get("audio_role") or ""),
                "fade_in_seconds": _mapping(segment.get("audio")).get("fade_in_seconds"),
                "fade_out_seconds": _mapping(segment.get("audio")).get("fade_out_seconds"),
            }
            for segment in expected_segments
        ],
        "bgm": {
            "approved_used": expected_bgm_used,
            "reported_used": reported_bgm.get("used"),
            "fingerprint_match": bgm_fingerprint_match,
            "approved_fingerprint": _fingerprint_audit(expected_bgm_fp),
            "reported_fingerprint": _fingerprint_audit(reported_bgm_fp),
            "error": bgm_error,
        },
    }
    failures: list[str] = []
    if segment_failures:
        failures.append("Render Report segment cache key 與核准 audio contract 不一致")
    if not bgm_fingerprint_match:
        failures.append("Render Report BGM used/fingerprint 與核准 BGM contract 不一致")
    return {"failures": failures, "metrics": _safe_value(provenance_metrics)}


def _fingerprint_audit(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "track_id",
            "source_size",
            "source_mtime_ns",
            "source_sha256",
            "gain_db",
            "start_seconds",
            "loop",
            "fade_in_seconds",
            "fade_out_seconds",
        )
        if key in value
    }


def _ordered_segments(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    indexed = [
        (index, dict(item))
        for index, item in enumerate(manifest.get("segments") or [])
        if isinstance(item, Mapping)
    ]
    return [
        item
        for _index, item in sorted(
            indexed,
            key=lambda pair: (
                int(pair[1].get("order")) if str(pair[1].get("order") or "").lstrip("-").isdigit() else pair[0],
                pair[0],
            ),
        )
    ]


def _continuity_check(manifest: Mapping[str, Any], snapshot: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    segments = [dict(item) for item in manifest.get("segments") or [] if isinstance(item, Mapping)]
    stable_ids = [str(item.get("segment_uuid") or item.get("segment_id") or "") for item in segments]
    missing = [index for index, value in enumerate(stable_ids, start=1) if not value]
    duplicates = [key for key, count in Counter(value for value in stable_ids if value).items() if count > 1]
    assets = {
        str(item.get("canonical_path") or ""): str(item.get("sha256") or "")
        for item in snapshot.get("assets") or []
        if isinstance(item, Mapping) and item.get("canonical_path") and item.get("sha256")
    }
    source_keys: list[str] = []
    exact_keys: list[tuple[str, float, float]] = []
    for item in segments:
        source_fp = assets.get(str(item.get("source_file") or ""), "")
        if source_fp:
            source_keys.append(source_fp)
            exact_keys.append((source_fp, round(float(item.get("source_in_seconds") or 0), 3), round(float(item.get("source_out_seconds") or 0), 3)))
    exact_repeats = sum(count - 1 for count in Counter(exact_keys).values() if count > 1)
    overused = sum(1 for count in Counter(source_keys).values() if count > int(thresholds["repeat_source_warning_count"]))
    duplicate_groups = [str(item.get("duplicate_group") or "") for item in segments if str(item.get("duplicate_group") or "")]
    repeated_duplicate_groups = sum(count - 1 for count in Counter(duplicate_groups).values() if count > 1)
    visual = _mapping(manifest.get("visual_timeline"))
    visual_items = [dict(item) for item in visual.get("resolved_items", visual.get("items", [])) if isinstance(item, Mapping)]
    timeline_ranges = []
    for item in visual_items:
        start = _finite(item.get("timeline_start_seconds", item.get("resolved_start_seconds", item.get("start_seconds"))))
        end = _finite(item.get("timeline_end_seconds", item.get("end_seconds")))
        if end is None and start is not None:
            item_duration = _finite(item.get("duration_seconds"))
            end = start + item_duration if item_duration is not None else None
        if start is not None and end is not None:
            timeline_ranges.append((start, end, str(item.get("stable_id") or "")))
    timeline_ranges.sort()
    overlaps = [
        {"first_stable_id": left[2], "second_stable_id": right[2], "start_seconds": right[0], "duration_seconds": round(left[1] - right[0], 6)}
        for left, right in zip(timeline_ranges, timeline_ranges[1:])
        if right[0] < left[1] - 0.001
    ]
    groups = [str(item.get("group_id") or "") for item in segments if str(item.get("group_id") or "")]
    distinct_groups = list(dict.fromkeys(groups))
    group_transitions = sum(1 for left, right in zip(groups, groups[1:]) if left != right)
    visual_types = [str(item.get("kind") or item.get("type") or "").lower() for item in visual_items]
    boundary_items = sum(1 for item_type in visual_types if item_type in {"chapter", "chapter_title", "title_card", "chapter-card", "chapter_card"})
    required_visual_types = {
        str(value).strip().lower()
        for value in _mapping(manifest.get("visual_timeline")).get("required_types") or []
        if str(value).strip()
    }
    missing_visual_types = sorted(required_visual_types - set(visual_types))
    metrics = {
        "segment_count": len(segments),
        "stable_id_coverage": len(stable_ids) - len(missing),
        "source_fingerprint_coverage": len(source_keys),
        "duplicate_stable_id_count": len(duplicates),
        "exact_repeat_count": exact_repeats,
        "overused_source_fingerprint_count": overused,
        "duplicate_group_repeat_count": repeated_duplicate_groups,
        "distinct_chapter_count": len(distinct_groups),
        "group_transition_count": group_transitions,
        "chapter_boundary_artifact_count": boundary_items,
        "intro_visual_count": visual_types.count("intro"),
        "outro_visual_count": visual_types.count("outro"),
        "missing_required_visual_type_count": len(missing_visual_types),
        "events": overlaps,
    }
    failures: list[str] = []
    warnings: list[str] = []
    if not segments:
        failures.append("manifest 沒有可檢查片段")
    if missing or duplicates:
        failures.append("stable segment UUID 缺失或重複")
    if overlaps:
        failures.append("視覺 timeline 有重疊")
    if len(source_keys) < len(segments):
        warnings.append("部分片段缺少 approval source fingerprint")
    if exact_repeats or overused or repeated_duplicate_groups:
        warnings.append("偵測到重複片段或來源過度重用")
    if distinct_groups and boundary_items < len(distinct_groups):
        warnings.append("章節切換缺少對應 title-card/boundary 證據")
    if missing_visual_types:
        warnings.append("核准 visual contract 缺少必要 intro/chapter/outro item")
    if failures:
        return _check("continuity_repeat", "blocked", "；".join(failures), metrics, severity="high", remediation="修正 stable UUID 或 timeline overlap，重新核准並正式輸出")
    if warnings:
        return _check("continuity_repeat", "warning", "；".join(warnings), metrics, severity="medium", remediation="依 stable UUID 與 output timecode 檢查重複鏡頭及章節連續性；刻意重複需人工填寫理由")
    return _check("continuity_repeat", "pass", "Stable UUID、source fingerprint、章節與 timeline 連續性通過", metrics)


def _analyze_output(cfg: Mapping[str, Any], output: Path, thresholds: Mapping[str, Any], *, runner: Any | None = None) -> dict[str, Any]:
    ffmpeg = str(cfg.get("ffmpeg_path") or "ffmpeg")
    configured = cfg.get("delivery_qa") if isinstance(cfg.get("delivery_qa"), Mapping) else {}
    timeout = max(30.0, float(configured.get("timeout_seconds") or 600))
    video_filter = (
        "signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG,"
        "cropdetect=24:16:0,"
        f"blackdetect=d={float(thresholds['black_block_seconds']) / 2:.3f}:pix_th=0.10,"
        f"freezedetect=n=-50dB:d={float(thresholds['freeze_warning_seconds']) / 2:.3f},"
        f"select=gt(scene\\,{float(thresholds['scene_change_threshold']):.3f}),showinfo"
    )
    audio_filter = f"silencedetect=n=-50dB:d={float(thresholds['silence_warning_seconds']) / 2:.3f},volumedetect,aformat=sample_fmts=flt,astats=metadata=0:reset=0"
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", str(output), "-vf", video_filter, "-af", audio_filter, "-f", "null", "-"]
    try:
        result = runner.run(command, capture_output=True, text=True, check=False, expected_duration_seconds=None) if runner is not None else subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "error_code": "analysis_timeout", "events": {}}
    except OSError as exc:
        return {"ok": False, "returncode": None, "error_code": _safe_error(exc), "events": {}}
    parsed = _parse_analysis_log((result.stderr or "") + "\n" + (result.stdout or ""))
    brightness_samples = parsed.pop("brightness_samples", [])
    parsed["brightness_sample_count"] = len(brightness_samples)
    if brightness_samples:
        values = [float(item["yavg"]) for item in brightness_samples]
        parsed["brightness_range"] = {"min": round(min(values), 3), "max": round(max(values), 3)}
    else:
        parsed["brightness_range"] = {}
    parsed["events"]["flash"] = _detect_flash_events(brightness_samples, thresholds)
    return {"ok": result.returncode == 0, "returncode": result.returncode, "error_code": "" if result.returncode == 0 else "ffmpeg_analysis_failed", **parsed}


def _parse_analysis_log(text: str) -> dict[str, Any]:
    black: list[dict[str, Any]] = []
    freeze: list[dict[str, Any]] = []
    silence: list[dict[str, Any]] = []
    flashes: list[float] = []
    scene_changes: list[float] = []
    crops: list[list[int]] = []
    brightness_samples: list[dict[str, float]] = []
    frame_timestamp: float | None = None
    frame_yavg: float | None = None
    freeze_start: float | None = None
    silence_start: float | None = None
    max_volume: float | None = None
    invalid_sample_count: int | None = None
    for line in text.splitlines():
        frame_match = re.search(r"frame:\s*\d+\s+pts:\s*[-\d]+\s+pts_time:\s*([-\d.]+)", line)
        if frame_match:
            if frame_timestamp is not None and frame_yavg is not None:
                brightness_samples.append({"timestamp_seconds": round(frame_timestamp, 6), "yavg": round(frame_yavg, 6)})
            frame_timestamp = float(frame_match.group(1))
            frame_yavg = None
        yavg_match = re.search(r"(?:lavfi\.signalstats\.)?YAVG=([-+\d.eE]+)", line)
        if yavg_match and frame_timestamp is not None:
            try:
                frame_yavg = float(yavg_match.group(1))
            except ValueError:
                frame_yavg = None
        match = re.search(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", line)
        if match:
            black.append(_interval("black", *map(float, match.groups())))
        match = re.search(r"freeze_start:\s*([\d.]+)", line)
        if match:
            freeze_start = float(match.group(1))
        end_match = re.search(r"freeze_end:\s*([\d.]+)", line)
        duration_match = re.search(r"freeze_duration:\s*([\d.]+)", line)
        if freeze_start is not None and (end_match or duration_match):
            end = float(end_match.group(1)) if end_match else freeze_start + float(duration_match.group(1))
            freeze.append(_interval("freeze", freeze_start, end, max(0.0, end - freeze_start)))
            freeze_start = None
        match = re.search(r"silence_start:\s*([\d.]+)", line)
        if match:
            silence_start = float(match.group(1))
        end_match = re.search(r"silence_end:\s*([\d.]+)", line)
        duration_match = re.search(r"silence_duration:\s*([\d.]+)", line)
        if silence_start is not None and (end_match or duration_match):
            end = float(end_match.group(1)) if end_match else silence_start + float(duration_match.group(1))
            silence.append(_interval("silence", silence_start, end, max(0.0, end - silence_start)))
            silence_start = None
        match = re.search(r"showinfo.*?pts_time:([\d.]+)", line)
        if match:
            scene_changes.append(round(float(match.group(1)), 6))
        match = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", line)
        if match:
            crops.append([int(value) for value in match.groups()])
        match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", line)
        if match:
            max_volume = float(match.group(1))
        match = re.search(r"Number of (?:NaNs|Infs|denormals):\s*(\d+)", line, re.IGNORECASE)
        if match:
            invalid_sample_count = max(int(match.group(1)), int(invalid_sample_count or 0))
    if frame_timestamp is not None and frame_yavg is not None:
        brightness_samples.append({"timestamp_seconds": round(frame_timestamp, 6), "yavg": round(frame_yavg, 6)})
    return {
        "events": {"black": black, "freeze": freeze, "silence": silence, "flash": flashes, "scene_change": scene_changes},
        "brightness_samples": brightness_samples,
        "crop_observations": crops[-500:],
        "max_volume_db": max_volume,
        "invalid_sample_count": invalid_sample_count,
    }


def _detect_flash_events(samples: list[Mapping[str, Any]], thresholds: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in samples:
        timestamp = _finite(item.get("timestamp_seconds"))
        yavg = _finite(item.get("yavg"))
        if timestamp is None or yavg is None:
            continue
        rows.append((timestamp, yavg))
    rows.sort(key=lambda item: item[0])
    if len(rows) < 3:
        return []
    delta_threshold = max(1.0, float(thresholds.get("flash_brightness_delta") or 45.0))
    reversal_window = max(0.05, float(thresholds.get("flash_reversal_window_seconds") or 0.75))
    transitions: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        delta = current[1] - previous[1]
        if abs(delta) < delta_threshold:
            continue
        transitions.append({
            "timestamp_seconds": round(current[0], 6),
            "brightness_before": round(previous[1], 3),
            "brightness_after": round(current[1], 3),
            "delta_brightness": round(delta, 3),
            "direction": "up" if delta > 0 else "down",
        })
    flash_indexes: set[int] = set()
    for index, transition in enumerate(transitions):
        for next_index in range(index + 1, len(transitions)):
            following = transitions[next_index]
            if following["timestamp_seconds"] - transition["timestamp_seconds"] > reversal_window:
                break
            if transition["direction"] != following["direction"]:
                flash_indexes.update((index, next_index))
                break
    events: list[dict[str, Any]] = []
    for index in sorted(flash_indexes):
        transition = dict(transitions[index])
        transition["kind"] = "flash"
        transition["cluster_id"] = f"flash-{index + 1}"
        events.append(transition)
    return events


def _probe_stream_details(ffprobe: str, output: Path) -> dict[str, Any]:
    command = [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=60)
        if result.returncode != 0:
            return {"ok": False, "error_code": "ffprobe_stream_details_failed"}
        raw = json.loads(result.stdout)
        video = next((item for item in raw.get("streams") or [] if item.get("codec_type") == "video"), {})
        audio = next((item for item in raw.get("streams") or [] if item.get("codec_type") == "audio"), {})
        rotation = _rotation(video)
        return {
            "ok": bool(video),
            "format_name": str(_mapping(raw.get("format")).get("format_name") or ""),
            "video_stream_count": sum(1 for item in raw.get("streams") or [] if item.get("codec_type") == "video"),
            "audio_stream_count": sum(1 for item in raw.get("streams") or [] if item.get("codec_type") == "audio"),
            "sample_aspect_ratio": str(video.get("sample_aspect_ratio") or ""),
            "display_aspect_ratio": str(video.get("display_aspect_ratio") or ""),
            "rotation_degrees": rotation,
            "audio_channel_layout": str(audio.get("channel_layout") or ""),
            "faststart": _has_faststart(output),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error_code": _safe_error(exc)}


def _generate_contact_sheet(ffmpeg: str, output: Path, target: Path, duration: float, *, runner: Any | None = None) -> bool:
    interval = max(0.20, float(duration or 1.0) / 12.0)
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(output), "-vf", f"fps=1/{interval:.6f},scale=320:-2,tile=4x3:padding=4:margin=4", "-frames:v", "1", str(target)]
    return _run_evidence_command(command, target, runner=runner)


def _generate_event_strip(ffmpeg: str, output: Path, target: Path, timestamp: float, *, runner: Any | None = None) -> bool:
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{max(0.0, timestamp - 0.5):.6f}", "-i", str(output), "-t", "1.0", "-vf", "fps=3,scale=320:-2,tile=3x1:padding=3:margin=3", "-frames:v", "1", str(target)]
    return _run_evidence_command(command, target, runner=runner)


def _generate_dense_contact_sheet(ffmpeg: str, output: Path, target: Path, timestamp: float, *, runner: Any | None = None) -> bool:
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{max(0.0, timestamp - 2.0):.6f}", "-i", str(output), "-t", "4.0", "-vf", "fps=3,scale=240:-2,tile=4x3:padding=3:margin=3", "-frames:v", "1", str(target)]
    return _run_evidence_command(command, target, runner=runner)


def _run_evidence_command(command: list[str], target: Path, *, runner: Any | None = None) -> bool:
    try:
        result = runner.run(command, capture_output=True, text=True, check=False, expected_duration_seconds=None) if runner is not None else subprocess.run(command, capture_output=True, check=False, timeout=120)
        return result.returncode == 0 and target.is_file() and target.stat().st_size > 0
    except (OSError, subprocess.TimeoutExpired):
        target.unlink(missing_ok=True)
        return False


def _check(check_id: str, status: str, summary: str, metrics: Mapping[str, Any], *, severity: str = "info", remediation: str = "") -> dict[str, Any]:
    if status not in QA_STATUSES:
        raise ValueError(f"invalid QA check status: {status}")
    if status != "pass" and not remediation:
        raise ValueError(f"non-pass QA check requires remediation: {check_id}")
    return {
        "check_id": str(check_id),
        "schema_version": QA_CHECK_SCHEMA_VERSION,
        "status": status,
        "severity": str(severity),
        "summary": str(summary),
        "metrics": _safe_value(dict(metrics)),
        "evidence_artifact_ids": [],
        "threshold_source": {"contract_version": QA_CONTRACT_VERSION, "thresholds": {}},
        "remediation": str(remediation),
    }


def _check_summary(checks: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("status") or "") for item in checks)
    return {status: int(counts.get(status, 0)) for status in ("pass", "warning", "blocked", "skipped")}


def _artifact_record(run_uuid: str, run_root: Path, path: Path, artifact_type: str, check_id: str, *, timestamp: float | None = None) -> dict[str, Any]:
    relative = path.relative_to(run_root).as_posix()
    digest = sha256_file(path)
    record = {
        "artifact_id": "qa-artifact-" + hashlib.sha256(f"{run_uuid}\0{relative}\0{digest}".encode("utf-8")).hexdigest()[:24],
        "type": str(artifact_type),
        "check_id": str(check_id),
        "relative_path": relative,
        "sha256": digest,
        "size_bytes": int(path.stat().st_size),
    }
    if timestamp is not None:
        record["timestamp_seconds"] = round(float(timestamp), 6)
    return record


def _public_artifact(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("artifact_id", "type", "check_id", "timestamp_seconds", "sha256", "size_bytes") if record.get(key) is not None}


def _register_bundle(cfg: Mapping[str, Any], project_id: int, run_root: Path, run_uuid: str, job_id: str) -> None:
    for path in [item for item in run_root.rglob("*") if item.is_file() and not item.is_symlink()]:
        try:
            register_artifact(
                cfg,
                int(project_id),
                path,
                "qa_report" if path.name in {"report.json", "REPORT.md"} else "qa_evidence",
                state="current",
                references=[f"qa_run:{run_uuid}", f"render_job:{job_id}"],
                generation=run_uuid,
                producer_job_id=job_id,
                producer_job_status="succeeded",
            )
        except Exception:
            # Artifact inventory can be reconciled independently.  Never turn
            # a completed report into a false render/provider failure.
            continue


def _approval_is_current(cfg: Mapping[str, Any], project_id: int, report: Mapping[str, Any]) -> bool:
    review = _read_json(project_dir(dict(cfg), int(project_id)) / "review_status.json")
    snapshot = _mapping(report.get("approval_snapshot"))
    expected_id = str(snapshot.get("snapshot_id") or "")
    expected_hash = str(snapshot.get("snapshot_hash") or "")
    if not expected_id or not expected_hash:
        return False
    return (
        review.get("approved_by_user") is True
        and str(review.get("status") or "") == "approved"
        and str(review.get("approval_snapshot_id") or "") == expected_id
        and str(review.get("approval_snapshot_hash") or "") == expected_hash
    )


def _output_metadata_is_current(cfg: Mapping[str, Any], project_id: int, report: Mapping[str, Any]) -> bool:
    from .render_job_store import RenderJobStore

    job = RenderJobStore(dict(cfg)).get(str(report.get("render_job_uuid") or ""))
    if not job or int(job.get("project_id") or 0) != int(project_id) or job.get("status") != "succeeded":
        return False
    output = Path(str(job.get("output_path") or "")).expanduser().resolve()
    expected = _mapping(report.get("output_fingerprint"))
    try:
        return (
            output.is_file()
            and not output.is_symlink()
            and int(output.stat().st_size) == int(expected.get("size_bytes") or -1)
            and int(output.stat().st_mtime_ns) == int(expected.get("mtime_ns") or -1)
        )
    except OSError:
        return False


def _assert_output_unchanged(cfg: Mapping[str, Any], project_id: int, report: Mapping[str, Any]) -> None:
    output = delivery_qa_output_path(cfg, int(project_id), str(report.get("qa_run_uuid") or ""))
    expected = _mapping(report.get("output_fingerprint"))
    if sha256_file(output) != str(expected.get("sha256") or ""):
        raise DeliveryQAError("qa_output_changed", "正式輸出在 QA 後已變更；請重新執行 QA", status=409)


def _validated_run_root(cfg: Mapping[str, Any], project_id: int, run_uuid: str) -> Path:
    token = str(run_uuid or "").strip()
    if not token or not _TOKEN.fullmatch(token):
        raise ValueError("invalid QA run identity")
    root = delivery_qa_root(cfg, int(project_id)).resolve()
    candidate = (root / token).resolve()
    if root not in candidate.parents or not candidate.is_dir() or candidate.is_symlink():
        raise FileNotFoundError("QA run not found")
    return candidate


def _empty_qa(project_id: int) -> dict[str, Any]:
    return {
        "schema_version": QA_RUN_SCHEMA_VERSION,
        "exists": False,
        "project_id": int(project_id),
        "lifecycle_status": "needs_qa",
        "deliverable_ready": False,
        "summary": {"pass": 0, "warning": 0, "blocked": 0, "skipped": 0},
        "checks": [],
        "human_review": {"status": "pending", "review_version": 0, "warning_acceptances": []},
        "sensitive_data_redacted": True,
    }


def _report_markdown(report: Mapping[str, Any]) -> str:
    summary = _mapping(report.get("summary"))
    lines = [
        "# Formal Delivery QA",
        "",
        f"- QA run: `{report.get('qa_run_uuid', '')}`",
        f"- Render job: `{report.get('render_job_uuid', '')}`",
        f"- Contract: `{_mapping(report.get('contract')).get('version', '')}`",
        f"- Profile: `{_mapping(report.get('profile')).get('profile_id', '')}`",
        f"- State: `{report.get('lifecycle_status', '')}`",
        f"- Pass / warning / blocked / skipped: {summary.get('pass', 0)} / {summary.get('warning', 0)} / {summary.get('blocked', 0)} / {summary.get('skipped', 0)}",
        "",
        "## Checks",
        "",
    ]
    for check in report.get("checks") or []:
        lines.extend([
            f"### {check.get('check_id')} — {check.get('status')}",
            "",
            str(check.get("summary") or ""),
            "",
            f"Remediation: {check.get('remediation') or 'None'}",
            "",
        ])
    lines.extend(["## Human final preview", "", "Automated QA does not replace watching the final output. Delivery requires an explicit local-user confirmation.", ""])
    return "\n".join(lines)


def _assert_safe_report(report: Mapping[str, Any]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    absolute = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|/(?:Users|home|mnt|var|tmp)/)", re.IGNORECASE)
    sensitive = re.compile(r"(?:authorization|api[_-]?key|bearer\s+|password|secret\s*[:=]|://[^\s/@:]+:[^\s/@]+@)", re.IGNORECASE)
    if absolute.search(serialized) or sensitive.search(serialized):
        raise DeliveryQAError("unsafe_qa_report", "Delivery QA report 含有不可匯出的敏感資料")
    if report.get("sensitive_data_redacted") is not True:
        raise DeliveryQAError("unsafe_qa_report", "Delivery QA report 未標記去敏 invariant")


def _safe_value(value: Any) -> Any:
    from .doctor import _redact_value

    return _redact_value(value)


def _safe_error(exc: object) -> str:
    raw = str(exc)
    if not raw and isinstance(exc, BaseException):
        raw = exc.__class__.__name__
    value = _safe_value(raw)
    return str(value or "operation_failed")


def _safe_user_text(value: object) -> str:
    return str(_safe_value(str(value or "").strip()) or "").strip()


def _attach_threshold_audit(checks: list[dict[str, Any]], profile: Mapping[str, Any]) -> None:
    keys = {
        "container_manifest": ("duration_tolerance_seconds",),
        "black_flash": ("black_block_seconds", "black_cluster_window_seconds", "black_cluster_count_warning", "edge_fade_tolerance_seconds", "flash_cluster_window_seconds", "flash_cluster_count_warning", "flash_brightness_delta", "flash_reversal_window_seconds", "scene_change_threshold"),
        "freeze_silence": ("freeze_warning_seconds", "silence_warning_seconds", "freeze_silence_block_seconds"),
        "border_crop_safe_area": ("border_tolerance_pixels",),
        "audio": ("loudness_min_lufs", "loudness_max_lufs", "true_peak_limit_db", "clipping_peak_warning_db", "av_tail_tolerance_seconds", "bgm_tail_tolerance_seconds"),
        "continuity_repeat": ("repeat_source_warning_count",),
    }
    values = _mapping(profile.get("resolved_thresholds"))
    sources = _mapping(profile.get("threshold_sources"))
    for check in checks:
        selected = keys.get(str(check.get("check_id") or ""), ())
        check["threshold_source"] = {
            "contract_version": QA_CONTRACT_VERSION,
            "profile_id": str(profile.get("profile_id") or "general_diary"),
            "thresholds": {key: {"value": values.get(key), "source": sources.get(key)} for key in selected},
        }


def _apply_threshold_override(
    target: dict[str, float | int],
    sources: dict[str, str],
    override: Any,
    source: str,
    validation: dict[str, Any],
) -> None:
    if override in (None, {}):
        return
    if not isinstance(override, Mapping):
        _record_invalid_threshold_override(validation, source, "override must be an object")
        return
    _merge_thresholds(target, sources, override, source, validation=validation)


def _record_invalid_threshold_override(validation: dict[str, Any], source: str, reason: str, key: str = "") -> None:
    validation.setdefault("invalid_overrides", []).append({
        "source": str(source),
        "key": str(key or "<override>"),
        "reason": str(reason),
    })


def _threshold_audit_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return str(value)
        return number if math.isfinite(number) else str(value)
    return _safe_user_text(value)[:120]


def _merge_thresholds(
    target: dict[str, float | int],
    sources: dict[str, str],
    override: Mapping[str, Any],
    source: str,
    *,
    validation: dict[str, Any] | None = None,
) -> None:
    for raw_key, raw_value in override.items():
        key = str(raw_key)
        if key not in target:
            if validation is not None:
                validation.setdefault("unknown_thresholds", []).append({
                    "source": str(source),
                    "key": key,
                    "value": _threshold_audit_value(raw_value),
                })
            continue
        current = target[key]
        try:
            if isinstance(raw_value, bool):
                raise ValueError("boolean is not a numeric threshold")
            number = float(raw_value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("threshold must be finite and non-negative")
            if isinstance(current, int) and not isinstance(current, bool):
                if not number.is_integer():
                    raise ValueError("integer threshold must be a whole number")
                parsed: float | int = int(number)
            else:
                parsed = number
        except (TypeError, ValueError, OverflowError):
            if validation is not None:
                _record_invalid_threshold_override(validation, source, "value must be finite, non-negative, and match threshold type", key)
            continue
        target[key] = parsed
        sources[key] = source
        if validation is not None:
            validation.setdefault("valid_overrides", []).append({
                "source": str(source),
                "key": key,
                "resolved_value": parsed,
            })


def _rotation(video: Mapping[str, Any]) -> int:
    try:
        tag = _mapping(video.get("tags")).get("rotate")
        if tag not in (None, ""):
            return int(round(float(tag))) % 360
        for item in video.get("side_data_list") or []:
            if isinstance(item, Mapping) and item.get("rotation") not in (None, ""):
                return int(round(float(item["rotation"]))) % 360
    except (TypeError, ValueError):
        return 0
    return 0


def _has_faststart(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            sample = stream.read(min(path.stat().st_size, 8 * 1024 * 1024))
        moov = sample.find(b"moov")
        mdat = sample.find(b"mdat")
        return moov >= 0 and (mdat < 0 or moov < mdat)
    except OSError:
        return False


def _interval(kind: str, start: float, end: float, duration: float) -> dict[str, Any]:
    return {"kind": kind, "start_seconds": round(float(start), 6), "end_seconds": round(float(end), 6), "duration_seconds": round(float(duration), 6)}


def _interval_overlaps(left: list[Mapping[str, Any]], right: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    overlaps: list[dict[str, Any]] = []
    for first in left:
        for second in right:
            start = max(float(first.get("start_seconds") or 0), float(second.get("start_seconds") or 0))
            end = min(float(first.get("end_seconds") or 0), float(second.get("end_seconds") or 0))
            if end > start:
                overlaps.append(_interval("freeze_silence_overlap", start, end, end - start))
    return overlaps


def _expected_duration(manifest: Mapping[str, Any]) -> float:
    visual = _mapping(manifest.get("visual_timeline"))
    candidates = (visual.get("resolved_duration_seconds"), manifest.get("expected_duration_seconds"))
    for candidate in candidates:
        value = _finite(candidate)
        if value is not None and value > 0:
            return value
    return sum(
        max(0.0, float(_mapping(item).get("timeline_duration_seconds") or 0))
        for item in manifest.get("segments") or []
        if isinstance(item, Mapping)
    )


def _timeline_segments(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = 0.0
    for item in manifest.get("segments") or []:
        if not isinstance(item, Mapping):
            continue
        duration = max(0.0, float(item.get("timeline_duration_seconds") or 0))
        stable_id = str(item.get("segment_uuid") or item.get("segment_id") or "")
        rows.append({
            "stable_segment_uuid": stable_id,
            "group_id": str(item.get("group_id") or ""),
            "start_seconds": round(cursor, 6),
            "end_seconds": round(cursor + duration, 6),
        })
        cursor += duration
    return rows


def _annotate_check_events(checks: list[dict[str, Any]], manifest: Mapping[str, Any]) -> None:
    timeline = _timeline_segments(manifest)
    for check in checks:
        metrics = check.get("metrics")
        if not isinstance(metrics, dict) or not isinstance(metrics.get("events"), list):
            continue
        for event in metrics["events"]:
            if not isinstance(event, dict):
                continue
            timestamp = _finite(event.get("start_seconds", event.get("timestamp_seconds")))
            if timestamp is None:
                continue
            event["output_timestamp_seconds"] = round(max(0.0, timestamp), 6)
            segment = next(
                (
                    item
                    for item in timeline
                    if float(item["start_seconds"]) <= timestamp < float(item["end_seconds"]) + 0.000001
                ),
                timeline[-1] if timeline else None,
            )
            if segment and segment.get("stable_segment_uuid"):
                event["stable_segment_uuid"] = segment["stable_segment_uuid"]


def _chapter_starts(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _timeline_segments(manifest):
        group_id = str(item.get("group_id") or "")
        if not group_id or group_id in seen:
            continue
        seen.add(group_id)
        chapters.append({
            "start_seconds": float(item["start_seconds"]),
            "stable_segment_uuid": str(item.get("stable_segment_uuid") or ""),
        })
    return chapters


def _max_cluster(values: list[float], window: float) -> int:
    ordered = sorted(float(value) for value in values)
    best = 0
    start = 0
    for end, value in enumerate(ordered):
        while start <= end and value - ordered[start] > window:
            start += 1
        best = max(best, end - start + 1)
    return best


def _event_rows(metrics: Any) -> list[dict[str, Any]]:
    if not isinstance(metrics, Mapping):
        return []
    return [dict(item) for item in metrics.get("events") or [] if isinstance(item, Mapping)]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _load_render_report(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file() or path.is_symlink():
        return {}, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}, "unparseable"
    if not isinstance(value, dict) or not value:
        return {}, "invalid"
    return value, "ok"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the transactional sibling short enough for Windows paths that are
    # already near the legacy 260-character boundary.
    temp = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "DeliveryQAError",
    "QAReviewVersionConflict",
    "QA_CHECK_SCHEMA_VERSION",
    "QA_CONTRACT_VERSION",
    "QA_LIFECYCLE_STATES",
    "QA_RUN_SCHEMA_VERSION",
    "delivery_qa_artifact_path",
    "delivery_qa_for_api",
    "delivery_qa_output_path",
    "load_current_delivery_qa",
    "resolve_qa_profile",
    "rerun_current_delivery_qa",
    "review_delivery_qa",
    "run_delivery_qa",
]
