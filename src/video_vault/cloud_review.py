"""Optional, bounded cloud review for selected local perception windows.

The local perception result remains the source of truth.  This module only
builds an auditable review plan and stores provider responses; it never accepts
an entire video as input and never mutates local frames or stable segments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol
import json

from .analyzer.cloud_provider import CloudProvider


CLOUD_REVIEW_CONTRACT_VERSION = "cloud-review-v1"
DEFAULT_CLOUD_REVIEW = {
    "enabled": False,
    "provider": "mock",
    "confidence_threshold": 0.55,
    "max_calls_per_clip": 3,
    "max_frames_per_clip": 12,
    "max_calls_per_project": 6,
    "max_frames_per_project": 24,
    "estimated_cost_per_frame_usd": 0.0,
    "max_estimated_cost_usd_per_clip": 0.12,
    "max_estimated_cost_usd_per_project": 0.24,
    "timeout_seconds": 60,
}
RULE_CONFLICT_REASONS = {
    "rule_conflict",
    "duplicate_conflict",
    "window_validation_warning",
    "conflicting_rule",
}


class CloudReviewError(RuntimeError):
    """A cloud review could not be completed."""


class CloudReviewProvider(Protocol):
    name: str
    model: str

    def review_window(
        self,
        frame_paths: list[Path],
        timestamps: list[float],
        context: Mapping[str, Any],
    ) -> tuple[dict, dict]: ...


def cloud_review_config(cfg: Mapping[str, Any]) -> dict:
    configured = ((cfg.get("perception") or {}).get("cloud_review") or {})
    result = dict(DEFAULT_CLOUD_REVIEW)
    if isinstance(configured, Mapping):
        result.update(configured)
    result["confidence_threshold"] = min(1.0, max(0.0, float(result["confidence_threshold"])))
    for key in ("max_calls_per_clip", "max_frames_per_clip", "max_calls_per_project", "max_frames_per_project"):
        result[key] = max(0, int(result[key]))
    result["estimated_cost_per_frame_usd"] = max(0.0, float(result["estimated_cost_per_frame_usd"]))
    for key in ("max_estimated_cost_usd_per_clip", "max_estimated_cost_usd_per_project"):
        result[key] = max(0.0, float(result[key]))
    result["timeout_seconds"] = max(1.0, float(result["timeout_seconds"]))
    return result


def empty_review_usage() -> dict:
    return {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0, "by_clip": {}}


def normalise_review_usage(value: Mapping[str, Any] | None) -> dict:
    result = empty_review_usage()
    if not isinstance(value, Mapping):
        return result
    result["calls"] = max(0, int(value.get("calls") or 0))
    result["frames"] = max(0, int(value.get("frames") or 0))
    result["estimated_cost_usd"] = max(0.0, float(value.get("estimated_cost_usd") or 0.0))
    raw_by_clip = value.get("by_clip") or {}
    if isinstance(raw_by_clip, Mapping):
        for clip_id, raw in raw_by_clip.items():
            if not isinstance(raw, Mapping):
                continue
            result["by_clip"][str(clip_id)] = {
                "calls": max(0, int(raw.get("calls") or 0)),
                "frames": max(0, int(raw.get("frames") or 0)),
                "estimated_cost_usd": max(0.0, float(raw.get("estimated_cost_usd") or 0.0)),
            }
    return result


def add_review_usage(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict:
    result = normalise_review_usage(left)
    increment = normalise_review_usage(right)
    result["calls"] += increment["calls"]
    result["frames"] += increment["frames"]
    result["estimated_cost_usd"] = round(result["estimated_cost_usd"] + increment["estimated_cost_usd"], 6)
    for clip_id, raw in increment["by_clip"].items():
        current = result["by_clip"].setdefault(clip_id, {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0})
        current["calls"] += raw["calls"]
        current["frames"] += raw["frames"]
        current["estimated_cost_usd"] = round(current["estimated_cost_usd"] + raw["estimated_cost_usd"], 6)
    return result


def review_usage_from_audits(audits: list[Mapping[str, Any] | None]) -> dict:
    result = empty_review_usage()
    for audit in audits:
        if isinstance(audit, Mapping):
            result = add_review_usage(result, audit.get("usage") if isinstance(audit.get("usage"), Mapping) else None)
    return result


def _window_reasons(window: Mapping[str, Any], threshold: float, explicit: bool) -> list[str]:
    reasons = {
        str(reason)
        for reason in window.get("review_reasons") or []
        if str(reason) in RULE_CONFLICT_REASONS or "conflict" in str(reason)
    }
    validation = window.get("validation") or {}
    validation_reasons = {str(reason) for reason in validation.get("needs_review_reasons") or []}
    reasons.update(reason for reason in validation_reasons if reason in RULE_CONFLICT_REASONS or "conflict" in reason)
    confidence = float(window.get("confidence") or 0.0)
    if confidence < threshold:
        reasons.add("low_confidence")
    if reasons & RULE_CONFLICT_REASONS:
        reasons.add("rule_conflict")
    if explicit:
        reasons.add("user_selected")
    return sorted(reasons)


def build_review_plan(
    windows: list[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    *,
    selected_window_ids: set[str] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict:
    """Return a public plan without frame paths or source-media identifiers."""

    policy = cloud_review_config(cfg)
    if not bool(policy.get("enabled")):
        return {
            "contract_version": CLOUD_REVIEW_CONTRACT_VERSION,
            "status": "disabled",
            "provider": str(policy["provider"]),
            "policy": policy,
            "windows": [],
            "rejected_windows": [],
            "estimated_calls": 0,
            "estimated_frames": 0,
            "estimated_cost_usd": 0.0,
            "privacy": {
                "full_video_upload": False,
                "payload": "selected_frames_only",
                "source_paths_exposed": False,
            },
        }
    selected = selected_window_ids
    current_usage = normalise_review_usage(usage)
    budget_limits = {
        "max_calls_per_clip": policy["max_calls_per_clip"],
        "max_frames_per_clip": policy["max_frames_per_clip"],
        "max_estimated_cost_usd_per_clip": policy["max_estimated_cost_usd_per_clip"],
        "max_calls_per_project": policy["max_calls_per_project"],
        "max_frames_per_project": policy["max_frames_per_project"],
        "max_estimated_cost_usd_per_project": policy["max_estimated_cost_usd_per_project"],
    }
    candidates = []
    for source in windows:
        window_uuid = str(source.get("window_uuid") or "")
        if not window_uuid:
            continue
        explicit = selected is not None and window_uuid in selected
        reasons = _window_reasons(source, float(policy["confidence_threshold"]), explicit)
        if selected is not None and not explicit:
            continue
        if not reasons and not explicit:
            continue
        frame_count = len(source.get("frame_timestamps") or source.get("frames") or [])
        candidates.append({
            "project_id": int(source.get("project_id") or 0),
            "video_id": int(source.get("video_id") or 0),
            "run_uuid": str(source.get("run_uuid") or ""),
            "window_uuid": window_uuid,
            "segment_uuid": str(source.get("segment_uuid") or ""),
            "ordinal": int(source.get("ordinal") or 0),
            "start_seconds": float(source.get("start_seconds") or 0),
            "end_seconds": float(source.get("end_seconds") or 0),
            "frame_timestamps": [float(value) for value in source.get("frame_timestamps") or []],
            "frame_count": frame_count,
            "confidence": float(source.get("confidence") or 0),
            "reasons": reasons,
            "source_paths_exposed": False,
        })
    candidates.sort(key=lambda item: (item["video_id"], item["ordinal"], item["window_uuid"]))
    accepted = []
    rejected = []
    calls_by_clip = {str(clip_id): int(raw.get("calls") or 0) for clip_id, raw in current_usage["by_clip"].items()}
    frames_by_clip = {str(clip_id): int(raw.get("frames") or 0) for clip_id, raw in current_usage["by_clip"].items()}
    costs_by_clip = {str(clip_id): float(raw.get("estimated_cost_usd") or 0.0) for clip_id, raw in current_usage["by_clip"].items()}
    total_calls = current_usage["calls"]
    total_frames = current_usage["frames"]
    total_cost = current_usage["estimated_cost_usd"]
    for item in candidates:
        clip_id = str(item["video_id"])
        item_cost = item["frame_count"] * policy["estimated_cost_per_frame_usd"]
        next_calls = calls_by_clip.get(clip_id, 0) + 1
        next_clip_frames = frames_by_clip.get(clip_id, 0) + item["frame_count"]
        next_clip_cost = costs_by_clip.get(clip_id, 0.0) + item_cost
        if next_calls > policy["max_calls_per_clip"] or next_clip_frames > policy["max_frames_per_clip"]:
            item["rejected_reason"] = "clip_budget_exceeded"
            rejected.append(item)
            continue
        if next_clip_cost > policy["max_estimated_cost_usd_per_clip"]:
            item["rejected_reason"] = "clip_cost_budget_exceeded"
            rejected.append(item)
            continue
        if total_calls + 1 > policy["max_calls_per_project"] or total_frames + item["frame_count"] > policy["max_frames_per_project"]:
            item["rejected_reason"] = "project_budget_exceeded"
            rejected.append(item)
            continue
        if total_cost + item_cost > policy["max_estimated_cost_usd_per_project"]:
            item["rejected_reason"] = "project_cost_budget_exceeded"
            rejected.append(item)
            continue
        accepted.append(item)
        calls_by_clip[clip_id] = next_calls
        frames_by_clip[clip_id] = next_clip_frames
        costs_by_clip[clip_id] = next_clip_cost
        total_calls += 1
        total_frames += item["frame_count"]
        total_cost += item_cost
    status = "ready" if accepted else ("budget_exceeded" if rejected else "no_eligible_windows")
    return {
        "contract_version": CLOUD_REVIEW_CONTRACT_VERSION,
        "status": status,
        "provider": str(policy["provider"]),
        "policy": policy,
        "windows": accepted,
        "rejected_windows": rejected,
        "estimated_calls": total_calls - current_usage["calls"],
        "estimated_frames": total_frames - current_usage["frames"],
        "estimated_cost_usd": round(total_cost - current_usage["estimated_cost_usd"], 6),
        "budget_usage": current_usage,
        "budget_limits": budget_limits,
        "privacy": {
            "full_video_upload": False,
            "payload": "selected_frames_only",
            "source_paths_exposed": False,
        },
    }


class MockCloudReviewProvider:
    name = "mock"
    model = "cloud-review-mock-v1"

    def review_window(self, frame_paths: list[Path], timestamps: list[float], context: Mapping[str, Any]) -> tuple[dict, dict]:
        if not frame_paths or len(frame_paths) != len(timestamps):
            raise CloudReviewError("cloud review frame payload is empty or mismatched")
        return (
            {
                "review_status": "completed",
                "disposition": "needs_human_confirmation",
                "confidence": round(float(context.get("local_confidence") or 0), 6),
                "notes": "mock cloud review; local result remains authoritative",
            },
            {"provider": self.name, "model": self.model, "frame_count": len(frame_paths)},
        )


class OpenAICloudReviewProvider:
    """OpenAI adapter behind the generic CloudReviewProvider contract."""

    name = "openai"

    def __init__(self, cfg: Mapping[str, Any]):
        policy = cloud_review_config(cfg)
        cloud = dict(((cfg.get("ai") or {}).get("cloud_review") or {}))
        if not cloud:
            cloud = dict(((cfg.get("ai") or {}).get("cloud") or {}))
        self.model = str(cloud.get("model") or "gpt-4.1-mini")
        cloud["timeout_seconds"] = policy["timeout_seconds"]
        adapter_cfg = {"ai": {"cloud": cloud}}
        self._provider = CloudProvider(adapter_cfg)

    def review_window(self, frame_paths: list[Path], timestamps: list[float], context: Mapping[str, Any]) -> tuple[dict, dict]:
        parsed, raw = self._provider.analyze_window(frame_paths, timestamps, dict(context))
        return ({"review_status": "completed", "disposition": "needs_human_confirmation", **parsed}, raw)


def cloud_review_provider(cfg: Mapping[str, Any]) -> CloudReviewProvider:
    policy = cloud_review_config(cfg)
    if not bool(policy.get("enabled")):
        raise CloudReviewError("cloud review is disabled")
    name = str(policy.get("provider") or "mock").lower()
    if name == "mock":
        return MockCloudReviewProvider()
    if name == "openai":
        return OpenAICloudReviewProvider(cfg)
    raise CloudReviewError(f"unsupported cloud review provider: {name}")


def execute_review_plan(
    plan: Mapping[str, Any],
    frame_paths_by_window: Mapping[str, list[Path]],
    cfg: Mapping[str, Any],
) -> dict:
    """Execute selected frames and fail closed on any provider failure."""

    attempted_window_uuids: list[str] = []
    policy = cloud_review_config(cfg)
    try:
        provider = cloud_review_provider(cfg)
    except CloudReviewError as exc:
        return {
            "contract_version": str(plan.get("contract_version") or CLOUD_REVIEW_CONTRACT_VERSION),
            "status": "failed",
            "provider": str(plan.get("provider") or "unknown"),
            "error": str(exc),
            "results": [],
            "completed_count": 0,
            "attempted_window_uuids": attempted_window_uuids,
            "usage": empty_review_usage(),
            "local_result_preserved": True,
            "project_needs_review": True,
        }
    results = []
    for item in plan.get("windows") or []:
        window_uuid = str(item.get("window_uuid") or "")
        paths = list(frame_paths_by_window.get(window_uuid) or [])
        timestamps = [float(value) for value in item.get("frame_timestamps") or []]
        attempted_window_uuids.append(window_uuid)
        try:
            parsed, raw = provider.review_window(paths, timestamps, {
                "local_confidence": item.get("confidence"),
                "window_uuid": window_uuid,
                "segment_uuid": item.get("segment_uuid"),
            })
            results.append({
                "window_uuid": window_uuid,
                "segment_uuid": str(item.get("segment_uuid") or ""),
                "status": "completed",
                "provider": provider.name,
                "model": provider.model,
                "result": parsed,
                "audit": {"frame_count": len(paths), "raw": _scrub_raw(raw)},
            })
        except Exception as exc:  # provider failures must never become success
            return {
                "contract_version": CLOUD_REVIEW_CONTRACT_VERSION,
                "status": "failed",
                "provider": provider.name,
                "model": provider.model,
                "error": str(exc),
                "results": results,
                "completed_count": len(results),
                "failed_window_uuid": window_uuid,
                "attempted_window_uuids": attempted_window_uuids,
                "usage": _attempted_usage(plan, attempted_window_uuids, policy),
                "local_result_preserved": True,
                "project_needs_review": True,
            }
    return {
        "contract_version": CLOUD_REVIEW_CONTRACT_VERSION,
        "status": "completed",
        "provider": provider.name,
        "model": provider.model,
        "results": results,
        "completed_count": len(results),
        "attempted_window_uuids": attempted_window_uuids,
        "usage": _attempted_usage(plan, attempted_window_uuids, policy),
    }


def _attempted_usage(plan: Mapping[str, Any], attempted_window_uuids: list[str], policy: Mapping[str, Any]) -> dict:
    usage = empty_review_usage()
    attempted = set(attempted_window_uuids)
    for item in plan.get("windows") or []:
        if str(item.get("window_uuid") or "") not in attempted:
            continue
        frames = int(item.get("frame_count") or len(item.get("frame_timestamps") or []))
        clip_id = str(item.get("video_id") or "")
        cost = frames * float(policy["estimated_cost_per_frame_usd"])
        usage["calls"] += 1
        usage["frames"] += frames
        usage["estimated_cost_usd"] = round(usage["estimated_cost_usd"] + cost, 6)
        clip = usage["by_clip"].setdefault(clip_id, {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0})
        clip["calls"] += 1
        clip["frames"] += frames
        clip["estimated_cost_usd"] = round(clip["estimated_cost_usd"] + cost, 6)
    return usage


def _scrub_raw(value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub_raw(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _scrub_raw(item)
            for key, item in value.items()
            if key not in {"frame_path", "source_path", "image_url", "api_key", "authorization"}
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
