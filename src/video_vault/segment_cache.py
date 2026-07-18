"""Deterministic hash cache for normalized single-segment outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SEGMENT_RENDERER_CONTRACT_VERSION = 1


def cache_key_payload(manifest: Mapping[str, Any], segment: Mapping[str, Any]) -> dict[str, Any]:
    source = Path(str(segment["source_file"])).expanduser().resolve()
    stat = source.stat() if source.exists() else None
    profile = dict(manifest.get("profile") or {})
    settings = dict(manifest.get("settings") or {})
    color = dict(settings.get("color") or {})
    lut = Path(str(color.get("lut_path"))).expanduser().resolve() if color.get("lut_path") else None
    lut_stat = lut.stat() if lut and lut.exists() else None
    return {
        "contract_version": SEGMENT_RENDERER_CONTRACT_VERSION,
        "source_file": str(source),
        "source_size": stat.st_size if stat else None,
        "source_mtime_ns": stat.st_mtime_ns if stat else None,
        "segment_id": str(segment.get("segment_id") or ""),
        "source_in_seconds": float(segment.get("source_in_seconds") or 0),
        "source_out_seconds": float(segment.get("source_out_seconds") or 0),
        "speed": float(segment.get("speed") or 0),
        "audio_role": str(segment.get("audio_role") or ""),
        "profile": {key: profile.get(key) for key in ("profile_id", "width", "height", "fps", "video_codec", "pixel_format", "audio_codec", "audio_sample_rate", "audio_channels")},
        "encoder": settings.get("encoder", "auto"),
        "audio": dict(settings.get("audio") or {}),
        "color_mode": color.get("mode", "none"),
        "lut_path": str(lut) if lut else None,
        "lut_size": lut_stat.st_size if lut_stat else None,
        "lut_mtime_ns": lut_stat.st_mtime_ns if lut_stat else None,
        "lut_sha256": _sha256_file(lut) if lut and lut.is_file() else None,
    }


def build_segment_cache_key(manifest: Mapping[str, Any], segment: Mapping[str, Any]) -> str:
    payload = cache_key_payload(manifest, segment)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def segment_cache_key(manifest: Mapping[str, Any], segment: Mapping[str, Any]) -> str:
    return build_segment_cache_key(manifest, segment)


def cache_paths(cache_root: str | Path, cache_key: str) -> dict[str, Path]:
    root = Path(cache_root)
    return {"output": root / f"{cache_key}.mp4", "partial": root / f"{cache_key}.partial.mp4", "metadata": root / f"{cache_key}.json", "log": root / f"{cache_key}.log"}


def write_cache_metadata(path: Path, cache_key: str, payload: Mapping[str, Any], **extra: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"cache_key": cache_key, "key_payload": dict(payload), **extra}
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def read_cache_metadata(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["SEGMENT_RENDERER_CONTRACT_VERSION", "build_segment_cache_key", "cache_key_payload", "cache_paths", "read_cache_metadata", "segment_cache_key", "write_cache_metadata"]
