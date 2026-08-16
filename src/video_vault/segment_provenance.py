"""Stable approval-semantic provenance for rendered segments.

This identity is deliberately independent from rendered-artifact cache keys.
Encoder resolution, renderer/cache contracts, diagnostics, and artifact paths
must be free to change without requiring re-approval of the edit semantics.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SEGMENT_APPROVAL_PROVENANCE_VERSION = "segment-approval-provenance-v1"


def approval_source_fingerprints(snapshot: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """Index immutable approval source evidence by resolved path."""

    result: dict[str, Mapping[str, Any]] = {}
    for asset in (snapshot or {}).get("assets", []) or []:
        if not isinstance(asset, Mapping):
            continue
        if str(asset.get("kind") or "") not in {"source", "source_media"}:
            continue
        path = str(asset.get("canonical_path") or "").strip()
        if path:
            result[str(Path(path).expanduser().resolve())] = asset
    return result


def segment_approval_provenance(
    manifest: Mapping[str, Any],
    segment: Mapping[str, Any],
    *,
    source_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the version/hash/payload for approved segment semantics.

    The payload intentionally excludes source paths, artifact paths, cache
    keys, encoder settings, renderer versions, and probe diagnostics.
    """

    audio = segment.get("audio") if isinstance(segment.get("audio"), Mapping) else {}
    source = dict(source_fingerprint or {})
    source_identity = source.get("source_identity")
    source_payload = {
        "contract_version": str(source.get("contract_version") or ""),
        "sha256": str(source.get("sha256") or ""),
        "size": _int_or_none(source.get("size", source.get("size_bytes"))),
        "mtime_ns": _int_or_none(source.get("mtime_ns")),
        "source_identity": dict(source_identity) if isinstance(source_identity, Mapping) else {},
        "available": bool(str(source.get("sha256") or "")),
    }
    profile = manifest.get("profile") if isinstance(manifest.get("profile"), Mapping) else {}
    payload = {
        "version": SEGMENT_APPROVAL_PROVENANCE_VERSION,
        "segment_id": str(segment.get("segment_id") or segment.get("segment_uuid") or ""),
        "order": _int_or_none(segment.get("order")),
        "source_fingerprint": source_payload,
        "source_in_seconds": _number(segment.get("source_in_seconds")),
        "source_out_seconds": _number(segment.get("source_out_seconds")),
        "source_duration_seconds": _number(segment.get("source_duration_seconds")),
        "speed": _number(segment.get("speed"), default=1.0),
        "timeline_duration_seconds": _number(segment.get("timeline_duration_seconds")),
        "audio": {
            "role": str(audio.get("role") or segment.get("audio_role") or ""),
            "volume_db": _number_or_none(audio.get("volume_db")),
            "fade_in_seconds": _number_or_none(audio.get("fade_in_seconds")),
            "fade_out_seconds": _number_or_none(audio.get("fade_out_seconds")),
        },
        "profile": {
            key: profile.get(key)
            for key in (
                "profile_id",
                "width",
                "height",
                "fps",
                "video_codec",
                "pixel_format",
                "audio_codec",
                "audio_sample_rate",
                "audio_channels",
            )
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "version": SEGMENT_APPROVAL_PROVENANCE_VERSION,
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


def _number(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return round(number, 6)


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return _number(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "SEGMENT_APPROVAL_PROVENANCE_VERSION",
    "approval_source_fingerprints",
    "segment_approval_provenance",
]
