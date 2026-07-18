"""Deterministic cache records for Render Pipeline v2 segment renders."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .render_types import ColorSettings, RenderProfile, RenderSegment


CACHE_VERSION = "segment-cache-v1"
ENGINE_VERSION = "segment-renderer-v2"


@dataclass(frozen=True)
class SegmentCachePaths:
    key: str
    media: Path
    metadata: Path
    log: Path

    @property
    def media_path(self) -> Path:
        return self.media

    @property
    def metadata_path(self) -> Path:
        return self.metadata

    @property
    def log_path(self) -> Path:
        return self.log


class SegmentCache:
    """Compatibility facade over the functional cache API."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def key(self, segment: RenderSegment, settings: Any, profile: RenderProfile | Mapping[str, Any] | str, *, encoder: str = "") -> str:
        return segment_cache_key(segment, color=getattr(settings, "color", settings), profile=profile, encoder=encoder)

    def entry(self, key: str) -> SegmentCachePaths:
        return cache_paths(self.root, key)

    def get(self, segment: RenderSegment, settings: Any, profile: RenderProfile | Mapping[str, Any] | str, *, encoder: str = "") -> SegmentCachePaths | None:
        color = getattr(settings, "color", settings)
        payload = cache_key_payload(segment, color=color, profile=profile, encoder=encoder)
        key = segment_cache_key(segment, color=color, profile=profile, encoder=encoder)
        paths = self.entry(key)
        return paths if is_valid_cache(paths, payload) else None


def cache_key_payload(
    segment: RenderSegment,
    *,
    color: ColorSettings | Mapping[str, Any] | None = None,
    profile: RenderProfile | Mapping[str, Any] | str = "",
    encoder: str = "",
    engine_version: str = ENGINE_VERSION,
    cache_version: str = CACHE_VERSION,
) -> dict[str, Any]:
    """Return every render-affecting value used by the cache key."""

    source = Path(segment.source_file).expanduser().resolve()
    source_stat = _stat(source)
    color_data = _as_mapping(color)
    lut_path = color_data.get("lut_path") or color_data.get("dji_lut_path")
    lut_stat = _stat(Path(lut_path).expanduser().resolve()) if lut_path else {}
    profile_data = _as_mapping(profile)
    return {
        "source_file": str(source),
        "source_size": source_stat.get("size"),
        "source_mtime_ns": source_stat.get("mtime_ns"),
        "source_in_ms": int(segment.source_in_ms),
        "source_out_ms": int(segment.source_out_ms),
        "speed": float(segment.speed),
        "audio_role": segment.audio_role,
        "color_mode": color_data.get("mode", "none"),
        "color_decision": color_data.get("decision", ""),
        "lut_path": str(Path(lut_path).expanduser().resolve()) if lut_path else None,
        "lut_mtime_ns": lut_stat.get("mtime_ns"),
        "profile": profile_data.get("name", profile if isinstance(profile, str) else ""),
        "encoder": encoder,
        "engine_version": engine_version,
        "cache_version": cache_version,
    }


def segment_cache_key(*args: Any, **kwargs: Any) -> str:
    payload = cache_key_payload(*args, **kwargs)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_paths(cache_dir: str | Path, key: str) -> SegmentCachePaths:
    root = Path(cache_dir)
    return SegmentCachePaths(key, root / f"{key}.mp4", root / f"{key}.json", root / f"{key}.log")


def is_valid_cache(paths: SegmentCachePaths, expected_payload: Mapping[str, Any]) -> bool:
    """Accept a cache only when media and its immutable metadata are intact."""

    if not paths.media.is_file() or paths.media.stat().st_size <= 0 or not paths.metadata.is_file():
        return False
    try:
        metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return metadata.get("key") == paths.key and metadata.get("key_payload") == dict(expected_payload)


def write_cache_metadata(paths: SegmentCachePaths, payload: Mapping[str, Any], **extra: Any) -> None:
    paths.metadata.parent.mkdir(parents=True, exist_ok=True)
    record = {"key": paths.key, "key_payload": dict(payload), **extra}
    paths.metadata.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _stat(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    if isinstance(value, str):
        return {"name": value}
    return {}


__all__ = ["CACHE_VERSION", "ENGINE_VERSION", "SegmentCache", "SegmentCachePaths", "cache_key_payload", "segment_cache_key", "cache_paths", "is_valid_cache", "write_cache_metadata"]
