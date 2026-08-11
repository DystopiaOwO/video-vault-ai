"""Multi-frame perception windows and evidence contracts.

This module deliberately keeps window planning separate from project publish.
It accepts an explicit frame manifest, produces deterministic windows, and
never changes live frames or segments by itself.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from typing import Any, Mapping

from ..capability_registry import resolve_verified_probe_capability
from .frame_analysis import PROMPT_VERSION, TAGS


MULTI_FRAME_SCHEMA_VERSION = 1
MULTI_FRAME_CONTRACT_VERSION = "perception-multiframe-v1"
MULTI_FRAME_PROMPT_VERSION = f"{PROMPT_VERSION}:multiframe-v1"
MIN_WINDOW_FRAMES = 3
MAX_WINDOW_FRAMES = 5
WINDOW_POLICY_NAME = "scene-aware-window"
WINDOW_POLICY_VERSION = 2
MAX_WINDOW_SPAN_SECONDS = 12.0
SUPPORTED_IMAGE_FORMATS = ("jpeg", "png", "webp")
CAPABILITY_SOURCES = {"explicit_config", "provider_metadata", "verified_probe", "built_in_mock"}


class MultiFrameError(RuntimeError):
    """Base error for a multi-frame provider or evidence contract failure."""


class MultiFrameUnsupported(MultiFrameError):
    """The configured provider cannot consume more than one image."""


class MultiFrameValidationError(MultiFrameError):
    """A window or provider response failed closed validation."""


def model_provenance(provider: Any, capability: Mapping[str, Any]) -> dict[str, Any]:
    """Return the auditable model contract attached to every formal window."""

    return {
        "provider": str(getattr(provider, "provider", "") or ""),
        "model": str(getattr(provider, "model", "") or ""),
        "prompt_version": str(getattr(provider, "multi_frame_prompt_version", MULTI_FRAME_PROMPT_VERSION)),
        "provider_contract_version": str(capability.get("provider_contract_version") or ""),
        "capability_source": str(capability.get("capability_source") or ""),
    }


def multi_frame_publish_errors(result: Mapping[str, Any]) -> list[str]:
    """Validate the minimum evidence/provenance contract before publishing."""

    errors: list[str] = []
    contract = result.get("multi_frame_contract")
    if not isinstance(contract, Mapping):
        errors.append("missing_multi_frame_contract")
        contract = {}
    if str(contract.get("status") or "") != "pass":
        errors.append("multi_frame_contract_not_pass")
    if not str(contract.get("provider") or ""):
        errors.append("missing_model_provider")
    if not str(contract.get("model") or ""):
        errors.append("missing_model_provenance")

    planned_windows = result.get("window_manifest")
    if not isinstance(planned_windows, list) or not planned_windows:
        errors.append("missing_mandatory_window_manifest")
        planned_windows = []
    planned_uuids = [
        str(window.get("window_uuid") or "")
        for window in planned_windows
        if isinstance(window, Mapping)
    ]
    if len(planned_uuids) != len(planned_windows) or any(not uuid for uuid in planned_uuids):
        errors.append("mandatory_window_manifest_has_missing_uuid")
    if len(set(planned_uuids)) != len(planned_uuids):
        errors.append("mandatory_window_manifest_has_duplicate_uuid")

    fragments = result.get("non_mandatory_evidence", [])
    if not isinstance(fragments, list):
        errors.append("non_mandatory_evidence_not_list")
        fragments = []
    fragment_uuids: list[str] = []
    for index, fragment in enumerate(fragments):
        if not isinstance(fragment, Mapping):
            errors.append(f"non_mandatory_fragment_{index}_not_object")
            continue
        fragment_uuid = str(fragment.get("fragment_uuid") or "")
        fragment_uuids.append(fragment_uuid)
        if not fragment_uuid:
            errors.append(f"non_mandatory_fragment_{index}_missing_uuid")
        if fragment.get("mandatory") is not False:
            errors.append(f"non_mandatory_fragment_{index}_marked_mandatory")
        if str(fragment.get("reason") or "") != "insufficient_scene_samples":
            errors.append(f"non_mandatory_fragment_{index}_reason_invalid")
        frames = fragment.get("frames")
        if not isinstance(frames, list) or not (0 < len(frames) < MIN_WINDOW_FRAMES):
            errors.append(f"non_mandatory_fragment_{index}_frame_count_invalid")
    if len(set(fragment_uuids)) != len(fragment_uuids):
        errors.append("non_mandatory_fragment_duplicate_uuid")
    if set(fragment_uuids).intersection(planned_uuids):
        errors.append("non_mandatory_fragment_overlaps_mandatory_uuid")

    validation = result.get("window_validation")
    checks = validation.get("checks") if isinstance(validation, Mapping) else None
    if not isinstance(checks, list) or len(checks) != len(planned_windows):
        errors.append("window_validation_coverage_missing")
        checks = []
    check_uuids = [
        str(check.get("window_uuid") or "")
        for check in checks
        if isinstance(check, Mapping)
    ]
    if len(check_uuids) != len(checks) or set(check_uuids) != set(planned_uuids):
        errors.append("window_validation_coverage_mismatch")
    if len(set(check_uuids)) != len(check_uuids):
        errors.append("window_validation_coverage_duplicate_uuid")
    if isinstance(validation, Mapping) and str(validation.get("status") or "") != "pass":
        errors.append("window_validation_not_pass")
    for check in checks:
        if isinstance(check, Mapping) and str(check.get("status") or "") != "pass":
            errors.append(
                f"mandatory_window_{str(check.get('window_uuid') or 'unknown')}_validation_not_pass"
            )

    results = result.get("window_results")
    if not isinstance(results, list) or not results:
        errors.append("missing_evidence_window_results")
        results = []
    result_uuids = [
        str(window_result.get("window_uuid") or "")
        for window_result in results
        if isinstance(window_result, Mapping)
    ]
    if len(result_uuids) != len(results) or any(not uuid for uuid in result_uuids):
        errors.append("window_result_has_missing_uuid")
    if len(set(result_uuids)) != len(result_uuids):
        errors.append("window_result_has_duplicate_uuid")
    if set(result_uuids) != set(planned_uuids):
        errors.append("window_result_coverage_mismatch")
    for index, window_result in enumerate(results):
        if not isinstance(window_result, Mapping):
            errors.append(f"window_result_{index}_is_not_an_object")
            continue
        provenance = window_result.get("model_provenance")
        if not isinstance(provenance, Mapping):
            errors.append(f"window_result_{index}_missing_model_provenance")
            continue
        if not str(provenance.get("provider") or "") or not str(provenance.get("model") or ""):
            errors.append(f"window_result_{index}_incomplete_model_provenance")
        elif (
            str(provenance.get("provider") or "") != str(contract.get("provider") or "")
            or str(provenance.get("model") or "") != str(contract.get("model") or "")
        ):
            errors.append(f"window_result_{index}_provenance_mismatch")
        validation = window_result.get("validation")
        if not isinstance(validation, Mapping) or str(validation.get("status") or "") != "pass":
            errors.append(f"window_result_{index}_validation_not_pass")
        if not str(window_result.get("window_uuid") or ""):
            errors.append(f"window_result_{index}_missing_window_uuid")
    segments = result.get("segments")
    if not isinstance(segments, list) or len(segments) != len(results):
        errors.append("published_segments_do_not_match_evidence_windows")
        segments = []
    segment_uuids = [
        str(segment.get("window_uuid") or "")
        for segment in segments
        if isinstance(segment, Mapping)
    ]
    if len(segment_uuids) != len(segments) or any(not uuid for uuid in segment_uuids):
        errors.append("published_segment_missing_window_uuid")
    if len(set(segment_uuids)) != len(segment_uuids):
        errors.append("published_segment_has_duplicate_uuid")
    if set(segment_uuids) != set(result_uuids):
        errors.append("published_segment_coverage_mismatch")
    contract_fragment_uuids = contract.get("non_mandatory_fragment_uuids") if isinstance(contract, Mapping) else None
    if contract_fragment_uuids is not None and set(str(item) for item in contract_fragment_uuids) != set(fragment_uuids):
        errors.append("non_mandatory_fragment_contract_mismatch")
    return errors


def frame_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _partition_lengths(
    count: int,
    min_frames: int = MIN_WINDOW_FRAMES,
    max_frames: int = MAX_WINDOW_FRAMES,
) -> list[int]:
    if count < min_frames:
        return []
    # Some provider limits cannot represent every count (for example, four
    # frames with an exact three-image limit). Return the largest legal prefix;
    # the planner records the remainder as non-mandatory evidence.
    for covered in range(count, min_frames - 1, -1):
        lengths: list[int] = []
        remaining = covered
        while remaining:
            if min_frames <= remaining <= max_frames:
                lengths.append(remaining)
                break
            size = max_frames
            remainder = remaining - size
            if 0 < remainder < min_frames:
                groups = (remaining + max_frames - 1) // max_frames
                base, extra = divmod(remaining, groups)
                lengths.extend([base + 1] * extra + [base] * (groups - extra))
                break
            lengths.append(size)
            remaining -= size
        if sum(lengths) == covered and all(min_frames <= length <= max_frames for length in lengths):
            return lengths
    return []


def _candidate_clusters(ordered: list[dict]) -> list[tuple[list[dict], list[str]]]:
    """Split samples at explicit scene boundaries and long temporal gaps."""

    clusters: list[tuple[list[dict], list[str]]] = []
    current: list[dict] = []
    reasons: list[str] = []
    for item in ordered:
        if not current:
            current = [item]
            continue
        previous = current[-1]
        gap = float(item["timestamp_seconds"]) - float(previous["timestamp_seconds"])
        current_reasons = set(item.get("sample_reasons") or [])
        previous_reasons = set(previous.get("sample_reasons") or [])
        hard_boundary = bool(
            current_reasons & {"scene", "hard_cut", "cut", "scene_change"}
            or previous_reasons & {"hard_cut", "cut", "scene_change"}
        )
        split_reason = ""
        if hard_boundary:
            split_reason = "scene_boundary"
        elif "boundary" in current_reasons:
            # Sampling marks clip endpoints as ``boundary``. Keep the marker
            # visible to the planner, but allow a short endpoint fragment to
            # merge when count/span constraints prove the merge is safe.
            split_reason = "sampling_boundary"
        elif gap > MAX_WINDOW_SPAN_SECONDS:
            split_reason = "large_temporal_gap"
        elif float(item["timestamp_seconds"]) - float(current[0]["timestamp_seconds"]) > MAX_WINDOW_SPAN_SECONDS:
            split_reason = "maximum_temporal_span"
        if split_reason:
            clusters.append((current, reasons or ["clip_start"]))
            current = [item]
            reasons = [split_reason]
        else:
            current.append(item)
    if current:
        clusters.append((current, reasons or ["clip_end"]))
    return clusters


def _cluster_span(cluster: list[dict]) -> float:
    if not cluster:
        return 0.0
    return float(cluster[-1]["timestamp_seconds"]) - float(cluster[0]["timestamp_seconds"])


def _merge_allowed(
    left: list[dict],
    right: list[dict],
    right_reasons: list[str],
    min_frames: int,
    max_frames: int,
) -> bool:
    protected = {"scene_boundary", "large_temporal_gap", "maximum_temporal_span"}
    combined = left + right
    partition = _partition_lengths(len(combined), min_frames, max_frames)
    fully_partitionable = sum(partition) == len(combined) and all(
        min_frames <= length <= max_frames for length in partition
    )
    return (
        bool(left and right)
        and not protected.intersection(right_reasons)
        and _cluster_span(combined) <= MAX_WINDOW_SPAN_SECONDS
        and (fully_partitionable or len(combined) < min_frames)
    )


def _coalesce_short_clusters(
    clusters: list[tuple[list[dict], list[str]]],
    min_frames: int,
    max_frames: int,
) -> list[tuple[list[dict], list[str]]]:
    """Merge only short fragments whose separating policy marker is soft."""

    pending = [(list(cluster), list(reasons)) for cluster, reasons in clusters]
    index = 0
    while index < len(pending):
        cluster, reasons = pending[index]
        if len(cluster) >= min_frames:
            index += 1
            continue
        if index > 0:
            left, left_reasons = pending[index - 1]
            if _merge_allowed(left, cluster, reasons, min_frames, max_frames):
                pending[index - 1] = (
                    left + cluster,
                    sorted(set(left_reasons + reasons + ["merged_short_fragment"])),
                )
                pending.pop(index)
                index = max(0, index - 1)
                continue
        if index + 1 < len(pending):
            right, right_reasons = pending[index + 1]
            if _merge_allowed(cluster, right, right_reasons, min_frames, max_frames):
                pending[index] = (
                    cluster + right,
                    sorted(set(reasons + right_reasons + ["merged_short_fragment"])),
                )
                pending.pop(index + 1)
                continue
        index += 1
    return pending


def _identity_digest(entries: list[dict], split_reasons: list[str], *, kind: str) -> str:
    identity_payload: list[dict[str, Any]] = [
        {
            "timestamp_seconds": item["timestamp_seconds"],
            "fingerprint": item["fingerprint"],
            "sample_reasons": item["sample_reasons"],
        }
        for item in entries
    ]
    identity_payload.append({
        "kind": kind,
        "policy": WINDOW_POLICY_NAME,
        "version": WINDOW_POLICY_VERSION,
        "split_reasons": split_reasons,
    })
    return hashlib.sha256(
        json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _public_frames(entries: list[dict]) -> list[dict]:
    return [
        {
            "frame_index": index,
            "frame_path": item.get("frame_path", ""),
            "timestamp_seconds": item["timestamp_seconds"],
            "sample_reasons": item["sample_reasons"],
            "fingerprint": item["fingerprint"],
        }
        for index, item in enumerate(entries)
    ]


def _non_mandatory_fragment(entries: list[dict], split_reasons: list[str], duration_seconds: float) -> dict:
    reasons = sorted(set(split_reasons + ["insufficient_scene_samples"]))
    digest = _identity_digest(entries, reasons, kind="non_mandatory_fragment")
    return {
        "schema_version": MULTI_FRAME_SCHEMA_VERSION,
        "fragment_uuid": f"fragment_{digest[:24]}",
        "mandatory": False,
        "reason": "insufficient_scene_samples",
        "window_policy": {
            "name": WINDOW_POLICY_NAME,
            "version": WINDOW_POLICY_VERSION,
            "split_reasons": reasons,
        },
        "frames": _public_frames(entries),
        "start_seconds": entries[0]["timestamp_seconds"] if entries else 0.0,
        "end_seconds": entries[-1]["timestamp_seconds"] if entries else 0.0,
        "duration_seconds": round(max(0.0, float(duration_seconds or 0)), 6),
        "validation": {
            "status": "uncovered",
            "mandatory": False,
            "model_result_required": False,
            "needs_review_reasons": ["insufficient_scene_samples"],
        },
    }


def plan_frame_windows(
    frame_manifest: list[dict],
    duration_seconds: float,
    *,
    min_frames: int = MIN_WINDOW_FRAMES,
    max_frames: int = MAX_WINDOW_FRAMES,
) -> dict[str, Any]:
    """Plan valid mandatory windows and explicit non-mandatory fragments."""

    if min_frames != MIN_WINDOW_FRAMES or max_frames != MAX_WINDOW_FRAMES:
        if not 2 <= min_frames <= max_frames <= 8:
            raise ValueError("multi-frame window size must be between 2 and 8")
    ordered = sorted(
        (dict(item) for item in frame_manifest),
        key=lambda item: (float(item.get("timestamp_seconds") or 0), str(item.get("frame_path") or "")),
    )
    for item in ordered:
        path = Path(str(item.get("frame_path") or ""))
        try:
            fingerprint = frame_fingerprint(path) if path.is_file() else ""
        except OSError:
            fingerprint = ""
        item["timestamp_seconds"] = round(float(item.get("timestamp_seconds") or 0), 6)
        item["sample_reasons"] = sorted(str(reason) for reason in item.get("sample_reasons") or ["baseline"])
        item["fingerprint"] = fingerprint

    windows: list[dict] = []
    fragments: list[dict] = []
    ordinal = 0
    clusters = _coalesce_short_clusters(_candidate_clusters(ordered), min_frames, max_frames)
    for cluster, cluster_reasons in clusters:
        if len(cluster) < min_frames:
            fragments.append(_non_mandatory_fragment(cluster, cluster_reasons, duration_seconds))
            continue
        lengths = _partition_lengths(len(cluster), min_frames, max_frames)
        cursor = 0
        for length in lengths:
            entries = cluster[cursor : cursor + length]
            cursor += length
            ordinal += 1
            split_reasons = list(cluster_reasons)
            digest = _identity_digest(entries, split_reasons, kind="mandatory_window")
            window = {
                "schema_version": MULTI_FRAME_SCHEMA_VERSION,
                "window_policy": {"name": WINDOW_POLICY_NAME, "version": WINDOW_POLICY_VERSION, "split_reasons": split_reasons},
                "window_uuid": f"window_{digest[:24]}",
                "ordinal": ordinal,
                "mandatory": True,
                "frames": _public_frames(entries),
                "start_seconds": entries[0]["timestamp_seconds"] if entries else 0.0,
                "end_seconds": entries[-1]["timestamp_seconds"] if entries else 0.0,
                "duration_seconds": round(max(0.0, float(duration_seconds or 0)), 6),
            }
            window["validation"] = validate_window(window, min_frames=min_frames, max_frames=max_frames)
            windows.append(window)
        if cursor < len(cluster):
            fragments.append(_non_mandatory_fragment(
                cluster[cursor:],
                cluster_reasons + ["provider_partition_remainder"],
                duration_seconds,
            ))
    return {
        "mandatory_windows": windows,
        "non_mandatory_fragments": fragments,
        "planning_summary": {
            "mandatory_window_count": len(windows),
            "non_mandatory_fragment_count": len(fragments),
            "non_mandatory_frame_count": sum(len(item.get("frames") or []) for item in fragments),
            "policy_name": WINDOW_POLICY_NAME,
            "policy_version": WINDOW_POLICY_VERSION,
        },
    }


def build_frame_windows(
    frame_manifest: list[dict],
    duration_seconds: float,
    *,
    min_frames: int = MIN_WINDOW_FRAMES,
    max_frames: int = MAX_WINDOW_FRAMES,
) -> list[dict]:
    """Compatibility wrapper returning only true mandatory model windows."""

    return list(plan_frame_windows(
        frame_manifest,
        duration_seconds,
        min_frames=min_frames,
        max_frames=max_frames,
    )["mandatory_windows"])


def provider_capability(provider: Any, cfg: Mapping[str, Any] | None = None) -> dict:
    """Resolve an explicit multi-image capability; local/cloud never assume support."""

    name = str(getattr(provider, "provider", "") or "")
    if name == "mock":
        return {
            "supports_multi_image": True,
            "maximum_images": 5,
            "supported_image_formats": list(SUPPORTED_IMAGE_FORMATS),
            "provider_contract_version": MULTI_FRAME_CONTRACT_VERSION,
            "prompt_contract_version": MULTI_FRAME_PROMPT_VERSION,
            "schema_version": MULTI_FRAME_SCHEMA_VERSION,
            "capability_source": "built_in_mock",
        }
    cfg = cfg or {}
    provider_cfg = ((cfg.get("ai") or {}).get(name) or {})
    capability = provider_cfg.get("multi_frame_capability")
    registry_resolution: Mapping[str, Any] | None = None
    if name == "local":
        base_url = str(
            getattr(provider, "base_url", "")
            or provider_cfg.get("base_url")
            or provider_cfg.get("lmstudio_url")
            or ""
        ).rstrip("/")
        model = str(getattr(provider, "model", "") or provider_cfg.get("model") or "")
        if base_url and model:
            registry_resolution = resolve_verified_probe_capability(
                cfg,
                provider=name,
                model=model,
                base_url=base_url,
                provider_contract_version=MULTI_FRAME_CONTRACT_VERSION,
                prompt_contract_version=MULTI_FRAME_PROMPT_VERSION,
                capability_schema_version=MULTI_FRAME_SCHEMA_VERSION,
            )
            if registry_resolution.get("status") == "pass":
                capability = registry_resolution.get("capability")
            elif registry_resolution.get("status") == "blocked":
                return {
                    "supports_multi_image": False,
                    "maximum_images": 0,
                    "supported_image_formats": [],
                    "provider_contract_version": MULTI_FRAME_CONTRACT_VERSION,
                    "prompt_contract_version": MULTI_FRAME_PROMPT_VERSION,
                    "schema_version": MULTI_FRAME_SCHEMA_VERSION,
                    "capability_source": "verified_probe_blocked",
                    "registry_status": "blocked",
                    "registry_reason": str(registry_resolution.get("reason") or "verified_probe_record_invalid"),
                    "binding_fingerprint": str(registry_resolution.get("binding_fingerprint") or ""),
                }
    if not isinstance(capability, Mapping):
        return {
            "supports_multi_image": False,
            "maximum_images": 0,
            "supported_image_formats": [],
            "provider_contract_version": "",
            "prompt_contract_version": "",
            "schema_version": 0,
            "capability_source": "missing",
            "registry_status": str((registry_resolution or {}).get("status") or "not_checked"),
            "registry_reason": str((registry_resolution or {}).get("reason") or ""),
        }
    source = str(capability.get("capability_source") or "explicit_config")
    supported = [str(item).lower() for item in capability.get("supported_image_formats") or []]
    maximum = capability.get("maximum_images")
    valid = (
        source in CAPABILITY_SOURCES - {"built_in_mock"}
        and isinstance(capability.get("supports_multi_image"), bool)
        and isinstance(maximum, int)
        and 3 <= maximum <= MAX_WINDOW_FRAMES
        and "jpeg" in supported
        and supported
        and all(item in SUPPORTED_IMAGE_FORMATS for item in supported)
        and str(capability.get("provider_contract_version") or "")
        and str(capability.get("prompt_contract_version") or "")
        and int(capability.get("schema_version") or 0) == MULTI_FRAME_SCHEMA_VERSION
    )
    if not valid:
        return {
            "supports_multi_image": False,
            "maximum_images": 0,
            "supported_image_formats": [],
            "provider_contract_version": "",
            "prompt_contract_version": "",
            "schema_version": 0,
            "capability_source": "invalid",
        }
    resolved = {
        "supports_multi_image": bool(capability["supports_multi_image"]),
        "maximum_images": int(maximum),
        "supported_image_formats": supported,
        "provider_contract_version": str(capability["provider_contract_version"]),
        "prompt_contract_version": str(capability["prompt_contract_version"]),
        "schema_version": MULTI_FRAME_SCHEMA_VERSION,
        "capability_source": source,
    }
    if source == "verified_probe":
        for key in (
            "verification_source",
            "verified_at",
            "binding_fingerprint",
            "verification_fingerprint",
            "endpoint_identity",
            "registry_contract",
        ):
            resolved[key] = capability.get(key)
    return resolved


def validate_window(window: Mapping[str, Any], *, min_frames: int = MIN_WINDOW_FRAMES, max_frames: int = MAX_WINDOW_FRAMES) -> dict:
    frames = list(window.get("frames") or [])
    duration = float(window.get("duration_seconds") or 0)
    timestamps = [float(frame.get("timestamp_seconds") or 0) for frame in frames]
    checks = []
    reasons: list[str] = []

    count_ok = min_frames <= len(frames) <= max_frames
    checks.append({"code": "frame_count", "status": "pass" if count_ok else "skipped", "value": len(frames)})
    if len(frames) < min_frames:
        return {
            "status": "skipped",
            "checks": checks,
            "evidence_artifact_ids": [],
            "needs_review_reasons": ["insufficient_evidence_frames"],
        }
    if len(frames) > max_frames:
        reasons.append("window_frame_count_out_of_range")

    ordered_ok = all(left < right for left, right in zip(timestamps, timestamps[1:]))
    checks.append({"code": "timestamps_strictly_increasing", "status": "pass" if ordered_ok else "blocked"})
    if not ordered_ok:
        reasons.append("timestamps_not_strictly_increasing")

    duration_ok = all(0 <= timestamp <= duration + 0.001 for timestamp in timestamps) if duration > 0 else all(timestamp >= 0 for timestamp in timestamps)
    checks.append({"code": "timestamps_in_source_duration", "status": "pass" if duration_ok else "blocked"})
    if not duration_ok:
        reasons.append("timestamp_outside_source_duration")

    fingerprints_ok = all(len(str(frame.get("fingerprint") or "")) == 64 for frame in frames)
    checks.append({"code": "frame_fingerprints_complete", "status": "pass" if fingerprints_ok else "blocked"})
    if not fingerprints_ok:
        reasons.append("missing_frame_fingerprint")

    status = "pass" if not reasons else "blocked"
    return {
        "status": status,
        "checks": checks,
        "evidence_artifact_ids": [],
        "needs_review_reasons": reasons,
    }


def window_cache_key(
    window: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    prompt_version: str = MULTI_FRAME_PROMPT_VERSION,
    schema_version: int = MULTI_FRAME_SCHEMA_VERSION,
    policy: Mapping[str, Any] | None = None,
    provider_contract_version: str = MULTI_FRAME_CONTRACT_VERSION,
    maximum_images: int = MAX_WINDOW_FRAMES,
    supported_image_formats: list[str] | tuple[str, ...] = SUPPORTED_IMAGE_FORMATS,
) -> str:
    payload = {
        "contract_version": MULTI_FRAME_CONTRACT_VERSION,
        "provider_contract_version": str(provider_contract_version),
        "schema_version": int(schema_version),
        "prompt_version": str(prompt_version),
        "provider": str(provider),
        "model": str(model),
        "maximum_images": int(maximum_images),
        "supported_image_formats": sorted(str(item) for item in supported_image_formats),
        "window_policy": dict(policy or window.get("window_policy") or {}),
        "frames": [
            {
                "fingerprint": str(frame.get("fingerprint") or ""),
                "timestamp_seconds": round(float(frame.get("timestamp_seconds") or 0), 6),
            }
            for frame in window.get("frames") or []
        ],
        "start_seconds": round(float(window.get("start_seconds") or 0), 6),
        "end_seconds": round(float(window.get("end_seconds") or 0), 6),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"multiframe_{digest}"


def parse_window_response(raw: Mapping[str, Any]) -> dict:
    """Extract JSON from chat-completion or Responses-style provider output."""

    text_value = raw.get("output_text")
    text = text_value if isinstance(text_value, (str, list)) else ""
    if not text:
        choices = raw.get("choices") or []
        if choices:
            content = ((choices[0].get("message") or {}).get("content") or "")
            text = content if isinstance(content, (str, list)) else ""
    if not text:
        for item in raw.get("output") or []:
            for content in item.get("content") or []:
                text += str(content.get("text") or "")
    if isinstance(text, list):
        text = "".join(
            str(item.get("text") or "") if isinstance(item, Mapping) else str(item)
            for item in text
        )
    text = str(text).strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("multi-frame provider response must be a JSON object")
    return data


def normalize_window_result(payload: Mapping[str, Any], window: Mapping[str, Any]) -> dict:
    """Validate and normalize the provider's clip-level response."""

    required = {
        "summary", "action", "start_seconds", "end_seconds", "shot_role",
        "technical_quality", "duplicate_group", "natural_audio_recommendation", "confidence",
    }
    missing = sorted(key for key in required if key not in payload)
    if missing:
        raise MultiFrameValidationError(f"multi-frame response missing fields: {', '.join(missing)}")
    try:
        start = float(payload["start_seconds"])
        end = float(payload["end_seconds"])
        confidence = float(payload["confidence"])
    except (TypeError, ValueError) as exc:
        raise MultiFrameValidationError("multi-frame response has invalid numeric fields") from exc
    duration = float(window.get("duration_seconds") or 0)
    window_start = float(window.get("start_seconds") or 0)
    window_end = float(window.get("end_seconds") or 0)
    if not all(math.isfinite(value) for value in (start, end, confidence)):
        raise MultiFrameValidationError("multi-frame response contains non-finite values")
    confidence = _normalize_unit_score(confidence, "multi-frame confidence")
    if end <= start or start < window_start - 0.001 or end > window_end + 0.001 or (duration > 0 and end > duration + 0.001):
        raise MultiFrameValidationError("multi-frame action range is outside its evidence window")
    technical = payload["technical_quality"]
    if isinstance(technical, Mapping):
        quality_score = float(technical.get("score", 0))
        quality_issues = [str(item) for item in technical.get("issues") or []]
    else:
        quality_score = float(technical)
        quality_issues = []
    quality_score = _normalize_unit_score(quality_score, "technical_quality score")
    tags = [str(tag) for tag in payload.get("tags") or [] if str(tag) in TAGS]
    recommendation = str(payload["natural_audio_recommendation"]).lower()
    if recommendation not in {"keep", "lower", "mute", "unknown"}:
        raise MultiFrameValidationError("natural_audio_recommendation is invalid")
    return {
        "schema_version": MULTI_FRAME_SCHEMA_VERSION,
        "summary": str(payload["summary"]),
        "action": str(payload["action"]),
        "start_seconds": round(start, 6),
        "end_seconds": round(end, 6),
        "shot_role": str(payload["shot_role"]),
        "technical_quality": {"score": round(quality_score, 6), "issues": quality_issues},
        "duplicate_group": str(payload["duplicate_group"] or ""),
        "natural_audio_recommendation": recommendation,
        "confidence": round(confidence, 6),
        "tags": tags,
    }


def _normalize_unit_score(value: float, label: str) -> float:
    """Accept provider 0-1, 0-10, or percentage scores deterministically."""

    if not math.isfinite(value) or value < 0:
        raise MultiFrameValidationError(f"{label} must be a finite non-negative number")
    if value <= 1:
        return value
    if value <= 10:
        return value / 10
    if value <= 100:
        return value / 100
    raise MultiFrameValidationError(f"{label} must be between 0 and 1, 0 and 10, or 0 and 100")


def _write_contact_sheet(frame_paths: list[Path], output: Path, ffmpeg_path: str | None) -> str:
    if not ffmpeg_path or not shutil.which(ffmpeg_path):
        return "ffmpeg_unavailable"
    output.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    labels = []
    for index in range(len(frame_paths)):
        filters.append(f"[{index}:v]scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2[v{index}]")
        labels.append(f"[v{index}]")
    graph = ";".join(filters) + ";" + "".join(labels) + f"hstack=inputs={len(frame_paths)}[out]"
    command = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y"]
    for path in frame_paths:
        command.extend(["-i", str(path)])
    command.extend(["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", str(output)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not output.is_file():
        return (completed.stderr or "contact sheet generation failed").strip()[:500]
    return ""


def write_non_mandatory_evidence(fragments: list[dict], evidence_root: Path) -> str:
    """Persist uncovered fragments without presenting them as model results."""

    public_fragments = []
    for fragment in fragments:
        public = {key: value for key, value in fragment.items() if key != "frames"}
        public["frames"] = [
            {key: value for key, value in frame.items() if key != "frame_path"}
            for frame in fragment.get("frames") or []
        ]
        public_fragments.append(public)
    path = evidence_root / "non_mandatory_fragments.json"
    atomic_write_json(path, {
        "schema_version": MULTI_FRAME_SCHEMA_VERSION,
        "window_policy": {"name": WINDOW_POLICY_NAME, "version": WINDOW_POLICY_VERSION},
        "fragments": public_fragments,
    })
    return str(path)


def write_window_evidence(
    window: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    validation: Mapping[str, Any],
    evidence_root: Path,
    *,
    ffmpeg_path: str | None = None,
    raw_response: Mapping[str, Any] | None = None,
    provider_contract: Mapping[str, Any] | None = None,
    segment_uuid: str = "",
) -> dict:
    """Write a run-scoped evidence bundle without exposing it to the browser."""

    window_uuid = str(window.get("window_uuid") or "window_unknown")
    directory = evidence_root / window_uuid
    directory.mkdir(parents=True, exist_ok=True)
    public_window = {
        key: value for key, value in window.items() if key != "frames"
    }
    public_window["frames"] = [
        {key: value for key, value in frame.items() if key != "frame_path"}
        for frame in window.get("frames") or []
    ]
    (directory / "window.json").write_text(json.dumps(public_window, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "validation.json").write_text(json.dumps(dict(validation), ensure_ascii=False, indent=2), encoding="utf-8")
    if result is not None:
        normalized = dict(result)
        if segment_uuid:
            normalized["segment_uuid"] = str(segment_uuid)
        (directory / "normalized.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw_response is not None:
        raw_public = _scrub_sensitive(raw_response)
        (directory / "raw_response.json").write_text(json.dumps(raw_public, ensure_ascii=False, indent=2), encoding="utf-8")
    if provider_contract is not None:
        (directory / "provider_contract.json").write_text(json.dumps(dict(provider_contract), ensure_ascii=False, indent=2), encoding="utf-8")
    frame_paths = [Path(str(frame.get("frame_path") or "")) for frame in window.get("frames") or []]
    contact_sheet = directory / "contact_sheet.jpg"
    contact_error = _write_contact_sheet(frame_paths, contact_sheet, ffmpeg_path) if frame_paths else "no frames"
    evidence = {
        "artifact_id": window_uuid,
        "window_json": str(directory / "window.json"),
        "validation_json": str(directory / "validation.json"),
        "normalized_json": str(directory / "normalized.json") if result is not None else "",
        "raw_response_json": str(directory / "raw_response.json") if raw_response is not None else "",
        "provider_contract_json": str(directory / "provider_contract.json") if provider_contract is not None else "",
        "contact_sheet": str(contact_sheet) if contact_sheet.is_file() else "",
    }
    if contact_error:
        evidence["contact_sheet_error"] = contact_error
    index_path = evidence_root / "raw_response_index.json"
    index = {}
    if index_path.is_file():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                index = existing
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            index = {}
    index[window_uuid] = {
        "window_uuid": window_uuid,
        "raw_response": f"{window_uuid}/raw_response.json" if raw_response is not None else "",
        "provider_contract": f"{window_uuid}/provider_contract.json" if provider_contract is not None else "",
        "status": str(validation.get("status") or "unknown"),
    }
    atomic_write_json(index_path, index)
    evidence["raw_response_index"] = str(index_path)
    return evidence


def _scrub_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_sensitive(item)
            for key, item in value.items()
            if str(key) not in {"frame_path", "source_file", "source_path", "original_source_path", "file_path"}
        }
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    return value


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish a complete JSON cache entry or leave the previous entry intact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(_scrub_sensitive(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def update_window_evidence_segment_uuid(
    evidence_root: Path,
    window_uuid: str,
    segment_uuid: str,
) -> None:
    """Add the published stable segment identity to an existing evidence bundle."""

    path = evidence_root / str(window_uuid) / "normalized.json"
    if not path.is_file():
        raise FileNotFoundError(f"normalized evidence is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("normalized evidence must be an object")
    payload["segment_uuid"] = str(segment_uuid)
    atomic_write_json(path, payload)
